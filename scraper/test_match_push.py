"""
Prueba del flujo REAL de avisos (no el generico de test_push.py): simula que
ha entrado un piso nuevo que encaja con PEPITO y usa la MISMA logica de
coincidencia (zona/tipo/presupuesto) y el mismo envio que usa el scraper de
verdad cuando encuentra un piso nuevo. No crea ningun piso real en la base,
solo construye el "evento" en memoria.

Solo manda el aviso al dueno de PEPITO (no toca a ningun otro comprador),
para no molestar a nadie mas del equipo con esta prueba.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import notify_push as np  # reutiliza _supabase_get, _zona_matches, VAPID_CLAIMS_SUB

from pywebpush import webpush, WebPushException

SUPA_URL = os.environ["SUPABASE_URL"]
SUPA_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
VAPID_PRIVATE = os.environ["VAPID_PRIVATE_KEY"]

evento = {
    "id": "prueba-manual-1",
    "zona": "Alicante Centro",
    "tipo": "Piso",
    "precio": 275000,
    "reservado": False,
}
print("Evento de prueba (piso simulado):", evento)

compradores = np._supabase_get(
    SUPA_URL, SUPA_KEY, "compradores",
    {"select": "id,nombre,zona,tipo,presupuesto,rol,owner_id", "nombre": "eq.PEPITO"},
)
if not compradores:
    print("No se encontro a PEPITO en compradores.")
    sys.exit(0)

comprador = compradores[0]
print("Comprador:", comprador)

if not np._zona_matches(evento["zona"], comprador.get("zona")):
    print("La zona no encaja, no se manda nada.")
    sys.exit(0)
if comprador.get("tipo") and evento["tipo"] and comprador["tipo"] != evento["tipo"]:
    print("El tipo no encaja, no se manda nada.")
    sys.exit(0)
presupuesto = comprador.get("presupuesto")
if presupuesto and evento["precio"] and evento["precio"] > presupuesto:
    print("El precio no encaja, no se manda nada.")
    sys.exit(0)

print("Encaja. Buscando suscripciones del dueno de PEPITO...")
subs = np._supabase_get(
    SUPA_URL, SUPA_KEY, "push_subscriptions",
    {"select": "id,owner_id,endpoint,p256dh,auth", "owner_id": f"eq.{comprador['owner_id']}"},
)
print(f"{len(subs)} suscripcion(es) encontrada(s) para ese dueno.")

precio_fmt = f"{evento['precio']:,}".replace(",", ".") + " EUR"
payload = json.dumps({
    "title": "Encaja - nueva coincidencia",
    "body": f"{comprador['nombre']} encaja con {evento['zona']} - {evento['tipo']} - {precio_fmt}",
    "tag": f"encaja-match-{comprador['id']}-{evento['id']}",
    "url": "/",
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
        )
        print(f"OK: aviso enviado a suscripcion {sub['id']}")
        enviados += 1
    except WebPushException as e:
        print(f"ERROR mandando a {sub['id']}: {e}")

print(f"{enviados} aviso(s) de coincidencia real enviado(s).")
