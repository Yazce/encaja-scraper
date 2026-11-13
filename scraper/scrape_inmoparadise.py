"""
Scraper de https://www.inmoparadise.com/for-sale/

- Recorre el listado y su paginación (?pag=N).
- Extrae de cada ficha: referencia, precio actual, precio anterior,
  % de bajada, zona, tipo, habitaciones, banos, m2 y URL.
- Guarda un snapshot en data/snapshot_latest.json (uso interno, solo
  para comparar contra la siguiente ejecucion).
- Compara contra el snapshot anterior para detectar pisos nuevos y
  bajadas de precio. Por cada cambio:
  - inserta una fila en la tabla "pisos" de Supabase (la misma base
    de datos que usa la app Encaja), usando SUPABASE_URL y
    SUPABASE_SERVICE_ROLE_KEY.
  - crea un GitHub Issue en este mismo repositorio usando
    GITHUB_TOKEN (ya lo provee el workflow).
  Si esas variables de entorno no estan presentes (p.ej. ejecucion
  local sin configurar), cada paso se omite sin fallar el resto.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from notify_push import notify_matching_compradores
from notify_inactivity import notify_stale_contacts

SITE_ROOT = "https://www.inmoparadise.com/"
BASE_URL = SITE_ROOT + "for-sale/es"  # version en español (tipo/caract. salen ya traducidos)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
# robots.txt de inmoparadise.com pide Crawl-delay: 6
CRAWL_DELAY_SECONDS = 6
REQUEST_TIMEOUT = 20
AGENTE_BOT = "Inmoparadise (auto)"

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "snapshot_latest.json"


@dataclass
class Listing:
    referencia: str
    precio: int | None
    precio_anterior: int | None
    bajada_pct: str | None
    zona: str
    tipo: str
    habitaciones: str | None
    banos: str | None
    m2: str | None
    url: str
    reservado: bool = False


def fetch_page(pag: int) -> str:
    resp = requests.get(
        BASE_URL, params={"pag": pag}, headers=HEADERS, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.text


def get_total_pages(soup: BeautifulSoup) -> int:
    nums = soup.select("#paginacion-numPaginas a")
    pages = [int(a.get_text(strip=True)) for a in nums if a.get_text(strip=True).isdigit()]
    return max(pages) if pages else 1


def parse_price(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_titulo(titulo: str) -> tuple[str, str]:
    """'Flat - Alicante (La Florida) , Built Surface 89m2, ...' -> (tipo, zona)"""
    tipo, _, resto = titulo.partition(" - ")
    zona_match = re.search(r"\(([^)]+)\)", resto)
    if zona_match:
        zona = zona_match.group(1).strip()
    else:
        zona = resto.split(",")[0].strip()
    return tipo.strip(), zona


def field_from_lista_datos(card, label: str) -> str | None:
    for li in card.select("li.bloque-icono-name-valor1"):
        spans = li.find_all("span")
        if len(spans) >= 2 and spans[0].get_text(strip=True) == label:
            return spans[1].get_text(strip=True)
    return None


def is_reservado(card) -> bool:
    """El sitio marca los pisos reservados con el texto "Reservado" (en la
    version en ingles aparecia "Reserved") junto a una imagen
    fondo_gestionada.svg superpuesta a la foto (tanto en el listado como en
    la ficha). Se detecta por texto, con la imagen como respaldo, para no
    depender de una clase CSS concreta."""
    texto = card.get_text(" ", strip=True)
    if re.search(r"\breservad[oa]\b", texto, re.IGNORECASE):
        return True
    if re.search(r"\breserved\b", texto, re.IGNORECASE):
        return True
    return card.select_one('img[src*="fondo_gestionada"]') is not None


def parse_card(card) -> Listing | None:
    referencia = field_from_lista_datos(card, "Referencia")
    if not referencia:
        return None

    titulo_el = card.select_one("h1.titulo")
    tipo, zona = parse_titulo(titulo_el.get_text(strip=True)) if titulo_el else ("", "")

    precio_el = card.select_one(".paginacion-ficha-tituloprecio")
    precio = parse_price(precio_el.get_text(strip=True)) if precio_el else None

    precio_anterior_el = card.select_one(".paginacion-ficha-precioanterior")
    precio_anterior = parse_price(precio_anterior_el.get_text(strip=True)) if precio_anterior_el else None

    bajada_el = card.select_one(".paginacion-ficha-precioporcentaje")
    bajada_pct = bajada_el.get_text(strip=True).replace("\xa0", " ") if bajada_el else None

    link_el = card.select_one("a.paginacion-ficha-masinfo") or card.select_one("a.irAfichaPropiedad")
    url = urljoin(SITE_ROOT, link_el["href"]) if link_el and link_el.get("href") else ""

    return Listing(
        referencia=referencia,
        precio=precio,
        precio_anterior=precio_anterior,
        bajada_pct=bajada_pct,
        zona=zona,
        tipo=tipo,
        habitaciones=field_from_lista_datos(card, "Habitaciones"),
        banos=field_from_lista_datos(card, "Baños"),
        m2=field_from_lista_datos(card, "Superficie"),
        url=url,
        reservado=is_reservado(card),
    )


# Tope de seguridad de paginas por si el sitio cambiara y nunca devolviera una
# pagina vacia (hoy hay unas 11 paginas de 12 pisos).
MAX_PAGINAS = 40


def scrape_all() -> dict[str, Listing]:
    # OJO: NO nos fiamos del paginador de la web (#paginacion-numPaginas): solo
    # ensena 9 numeros aunque haya mas paginas, y con eso el scraper se dejaba
    # sin leer los pisos de las paginas 10 en adelante. En su lugar se pide
    # pagina a pagina hasta que una no traiga fichas nuevas.
    listings: dict[str, Listing] = {}
    for pag in range(1, MAX_PAGINAS + 1):
        if pag > 1:
            time.sleep(CRAWL_DELAY_SECONDS)
        soup = BeautifulSoup(fetch_page(pag), "html.parser")
        cards = soup.select("article.paginacion-ficha.propiedad")
        nuevos = 0
        for card in cards:
            item = parse_card(card)
            if item and item.referencia not in listings:
                listings[item.referencia] = item
                nuevos += 1
        if not cards or nuevos == 0:
            break
    print(f"Leidas {pag} pagina(s): {len(listings)} pisos en la web.")
    return listings


def load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return {"fecha": None, "listings": {}}
    with SNAPSHOT_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_caract(item: Listing, extra: str = "") -> str:
    partes = []
    if item.habitaciones:
        partes.append(f"{item.habitaciones} hab")
    if item.banos:
        partes.append(f"{item.banos} baños")
    if item.m2:
        partes.append(item.m2)
    partes.append(f"ref. {item.referencia}")
    if extra:
        partes.append(extra)
    partes.append(item.url)
    return " · ".join(partes)


def compute_bajada_pct(precio: int | None, precio_anterior: int | None) -> str | None:
    if not precio or not precio_anterior or precio_anterior <= 0:
        return None
    return str(round((1 - precio / precio_anterior) * 100))


def piso_event(item: Listing, origen: str, precio_anterior: int | None = None) -> dict:
    bajada_pct = None
    extra = ""
    if origen == "bajada_precio" and precio_anterior:
        bajada_pct = compute_bajada_pct(item.precio, precio_anterior)
        extra = f"bajada de {precio_anterior:,}€ a {item.precio:,}€".replace(",", ".")
        if bajada_pct:
            extra += f" (-{bajada_pct}%)"

    return {
        "id": f"ip-{item.referencia}-{origen}-{item.precio}-{uuid.uuid4().hex[:6]}",
        "referencia": item.referencia,
        "zona": item.zona,
        "tipo": item.tipo,
        "precio": item.precio,
        "precio_anterior": precio_anterior,
        "bajada_pct": bajada_pct,
        "caract": build_caract(item, extra),
        "agente": AGENTE_BOT,
        "origen": origen,
        "url": item.url,
        "reservado": item.reservado,
        "fecha": datetime.now(timezone.utc).isoformat(),
    }


def fmt_money(n: int | None) -> str:
    return f"{n:,}".replace(",", ".") + " €" if n is not None else "—"


def issue_title(event: dict) -> str:
    zona = event["zona"] or "Sin zona"
    if event["origen"] == "nuevo":
        return f"[Piso nuevo] {zona} - {fmt_money(event['precio'])}"
    return (
        f"[Bajada] {zona} - "
        f"{fmt_money(event['precio_anterior'])} -> {fmt_money(event['precio'])}"
    )


def issue_body(event: dict) -> str:
    lineas = [
        f"**Zona:** {event['zona'] or '—'}",
        f"**Tipo:** {event['tipo'] or '—'}",
        f"**Precio:** {fmt_money(event['precio'])}",
    ]
    if event["origen"] == "bajada_precio":
        extra = f"**Precio anterior:** {fmt_money(event['precio_anterior'])}"
        if event.get("bajada_pct"):
            extra += f" (-{event['bajada_pct']}%)"
        lineas.append(extra)
    lineas.append(f"**Características:** {event['caract']}")
    lineas.append(f"**Ficha:** {event['url']}")
    return "\n\n".join(lineas)


def create_github_issue(event: dict) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"title": issue_title(event), "body": issue_body(event)},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 300:
        print(f"No se pudo crear el issue para {event['referencia']}: "
              f"{resp.status_code} {resp.text}")


def insert_piso_supabase(event: dict) -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return

    resp = requests.post(
        f"{url.rstrip('/')}/rest/v1/pisos",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json=event,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 300:
        print(f"No se pudo guardar en Supabase el piso {event['referencia']}: "
              f"{resp.status_code} {resp.text}")


def update_reservado_supabase(referencia: str, reservado: bool) -> None:
    """Actualiza el campo reservado en TODAS las filas ya guardadas para esa
    referencia (puede haber mas de una fila si hubo evento de 'nuevo' y
    luego de 'bajada_precio'), para que ninguna quede desactualizada."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return

    resp = requests.patch(
        f"{url.rstrip('/')}/rest/v1/pisos",
        params={"referencia": f"eq.{referencia}"},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"reservado": reservado},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code >= 300:
        print(f"No se pudo actualizar 'reservado' para {referencia}: "
              f"{resp.status_code} {resp.text}")


