"""
Prueba puntual del aviso de inactividad: manda UN aviso de ejemplo, solo a
la cuenta de Yacelly, para comprobar que funciona el camino completo: el
aviso lleva a la ficha de un contacto real dentro de Encaja (no manda
ningun WhatsApp por su cuenta - eso solo pasa si ella misma toca el boton
"WhatsApp" ya dentro de la ficha).

Usa el primer contacto real que tenga Yacelly (no modifica nada suyo, solo
lee su nombre e id para construir el enlace). No se ejecuta
automaticamente - solo a mano, disparando el workflow "Test inactivity
push" desde la pestana Actions de GitHub.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import notify_push as np  # reutiliza _supabase_get, VAPID_CLAIMS_SUB

from pywebpush import webpush, WebPushException

SUPA_URL = os.environ["SUPABASE_URL"]
SUPA_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
VAPID_PRIVATE = os.environ["VAPID_PRIVATE_KEY"]

YACELLY_OWNER_ID = "c45a217c-5588-4856-99e8-3407a13b2557"

compradores = np._supabase_get(
    SUPA_URL, SUPA_KEY, "compradores",
    {"select": "id,nombre", "owner_id": f"eq.{YACELLY_OWNER_ID}", "limit": "1"},
)
if not compradores:
    print("Yacelly todavia no tiene ningun contacto propio, no se puede probar el enlace a una ficha real.")
    sys.exit(0)

c = compradores[0]
print(f"Usando el contacto real '{c['nombre']}' (id {c['id']}) solo para construir el enlace de prueba.")
print("Esto NO le manda nada: el aviso solo abre su ficha en Encaja, y ahi hay que tocar el boton WhatsApp a mano.")

subs = np._supabase_get(
    SUPA_URL, SUPA_KEY, "push_subscriptions",
    {"select": "id,owner_id,endpoint,p256dh,auth", "owner_id": f"eq.{YACELLY_OWNER_ID}"},
)
print(f"\n{len(subs)} suscripcion(es) de Yacelly encontrada(s).")

payload = json.dumps({
    "title": "Encaja — recordatorio (EJEMPLO de prueba)",
    "body": f"Hace 15 días que no contactas con {c['nombre']}. Toca para abrir su ficha y mandarle WhatsApp.",
    "tag": "encaja-inactividad-ejemplo",
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
        print(f"OK: aviso de ejemplo enviado a suscripcion {sub['id']}")
        enviados += 1
    except WebPushException as e:
        print(f"ERROR mandando a {sub['id']}: {e}")

print(f"\n{enviados} aviso(s) de ejemplo enviado(s) a Yacelly.")
