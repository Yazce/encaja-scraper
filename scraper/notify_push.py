"""
Manda avisos push reales (Web Push) a los agentes de Encaja cuando un piso
nuevo o una bajada de precio detectados por el scraper encajan con lo que
busca alguno de sus compradores. A diferencia del aviso del navegador
(que solo suena si la pestaña de Encaja está abierta), este llega aunque
el móvil esté bloqueado o la app cerrada, porque lo manda este mismo
proceso directamente al servicio de notificaciones del sistema operativo.

Depende de:
- la tabla push_subscriptions de Supabase (la suscripción de cada
  dispositivo, guardada por la propia app cuando alguien pulsa "Activar
  avisos")
- el secreto VAPID_PRIVATE_KEY de este repositorio en GitHub Actions

Si falta cualquiera de esas piezas (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
o VAPID_PRIVATE_KEY), esta función no hace nada — no rompe el resto del
scraper.

La lógica de "encaja o no encaja" (zona / tipo / presupuesto) es la misma
que usa la web en computeMatches(), para que un piso que aparecería como
coincidencia en Encaja sea también el que dispara el aviso aquí.

Además del dueño del comprador, SIEMPRE se avisa también a las cuentas
administradoras (yacelly y Giga), para que puedan supervisar que todo se
gestiona y detectar problemas, aunque el comprador no sea suyo.
"""

from __future__ import annotations

import json
import os
import unicodedata

import requests

try:
    from pywebpush import webpush, WebPushException
except ImportError:  # por si se ejecuta en un entorno sin la dependencia
    webpush = None
    WebPushException = Exception

VAPID_CLAIMS_SUB = "mailto:encaja@inmoparadise.com"
REQUEST_TIMEOUT = 20

# Cuentas administradoras: reciben SIEMPRE el aviso de cualquier
# coincidencia, sea o no suyo el comprador, para poder supervisar que se
# gestiona todo correctamente.
ADMIN_OWNER_IDS = {
    "c45a217c-5588-4856-99e8-3407a13b2557",  # yacelly@inmoparadise.local
    "8df7c3b7-e0f1-4b08-ba07-5063d03fba58",  # gigagiga717@gmail.com (Giga)
}


def _norm(s: str | None) -> str:
    s = (s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _zona_matches(piso_zona: str | None, comprador_zona: str | None) -> bool:
    nb = _norm(comprador_zona)
    if not nb or nb == "alicante":
        return True  # sin zona o "alicante" = cualquier barrio
    a = _norm(piso_zona)
    if not a:
        return False
    if a == nb or nb in a or a in nb:
        return True
    wa = [w for w in a.split() if len(w) > 3]
    wb = [w for w in nb.split() if len(w) > 3]
    return any(w in wb for w in wa)


def _supabase_get(url: str, key: str, table: str, params: dict) -> list[dict]:
    resp = requests.get(
        f"{url.rstrip('/')}/rest/v1/{table}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _delete_subscription(url: str, key: str, sub_id: str) -> None:
    requests.delete(
        f"{url.rstrip('/')}/rest/v1/push_subscriptions",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"id": f"eq.{sub_id}"},
        timeout=REQUEST_TIMEOUT,
    )


def _fmt_money(n) -> str:
    return f"{n:,}".replace(",", ".") + " €" if n is not None else "—"


def notify_matching_compradores(events: list[dict]) -> None:
    if not events or webpush is None:
        return

    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    if not supa_url or not supa_key or not vapid_private:
        return

    # Un piso que ya nace reservado no se ofrece a nadie (igual que en la web).
    pisos_a_avisar = [e for e in events if not e.get("reservado")]
    if not pisos_a_avisar:
        return

    try:
        compradores = _supabase_get(
            supa_url, supa_key, "compradores",
            {"select": "id,nombre,zona,tipo,presupuesto,rol,owner_id", "rol": "neq.vendedor"},
        )
        subs = _supabase_get(
            supa_url, supa_key, "push_subscriptions",
            {"select": "id,owner_id,endpoint,p256dh,auth"},
        )
    except requests.RequestException as e:
        print(f"No se pudieron leer compradores/suscripciones para los avisos push: {e}")
        return

    if not compradores or not subs:
        return

    subs_por_owner: dict[str, list[dict]] = {}
    for s in subs:
        subs_por_owner.setdefault(s["owner_id"], []).append(s)

    enviados = 0
    for event in pisos_a_avisar:
        zona_evento = event.get("zona") or "Sin zona"
        tipo_evento = event.get("tipo") or ""
        precio_evento = event.get("precio")

        for comprador in compradores:
            if not _zona_matches(event.get("zona"), comprador.get("zona")):
                continue
            if comprador.get("tipo") and tipo_evento and comprador["tipo"] != tipo_evento:
                continue
            presupuesto = comprador.get("presupuesto")
            if presupuesto and precio_evento and precio_evento > presupuesto:
                continue

            # El dueño del comprador + las cuentas administradoras (sin
            # repetir suscripciones si el dueño ya es una de ellas).
            owner_ids = {comprador.get("owner_id"), *ADMIN_OWNER_IDS} - {None}
            destinatarios: dict[str, dict] = {}
            for oid in owner_ids:
                for sub in subs_por_owner.get(oid) or []:
                    destinatarios[sub["id"]] = sub
            if not destinatarios:
                continue

            payload = json.dumps({
                "title": "Encaja — nueva coincidencia",
                "body": (
                    f"{comprador['nombre']} encaja con {zona_evento}"
                    f"{(' · ' + tipo_evento) if tipo_evento else ''} · {_fmt_money(precio_evento)}"
                ),
                "tag": f"encaja-match-{comprador['id']}-{event['id']}",
                "url": "/",
            }, ensure_ascii=False)

            for sub in destinatarios.values():
                subscription_info = {
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                }
                try:
                    webpush(
                        subscription_info=subscription_info,
                        data=payload,
                        vapid_private_key=vapid_private,
                        vapid_claims={"sub": VAPID_CLAIMS_SUB},
                    )
                    enviados += 1
                except WebPushException as e:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status in (404, 410):
                        # La suscripción ya no existe (se desinstaló la app o
                        # el navegador la revocó): la borramos para no
                        # reintentar en vano cada vez.
                        try:
                            _delete_subscription(supa_url, supa_key, sub["id"])
                        except requests.RequestException:
                            pass
                    else:
                        print(f"No se pudo mandar el aviso push a {comprador['nombre']}: {e}")

    if enviados:
        print(f"{enviados} aviso(s) push enviado(s).")
