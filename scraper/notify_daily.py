"""
Resumen de la mañana: un único aviso push al día por persona, a primera
hora, con lo que tiene pendiente:

- cuántos contactos le toca contactar hoy (compra/venta y alquiler, con los
  mismos umbrales de 5 / 14 días que el recordatorio de inactividad),
- cuántos clientes tienen coincidencias de pisos sin enviar,
- las visitas de hoy y las visitas ya hechas que faltan por apuntar.

Las administradoras reciben el resumen de todo el equipo.

Se manda en la primera pasada del scraper entre las HORA_INICIO y las
HORA_FIN (hora de España) y solo una vez al día: la fecha del último
resumen se guarda en data/snapshot_latest.json ("resumen_diario").

Los contactos incluidos en el resumen quedan marcados como "ya avisados"
(ultimo_aviso_inactividad), para que el recordatorio de inactividad no
repita lo mismo unos minutos después.

Depende de las mismas piezas que el resto de avisos (SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, VAPID_PRIVATE_KEY); si falta alguna, no hace nada.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import notify_inactivity as ni
import notify_push as np

HORA_INICIO = 9
HORA_FIN = 14
# Mismo criterio que PRESUPUESTO_MIN_PCT en index.html.
PRESUPUESTO_MIN_PCT = 0.6
PAGINA = 1000


def _madrid(dt: datetime) -> datetime:
    try:
        return dt.astimezone(ZoneInfo("Europe/Madrid"))
    except Exception:
        return dt + timedelta(hours=1)  # sin base de zonas horarias: aproximación


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_all(url: str, key: str, table: str, params: dict, order: str) -> list[dict]:
    """Como np._supabase_get pero paginando: Supabase corta en 1000 filas."""
    filas: list[dict] = []
    offset = 0
    while True:
        lote = np._supabase_get(
            url, key, table,
            {**params, "order": order, "limit": PAGINA, "offset": offset},
        )
        filas.extend(lote)
        if len(lote) < PAGINA:
            return filas
        offset += PAGINA


def _habitaciones(piso: dict) -> int | None:
    m = re.search(r"(\d+)\s*hab", piso.get("caract") or "", re.I)
    return int(m.group(1)) if m else None


def pisos_vigentes(pisos: list[dict]) -> list[dict]:
    """Cuando un piso baja de precio hay dos filas con la misma referencia:
    solo cuenta la más reciente (igual que pisosVigentes() en index.html)."""
    ultima: dict[str, dict] = {}
    for p in pisos:
        ref = p.get("referencia")
        if not ref:
            continue
        u = ultima.get(ref)
        if u is None or (p.get("fecha") or "") > (u.get("fecha") or ""):
            ultima[ref] = p
    return [p for p in pisos if not p.get("referencia") or ultima.get(p["referencia"]) is p]


def compradores_con_coincidencias(compradores: list[dict], pisos: list[dict], contactados: set[str]) -> set[str]:
    """Ids de compradores con al menos una coincidencia sin enviar (misma
    lógica que computeMatches() en index.html)."""
    con: set[str] = set()
    vigentes = [p for p in pisos_vigentes(pisos) if not p.get("reservado")]
    for c in compradores:
        if c.get("rol") == "vendedor":
            continue
        if (c.get("estado") or "activo") in ("descartado", "cerrado", "sin_revisar"):
            continue
        for p in vigentes:
            if f"{c['id']}::{p['id']}" in contactados:
                continue
            if not np._zona_matches(p.get("zona"), c.get("zona")):
                continue
            if c.get("tipo") and p.get("tipo") and c["tipo"] != p["tipo"]:
                continue
            presupuesto, precio = c.get("presupuesto"), p.get("precio")
            if presupuesto and precio and (precio > presupuesto or precio < presupuesto * PRESUPUESTO_MIN_PCT):
                continue
            if c.get("habitaciones_min"):
                hab = _habitaciones(p)
                if hab is not None and hab < c["habitaciones_min"]:
                    continue
            con.add(c["id"])
            break
    return con


def toca_hoy(c: dict, ahora: datetime, hoy: str) -> bool:
    """¿Aparece hoy en la pestaña "Sin contacto"? (estaParaContactarHoy en la web)"""
    if (c.get("estado") or "activo") != "activo":
        return False
    proxima = c.get("proxima_contacto")
    if proxima:
        return str(proxima)[:10] <= hoy
    dias = ni._dias_desde(c.get("ultimo_contacto") or c.get("fecha"), ahora)
    return dias is not None and dias >= ni.umbral_dias(c)


def texto_resumen(pendientes: int, con_coincidencias: int, visitas_hoy: list[tuple[str, str]], por_apuntar: int) -> str | None:
    partes: list[str] = []
    if pendientes:
        partes.append(f"{pendientes} para contactar")
    if con_coincidencias:
        partes.append(
            "1 cliente con coincidencias sin enviar" if con_coincidencias == 1
            else f"{con_coincidencias} clientes con coincidencias sin enviar"
        )
    if len(visitas_hoy) == 1:
        hora, nombre = visitas_hoy[0]
        partes.append(f"visita a las {hora} con {nombre}")
    elif visitas_hoy:
        partes.append(f"{len(visitas_hoy)} visitas hoy")
    if por_apuntar:
        partes.append("1 visita por apuntar" if por_apuntar == 1 else f"{por_apuntar} visitas por apuntar")
    if not partes:
        return None
    return "Hoy: " + " · ".join(partes) + "."


def _marcar_avisados(url: str, key: str, table: str, ids: list[str], marca: str) -> None:
    for i in range(0, len(ids), 50):
        lote = ids[i:i + 50]
        lista = ",".join('"' + x.replace('"', "") + '"' for x in lote)
        try:
            resp = requests.patch(
                f"{url.rstrip('/')}/rest/v1/{table}",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                params={"id": f"in.({lista})"},
                json={"ultimo_aviso_inactividad": marca},
                timeout=ni.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"No se pudo marcar como avisados en {table}: {e}")


def notify_daily_summary(ultimo_resumen: str | None) -> str | None:
    """Manda el resumen si toca. Devuelve la fecha de hoy (YYYY-MM-DD) si se
    ha hecho el resumen de hoy en esta pasada, o None si no tocaba."""
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    if not supa_url or not supa_key or not vapid_private or ni.webpush is None:
        return None

    ahora = datetime.now(timezone.utc)
    local = _madrid(ahora)
    hoy = local.date().isoformat()
    if ultimo_resumen == hoy or not (HORA_INICIO <= local.hour < HORA_FIN):
        return None

    try:
        subs = np._supabase_get(
            supa_url, supa_key, "push_subscriptions",
            {"select": "id,owner_id,endpoint,p256dh,auth"},
        )
        compradores = _get_all(
            supa_url, supa_key, "compradores",
            {"select": "*", "estado": "in.(activo,en_visita)"}, "id",
        )
        pisos = _get_all(supa_url, supa_key, "pisos", {"select": "id,referencia,zona,tipo,precio,caract,reservado,fecha"}, "id")
        contactados = {r["match_key"] for r in _get_all(supa_url, supa_key, "contactados", {"select": "match_key"}, "match_key")}
    except requests.RequestException as e:
        print(f"No se pudieron leer los datos para el resumen de la mañana: {e}")
        return None
    try:
        alquiler = _get_all(supa_url, supa_key, "alquiler_clientes", {"select": "*", "estado": "eq.activo"}, "id")
    except requests.RequestException:
        alquiler = []

    if not subs:
        return hoy

    con_match = compradores_con_coincidencias(compradores, pisos, contactados)

    subs_por_owner: dict[str, list[dict]] = {}
    for s in subs:
        subs_por_owner.setdefault(s["owner_id"], []).append(s)

    avisados_venta: set[str] = set()
    avisados_alquiler: set[str] = set()
    enviados = 0
    for owner_id, subs_owner in subs_por_owner.items():
        es_admin = owner_id in np.ADMIN_OWNER_IDS
        mios = [c for c in compradores if es_admin or c.get("owner_id") == owner_id]
        mios_alq = [c for c in alquiler if es_admin or c.get("owner_id") == owner_id]

        pend = [c for c in mios if toca_hoy(c, ahora, hoy)]
        pend_alq = [c for c in mios_alq if toca_hoy(c, ahora, hoy)]
        n_match = sum(1 for c in mios if c["id"] in con_match)

        visitas_hoy: list[tuple[str, str]] = []
        por_apuntar = 0
        for c in mios:
            v = _parse(c.get("visita_fecha"))
            if v is None:
                continue
            v_local = _madrid(v)
            if v_local.date().isoformat() == hoy:
                visitas_hoy.append((v_local.strftime("%H:%M"), c["nombre"]))
            elif v < ahora:
                por_apuntar += 1
        visitas_hoy.sort()

        cuerpo = texto_resumen(len(pend) + len(pend_alq), n_match, visitas_hoy, por_apuntar)
        if not cuerpo:
            continue
        payload = json.dumps({
            "title": "Encaja — buenos días",
            "body": cuerpo + " Toca para ver la lista.",
            "tag": "encaja-resumen-diario",
            "url": "/?tab=sincontacto",
        }, ensure_ascii=False)

        llego = False
        for sub in subs_owner:
            if ni._enviar(sub, payload, vapid_private, supa_url, supa_key):
                llego = True
        if llego:
            enviados += 1
            avisados_venta.update(c["id"] for c in pend)
            avisados_alquiler.update(c["id"] for c in pend_alq)

    marca = ahora.isoformat()
    _marcar_avisados(supa_url, supa_key, "compradores", sorted(avisados_venta), marca)
    _marcar_avisados(supa_url, supa_key, "alquiler_clientes", sorted(avisados_alquiler), marca)

    print(f"Resumen de la mañana enviado a {enviados} persona(s).")
    return hoy
