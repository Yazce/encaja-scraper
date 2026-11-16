"""
Prueba del resumen de la mañana: calcula el resumen REAL (con los datos de
hoy) y lo manda SOLO a los dispositivos de Yacelly, a cualquier hora. No
marca ningún contacto como avisado ni cuenta como el resumen del día, así
que no cambia nada del funcionamiento normal.

No se ejecuta automaticamente - solo a mano, disparando el workflow
"Test daily summary push (Yacelly)" desde la pestana Actions de GitHub.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

import notify_daily as nd
import notify_inactivity as ni
import notify_push as np

YACELLY = "c45a217c-5588-4856-99e8-3407a13b2557"  # yacelly@inmoparadise.local (administradora)

SUPA_URL = os.environ["SUPABASE_URL"]
SUPA_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
VAPID_PRIVATE = os.environ["VAPID_PRIVATE_KEY"]

ahora = datetime.now(timezone.utc)
local = nd._madrid(ahora)
hoy = local.date().isoformat()

subs = np._supabase_get(
    SUPA_URL, SUPA_KEY, "push_subscriptions",
    {"select": "id,owner_id,endpoint,p256dh,auth", "owner_id": f"eq.{YACELLY}"},
)
print(f"{len(subs)} dispositivo(s) de Yacelly con avisos activados.")

compradores = nd._get_all(SUPA_URL, SUPA_KEY, "compradores", {"select": "*", "estado": "in.(activo,en_visita)"}, "id")
pisos = nd._get_all(SUPA_URL, SUPA_KEY, "pisos", {"select": "id,referencia,zona,tipo,precio,caract,reservado,fecha"}, "id")
contactados = {r["match_key"] for r in nd._get_all(SUPA_URL, SUPA_KEY, "contactados", {"select": "match_key"}, "match_key")}
try:
    alquiler = nd._get_all(SUPA_URL, SUPA_KEY, "alquiler_clientes", {"select": "*", "estado": "eq.activo"}, "id")
except Exception:
    alquiler = []

# Yacelly es administradora: su resumen es el de todo el equipo.
pend = [c for c in compradores if nd.toca_hoy(c, ahora, hoy)]
pend_alq = [c for c in alquiler if nd.toca_hoy(c, ahora, hoy)]
con_match = nd.compradores_con_coincidencias(compradores, pisos, contactados)

visitas_hoy: list[tuple[str, str]] = []
por_apuntar = 0
for c in compradores:
    v = nd._parse(c.get("visita_fecha"))
    if v is None:
        continue
    v_local = nd._madrid(v)
    if v_local.date().isoformat() == hoy:
        visitas_hoy.append((v_local.strftime("%H:%M"), c["nombre"]))
    elif v < ahora:
        por_apuntar += 1
visitas_hoy.sort()

print(f"Para contactar: {len(pend)} de compra/venta + {len(pend_alq)} de alquiler")
print(f"Clientes con coincidencias sin enviar: {len(con_match)}")
print(f"Visitas hoy: {len(visitas_hoy)} · por apuntar: {por_apuntar}")

cuerpo = nd.texto_resumen(len(pend) + len(pend_alq), len(con_match), visitas_hoy, por_apuntar)
if not cuerpo:
    cuerpo = "Hoy no tienes nada pendiente."
print("Texto del aviso:", cuerpo)

payload = json.dumps({
    "title": "Encaja — buenos días (ejemplo)",
    "body": cuerpo + " Toca para ver la lista.",
    "tag": "encaja-resumen-diario-prueba",
    "url": "/?tab=sincontacto",
}, ensure_ascii=False)

enviados = sum(1 for sub in subs if ni._enviar(sub, payload, VAPID_PRIVATE, SUPA_URL, SUPA_KEY))
print(f"{enviados} aviso(s) de ejemplo enviado(s) a Yacelly.")