def detect_changes(
    previous: dict[str, dict], current: dict[str, Listing]
) -> tuple[list[dict], list[tuple[str, bool]]]:
    events: list[dict] = []
    reservado_updates: list[tuple[str, bool]] = []

    for referencia, item in current.items():
        prev = previous.get(referencia)
        if prev is None:
            events.append(piso_event(item, "nuevo"))
            continue

        prev_precio = prev.get("precio")
        if item.precio is not None and prev_precio is not None and item.precio < prev_precio:
            events.append(piso_event(item, "bajada_precio", precio_anterior=prev_precio))

        # Un piso ya conocido puede pasar a reservado (o dejar de estarlo)
        # sin que haya cambio de precio ni sea "nuevo": lo detectamos y
        # actualizamos aparte, sin generar issue de GitHub por esto.
        if item.reservado != bool(prev.get("reservado", False)):
            reservado_updates.append((referencia, item.reservado))

    return events, reservado_updates


def referencias_en_supabase() -> set[str] | None:
    """Referencias que ya hay en la tabla pisos (None si no hay credenciales)."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    refs: set[str] = set()
    offset = 0
    while True:
        resp = requests.get(
            f"{url.rstrip('/')}/rest/v1/pisos",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Range-Unit": "items",
                "Range": f"{offset}-{offset + 999}",
            },
            params={"select": "referencia"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        refs.update(r["referencia"] for r in rows if r.get("referencia"))
        if len(rows) < 1000:
            break
        offset += 1000
    return refs


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = load_snapshot()
    previous_listings: dict[str, dict] = snapshot.get("listings", {})
    # Los snapshots anteriores se hicieron leyendo solo 9 paginas (ver
    # scrape_all). La primera vez con el recuento completo es un "punto de
    # partida": los pisos que faltaban se guardan en Supabase si no estaban,
    # pero SIN avisos push ni issues, porque no son novedades de verdad.
    es_punto_de_partida = not snapshot.get("recuento_completo")

    current_listings = scrape_all()

    if not current_listings:
        print(
            "La pagina no devolvio ningun piso (posible cambio en el HTML del "
            "sitio o fallo de red). No se toca el snapshot ni se procesa nada, "
            "para no marcar como 'desaparecidos' los pisos ya conocidos."
        )
        return

    events, reservado_updates = detect_changes(previous_listings, current_listings)

    if es_punto_de_partida:
        print(f"Primer recuento completo: {len(events)} evento(s) se tratan como punto "
              "de partida (sin avisos ni issues).")
        try:
            existentes = referencias_en_supabase()
        except requests.RequestException as e:
            existentes = None
            print(f"No se pudo leer la tabla pisos de Supabase: {e}")
        if existentes is not None:
            faltan = [e for e in events if e["origen"] == "nuevo" and e["referencia"] not in existentes]
            for event in faltan:
                try:
                    insert_piso_supabase(event)
                except requests.RequestException as e:
                    print(f"Error guardando en Supabase {event['referencia']}: {e}")
            print(f"{len(faltan)} piso(s) que faltaban se han guardado en Supabase.")
        events = []

    if events:
        print(f"{len(events)} cambio(s) detectado(s): "
              f"{sum(1 for e in events if e['origen'] == 'nuevo')} nuevos, "
              f"{sum(1 for e in events if e['origen'] == 'bajada_precio')} bajadas de precio.")

        for event in events:
            try:
                insert_piso_supabase(event)
            except requests.RequestException as e:
                print(f"Error guardando en Supabase {event['referencia']}: {e}")
            try:
                create_github_issue(event)
            except requests.RequestException as e:
                print(f"Error creando issue para {event['referencia']}: {e}")

        try:
            notify_matching_compradores(events)
        except Exception as e:
            print(f"Error mandando avisos push: {e}")
    else:
        print("Sin cambios respecto al snapshot anterior.")

    if reservado_updates:
        pasan_a_reservado = sum(1 for _, r in reservado_updates if r)
        print(f"{len(reservado_updates)} piso(s) cambian de estado 'reservado' "
              f"({pasan_a_reservado} pasan a reservados, "
              f"{len(reservado_updates) - pasan_a_reservado} dejan de estarlo).")
        for referencia, reservado in reservado_updates:
            try:
                update_reservado_supabase(referencia, reservado)
            except requests.RequestException as e:
                print(f"Error actualizando 'reservado' para {referencia}: {e}")

    try:
        notify_stale_contacts()
    except Exception as e:
        print(f"Error mandando avisos de inactividad: {e}")

    new_snapshot = {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "total_pisos": len(current_listings),
        "recuento_completo": True,
        "listings": {ref: asdict(item) for ref, item in current_listings.items()},
    }
    with SNAPSHOT_PATH.open("w", encoding="utf-8") as f:
        json.dump(new_snapshot, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
