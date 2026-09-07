"""
Avisa por push a la comercial (y siempre también a las administradoras)
cuando lleva UMBRAL_DIAS sin contactar con uno de sus clientes activos, con
un mensaje de WhatsApp ya redactado y listo para tocar "Enviar" (nunca se
manda solo, hace falta que la persona lo confirme desde su móvil).

Solo mira los contactos con estado = 'activo': la bolsa de contactos
"Importados" (estado sin_revisar, sin repartir aún entre el equipo) queda
fuera a propósito, para no generar miles de avisos de golpe.

Para no repetir el mismo aviso cada 6 horas (esta comprobación se ejecuta
en cada pasada del scraper), se guarda ultimo_aviso_inactividad y solo se
vuelve a avisar cuando ya han pasado otros UMBRAL_DIAS desde el aviso
anterior — es decir, un recordatorio cada 14 días mientras el contacto
siga sin moverse, no un aviso único.

Depende de las mismas piezas que notify_push.py (SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, VAPID_PRIVATE_KEY); si falta alguna, no hace
nada.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

import notify_push as np  # reutiliza _supabase_get, _delete_subscription, _zona_matches, _fmt_money, ADMIN_OWNER_IDS, VAPID_CLAIMS_SUB

REQUEST_TIMEOUT = 20
UMBRAL_DIAS = 14
MAX_PISOS_POR_AVISO = 3


def _supabase_patch(url: str, key: str, table: str, match: dict, data: dict) -> None:
    params = {k: f"eq.{v}" for k, v in match.items()}
    resp = requests.patch(
        f"{url.rstrip('/')}/rest/v1/{table}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        params=params,
        json=data,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()


def _dias_desde(iso: str | None, ahora: datetime) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (ahora - dt).total_seconds() / 86400


def _mensaje_comprador(nombre: str, dias: int, zona_busca: str | None, pisos: list[dict]) -> str:
    saludo = f"Hola {nombre}, soy de Inmoparadise 👋 Hace {dias} días que no hablamos, ¿sigues buscando piso?"
    if not pisos:
        return f"{saludo} Cuéntame cómo lo llevas, a ver si te puedo ayudar en algo."
    lineas = [f"{saludo} Mira, tengo estas viviendas que pueden encajarte:", ""]
    for i, p in enumerate(pisos[:MAX_PISOS_POR_AVISO], start=1):
        linea = f"{i}) {p.get('zona') or 'Sin zona'}"
        if p.get("tipo"):
            linea += f" · {p['tipo']}"
        linea += f" — {np._fmt_money(p.get('precio'))}"
        lineas.append(linea)
        if p.get("url"):
            lineas.append(p["url"])
        lineas.append("")
    lineas.append("¿Te sigue interesando alguna, o ha cambiado lo que buscas?")
    return "\n".join(lineas)


def _mensaje_vendedor(nombre: str, dias: int, zona: str | None) -> str:
    donde = f" en {zona}" if zona else ""
    return (
        f"Hola {nombre}, soy de Inmoparadise 👋 Hace {dias} días que no hablamos, "
        f"¿sigue en pie la venta de tu vivienda{donde}? Cuéntame cómo lo llevas."
    )


def notify_stale_contacts() -> None:
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    if not supa_url or not supa_key or not vapid_private or webpush is None:
        return

    ahora = datetime.now(timezone.utc)

    try:
        contactos = np._supabase_get(
            supa_url, supa_key, "compradores",
            {
                "select": "id,nombre,telefono,zona,tipo,presupuesto,rol,owner_id,fecha,ultimo_contacto,ultimo_aviso_inactividad",
                "estado": "eq.activo",
            },
        )
        subs = np._supabase_get(
            supa_url, supa_key, "push_subscriptions",
            {"select": "id,owner_id,endpoint,p256dh,auth"},
        )
    except requests.RequestException as e:
        print(f"No se pudieron leer los contactos activos para el aviso de inactividad: {e}")
        return

    if not contactos or not subs:
        return

    subs_por_owner: dict[str, list[dict]] = {}
    for s in subs:
        subs_por_owner.setdefault(s["owner_id"], []).append(s)

    # Pisos activos (no reservados), para poder ofrecer coincidencias a
    # quien busca comprar — misma idea que notify_push.py.
    try:
        pisos = np._supabase_get(
            supa_url, supa_key, "pisos",
            {"select": "id,zona,tipo,precio,caract,url,reservado", "reservado": "eq.false"},
        )
    except requests.RequestException:
        pisos = []

    avisados = 0
    for c in contactos:
        referencia = c.get("ultimo_contacto") or c.get("fecha")
        dias_sin_contacto = _dias_desde(referencia, ahora)
        if dias_sin_contacto is None or dias_sin_contacto < UMBRAL_DIAS:
            continue
        dias_desde_aviso = _dias_desde(c.get("ultimo_aviso_inactividad"), ahora)
        if dias_desde_aviso is not None and dias_desde_aviso < UMBRAL_DIAS:
            continue  # ya se avisó hace poco, no repetir cada 6 horas

        owner_ids = {c.get("owner_id"), *np.ADMIN_OWNER_IDS} - {None}
        destinatarios: dict[str, dict] = {}
        for oid in owner_ids:
            for sub in subs_por_owner.get(oid) or []:
                destinatarios[sub["id"]] = sub
        if not destinatarios:
            continue

        dias = int(dias_sin_contacto)
        if c.get("rol") == "vendedor":
            texto = _mensaje_vendedor(c["nombre"], dias, c.get("zona"))
        else:
            candidatos = [p for p in pisos if np._zona_matches(p.get("zona"), c.get("zona"))]
            if c.get("tipo"):
                candidatos = [p for p in candidatos if not p.get("tipo") or p["tipo"] == c["tipo"]]
            if c.get("presupuesto"):
                candidatos = [p for p in candidatos if not p.get("precio") or p["precio"] <= c["presupuesto"]]
            texto = _mensaje_comprador(c["nombre"], dias, c.get("zona"), candidatos)

        # El aviso lleva a la ficha del contacto dentro de Encaja (igual que
        # el aviso de coincidencia de piso), no directo a un enlace de
        # WhatsApp: un enlace de WhatsApp abierto automáticamente al pinchar
        # el aviso (sin que la persona lo toque ella misma dentro de una
        # página) no abre la app en Android, se queda en la web. Dentro de
        # la ficha hay un botón "WhatsApp" con el mensaje ya listo — al
        # tocarlo ella misma sí abre la app de verdad.
        payload = json.dumps({
            "title": "Encaja — recordatorio",
            "body": f"Hace {dias} días que no contactas con {c['nombre']}. Toca para abrir su ficha y mandarle WhatsApp.",
            "tag": f"encaja-inactividad-{c['id']}",
            "url": f"/?comprador={c['id']}",
        }, ensure_ascii=False)

        enviado_a_alguien = False
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
                    vapid_claims={"sub": np.VAPID_CLAIMS_SUB},
                    headers={"Urgency": "normal"},
                    ttl=60 * 60 * 24,
                )
                enviado_a_alguien = True
            except WebPushException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (404, 410):
                    try:
                        np._delete_subscription(supa_url, supa_key, sub["id"])
                    except requests.RequestException:
                        pass
                else:
                    print(f"No se pudo mandar el aviso de inactividad a {c['nombre']}: {e}")

        if enviado_a_alguien:
            avisados += 1
            try:
                _supabase_patch(
                    supa_url, supa_key, "compradores",
                    {"id": c["id"]}, {"ultimo_aviso_inactividad": ahora.isoformat()},
                )
            except requests.RequestException as e:
                print(f"No se pudo guardar ultimo_aviso_inactividad de {c['nombre']}: {e}")

    if avisados:
        print(f"{avisados} aviso(s) de inactividad enviado(s).")
