"""
Prueba del aviso de inactividad usando el flujo REAL (no un texto de
ejemplo): usa a PEPITO -su telefono es el personal de Yacelly, para poder
tocar "WhatsApp" sin arriesgarse a escribirle a un cliente de verdad- y
calcula los dias sin contacto igual que hace notify_inactivity.py de
verdad, a partir de ultimo_contacto/fecha guardados en la base (PEPITO
esta puesto como si llevara ~15 dias sin contacto, para simular el aviso
de "dos semanas sin hablar"). El mensaje que se manda -y el que generara
tambien el boton "WhatsApp" de su ficha- es el mismo que construye la
funcion real _mensaje_comprador/_mensaje_vendedor, con los pisos que le
encajen incluidos si los hay.

No se ejecuta automaticamente - solo a mano, disparando el workflow
"Test inactivity push" desde la pestana Actions de GitHub.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

import notify_inactivity as ni  # reutiliza _mensaje_comprador, _mensaje_vendedor, _dias_desde
import notify_push as np  # reutiliza _supabase_get, _zona_matches, ADMIN_OWNER_IDS, VAPID_CLAIMS_SUB

from pywebpush import webpush, WebPushException

SUPA_URL = os.environ["SUPABASE_URL"]
SUPA_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
VAPID_PRIVATE = os.environ["VAPID_PRIVATE_KEY"]

compradores = np._supabase_get(
    SUPA_URL, SUPA_KEY, "compradores",
    {
        "select": "id,nombre,telefono,zona,tipo,presupuesto,rol,owner_id,fecha,ultimo_contacto,ultimo_aviso_inactividad",
        "nombre": "eq.PEPITO",
    },
)
if not compradores:
    print("No se encontro a PEPITO en compradores.")
    sys.exit(0)

c = compradores[0]
print("Comprador:", c)

ahora = datetime.now(timezone.utc)
referencia = c.get("ultimo_contacto") or c.get("fecha")
dias_sin_contacto = ni._dias_desde(referencia, ahora)
if dias_sin_contacto is None:
    print("PEPITO no tiene fecha de contacto, no se puede calcular la inactividad.")
    sys.exit(0)

dias = int(dias_sin_contacto)
print(f"Dias sin contacto (simulados en la base): {dias}")

# Pisos activos, igual que en el aviso real, para poder ofrecer coincidencias.
try:
    pisos = np._supabase_get(
        SUPA_URL, SUPA_KEY, "pisos",
        {"select": "id,zona,tipo,precio,caract,url,reservado", "reservado": "eq.false"},
    )
except Exception:
    pisos = []

if c.get("rol") == "vendedor":
    texto = ni._mensaje_vendedor(c["nombre"], dias, c.get("zona"))
else:
    candidatos = [p for p in pisos if np._zona_matches(p.get("zona"), c.get("zona"))]
    if c.get("tipo"):
        candidatos = [p for p in candidatos if not p.get("tipo") or p["tipo"] == c["tipo"]]
    if c.get("presupuesto"):
        candidatos = [p for p in candidatos if not p.get("precio") or p["precio"] <= c["presupuesto"]]
    texto = ni._mensaje_comprador(c["nombre"], dias, c.get("zona"), candidatos)

print("\nMensaje que se generaria (el mismo que usa el boton WhatsApp de su ficha):\n")
print(texto)

print("\nBuscando suscripciones del dueno de PEPITO + administradoras (igual que el aviso real)...")
owner_ids = {c.get("owner_id"), *np.ADMIN_OWNER_IDS} - {None}
subs_por_id: dict[str, dict] = {}
for oid in owner_ids:
    encontradas = np._supabase_get(
        SUPA_URL, SUPA_KEY, "push_subscriptions",
        {"select": "id,owner_id,endpoint,p256dh,auth", "owner_id": f"eq.{oid}"},
    )
    for s in encontradas:
        subs_por_id[s["id"]] = s
subs = list(subs_por_id.values())
print(f"{len(subs)} suscripcion(es) encontrada(s).")

payload = json.dumps({
    "title": "Encaja — recordatorio",
    "body": f"Hace {dias} días que no contactas con {c['nombre']}. Toca para abrir su ficha y mandarle WhatsApp.",
    "tag": f"encaja-inactividad-{c['id']}",
    "url": f"/?comprador={c['id']}",
}, ensure_ascii=False)

enviados = 0
for sub in subs:
    subscription_info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": np.VAPID_CLAIMS_SUB},
            headers={"Urgency": "high"},
            ttl=60 * 60 * 24,
        )
        print(f"OK: aviso de inactividad (real, con PEPITO) enviado a suscripcion {sub['id']}")
        enviados += 1
    except WebPushException as e:
        print(f"ERROR mandando a {sub['id']}: {e}")

print(f"\n{enviados} aviso(s) de inactividad enviado(s) usando el flujo real con PEPITO.")
