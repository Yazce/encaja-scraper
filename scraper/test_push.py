"""
Script de prueba puntual: manda un aviso push a TODAS las suscripciones
guardadas en push_subscriptions, para comprobar de principio a fin que el
envío funciona (incluye probar que llega aunque el móvil esté bloqueado).

No se ejecuta automáticamente — solo a mano, disparando el workflow
"Test push notification" desde la pestaña Actions de GitHub. No forma
parte del flujo normal del scraper (esa lógica vive en notify_push.py).
"""

from __future__ import annotations

import json
import os

import requests
from pywebpush import webpush, WebPushException


def main() -> None:
    supa_url = os.environ["SUPABASE_URL"]
    supa_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    vapid_private_key = os.environ["VAPID_PRIVATE_KEY"]

    resp = requests.get(
        f"{supa_url.rstrip('/')}/rest/v1/push_subscriptions",
        headers={"apikey": supa_key, "Authorization": f"Bearer {supa_key}"},
        params={"select": "id,endpoint,p256dh,auth"},
        timeout=20,
    )
    resp.raise_for_status()
    subs = resp.json()
    print(f"{len(subs)} suscripcion(es) encontradas.")

    payload = json.dumps(
        {
            "title": "Encaja — prueba de aviso",
            "body": "Esto es una prueba: si te llega con el móvil bloqueado, ¡ya funciona de verdad!",
            "tag": "encaja-test-push",
            "url": "/",
        },
        ensure_ascii=False,
    )

    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims={"sub": "mailto:encaja@inmoparadise.com"},
            )
            print(f"OK: aviso enviado a suscripcion {sub['id']}")
        except WebPushException as e:
            print(f"ERROR mandando a {sub['id']}: {e}")
            if getattr(e, "response", None) is not None:
                print(f"  status: {e.response.status_code} body: {e.response.text}")


if __name__ == "__main__":
    main()
