"""
Scraper de https://www.inmoparadise.com/for-sale/

- Recorre el listado y su paginación (?pag=N).
- Extrae de cada ficha: referencia, precio actual, precio anterior,
  % de bajada, zona, tipo, habitaciones, banos, m2 y URL.
- Guarda un snapshot en data/snapshot_latest.json.
- Compara contra el snapshot anterior para detectar pisos nuevos y
  bajadas de precio, y anade esos eventos a data/pisos.json en el
  mismo formato que usa la lista "pisos" de la app Encaja, para que
  Encaja pueda leer ese JSON (via raw.githubusercontent.com) y
  importarlo sin necesitar ninguna API de storage.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SITE_ROOT = "https://www.inmoparadise.com/"
BASE_URL = SITE_ROOT + "for-sale/"
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
MAX_EVENTOS_GUARDADOS = 500

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "snapshot_latest.json"
PISOS_PATH = DATA_DIR / "pisos.json"


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


def parse_card(card) -> Listing | None:
    referencia = field_from_lista_datos(card, "Reference")
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
        habitaciones=field_from_lista_datos(card, "Bedrooms"),
        banos=field_from_lista_datos(card, "Bathrooms"),
        m2=field_from_lista_datos(card, "Surface"),
        url=url,
    )


def scrape_all() -> dict[str, Listing]:
    first_html = fetch_page(1)
    soup = BeautifulSoup(first_html, "html.parser")
    total_pages = get_total_pages(soup)

    listings: dict[str, Listing] = {}
    for card in soup.select("article.paginacion-ficha.propiedad"):
        item = parse_card(card)
        if item:
            listings[item.referencia] = item

    for pag in range(2, total_pages + 1):
        time.sleep(CRAWL_DELAY_SECONDS)
        html = fetch_page(pag)
        page_soup = BeautifulSoup(html, "html.parser")
        for card in page_soup.select("article.paginacion-ficha.propiedad"):
            item = parse_card(card)
            if item:
                listings[item.referencia] = item

    return listings


def load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return {"fecha": None, "listings": {}}
    with SNAPSHOT_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_pisos() -> list[dict]:
    if not PISOS_PATH.exists():
        return []
    with PISOS_PATH.open("r", encoding="utf-8") as f:
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


def piso_event(item: Listing, origen: str, precio_anterior: int | None = None) -> dict:
    extra = ""
    if origen == "bajada_precio" and precio_anterior:
        extra = f"bajada de {precio_anterior:,}€ a {item.precio:,}€".replace(",", ".")
        if item.bajada_pct:
            extra += f" ({item.bajada_pct})"

    return {
        "id": f"ip-{item.referencia}-{origen}-{item.precio}-{uuid.uuid4().hex[:6]}",
        "referencia": item.referencia,
        "zona": item.zona,
        "tipo": item.tipo,
        "precio": item.precio,
        "precio_anterior": precio_anterior,
        "bajada_pct": item.bajada_pct if origen == "bajada_precio" else None,
        "caract": build_caract(item, extra),
        "agente": AGENTE_BOT,
        "origen": origen,
        "url": item.url,
        "fecha": datetime.now(timezone.utc).isoformat(),
    }


def detect_changes(previous: dict[str, dict], current: dict[str, Listing]) -> list[dict]:
    events: list[dict] = []

    for referencia, item in current.items():
        prev = previous.get(referencia)
        if prev is None:
            events.append(piso_event(item, "nuevo"))
            continue

        prev_precio = prev.get("precio")
        if item.precio is not None and prev_precio is not None and item.precio < prev_precio:
            events.append(piso_event(item, "bajada_precio", precio_anterior=prev_precio))

    return events


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = load_snapshot()
    previous_listings: dict[str, dict] = snapshot.get("listings", {})

    current_listings = scrape_all()

    events = detect_changes(previous_listings, current_listings)

    if events:
        pisos = load_pisos()
        pisos = events + pisos  # los mas recientes primero
        pisos = pisos[:MAX_EVENTOS_GUARDADOS]
        with PISOS_PATH.open("w", encoding="utf-8") as f:
            json.dump(pisos, f, ensure_ascii=False, indent=2)
        print(f"{len(events)} cambio(s) detectado(s): "
              f"{sum(1 for e in events if e['origen'] == 'nuevo')} nuevos, "
              f"{sum(1 for e in events if e['origen'] == 'bajada_precio')} bajadas de precio.")
    else:
        print("Sin cambios respecto al snapshot anterior.")

    new_snapshot = {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "total_pisos": len(current_listings),
        "listings": {ref: asdict(item) for ref, item in current_listings.items()},
    }
    with SNAPSHOT_PATH.open("w", encoding="utf-8") as f:
        json.dump(new_snapshot, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
