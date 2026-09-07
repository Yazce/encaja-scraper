"""
Prueba puntual del aviso de inactividad: manda UN ejemplo, con datos
inventados (no toca ningun cliente real), solo a la cuenta de Yacelly, para
ver como queda el aviso y el mensaje de WhatsApp antes de esperar a que
salte de verdad con un cliente real.

No se ejecuta automaticamente - solo a mano, disparando el workflow
"Test inactivity push" desde la pestana Actions de GitHub.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(__file__))

import notify_inactivity as ni  # reutiliza _mensaje_comprador, _mensaje_vendedor
import notify_push as np  # reutiliza _supabase_get, VAPID_CLAIMS_SUB

from pywebpush import webpush, WebPushException

SUPA_URL = os.environ["SUPABASE_URL"]
SUPA_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
VAPID_PRIVATE = os.environ["VAPID_PRIVATE_KEY"]

YACELLY_OWNER_ID = "c45a217c-5588-4856-99e8-3407a13b2557"

# Datos de ejemplo (inventados, no son un cliente real) para ver el
# mensaje completo con pisos incluidos, tal y como le llegaria a una
# comercial de verdad.
PISOS_EJEMPLO = [
    {"zona": "Babel", "tipo": "Piso", "precio": 145000, "url": "https://www.inmoparadise.com/ficha/ejemplo-1"},
    {"zona": "Ciudad Elegida", "tipo": "Piso", "precio": 155000, "url": "https://www.inmoparadise.com/ficha/ejemplo-2"},
]

texto = ni._mensaje_comprador("Cliente de Ejemplo", 15, "Babel", PISOS_EJEMPLO)
# Numero personal de Yaz, solo para esta prueba: asi al tocar "Enviar"
# se ve el mensaje real en WhatsApp sin arriesgarse a escribirle a un
# cliente de verdad.
wa_url = f"https://wa.me/34684139915?text={quote(texto)}"

print("Mensaje de ejemplo que se mandaria por WhatsApp:\n")
print(texto)
print(f"\nEnlace de WhatsApp: {wa_url}")

subs = np._supabase_get(
    SUPA_URL, SUPA_KEY, "push_subscriptions",
    {"select": "id,owner_id,endpoint,p256dh,auth", "owner_id": f"eq.{YACELLY_OWNER_ID}"},
)
print(f"\n{len(subs)} suscripcion(es) de Yacelly encontrada(s).")

payload = json.dumps({
    "title": "Encaja — recordatorio (EJEMPLO de prueba)",
    "body": "Hace 15 días que no contactas con Cliente de Ejemplo",
    "tag": "encaja-inactividad-ejemplo",
    "url": wa_url,
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
