"""
Avisa por push a la comercial (y siempre también a las administradoras)
cuando lleva demasiados días sin contactar con uno de sus contactos activos.
Al tocar el aviso se abre Encaja, y desde ahí el botón "WhatsApp" deja el
mensaje ya redactado (nunca se manda solo, hace falta que la persona lo
confirme desde su móvil).

Cada cuánto se avisa depende de quién sea el contacto (umbral_dias):

- Clientes nuestros (compradores, "compra y vende", y vendedores marcados
  como "cliente nuestro" en su ficha): cada UMBRAL_DIAS_CLIENTE (5) días.
- Vendedores que todavía NO son clientes nuestros (propietarios a los que
  se está intentando captar): cada UMBRAL_DIAS_VENDEDOR_NO_CLIENTE (14)
  días, para no agobiarles.

Si en la ficha se puso a mano una "próxima fecha de contacto" que aún no ha
llegado, no se avisa hasta ese día (igual que la pestaña "Sin contacto" de
la web).

Los clientes de alquiler (tabla alquiler_clientes) se avisan también cada
UMBRAL_DIAS_CLIENTE días.

Solo mira los contactos con estado = 'activo': la bolsa de contactos
"Importados" (estado sin_revisar, sin repartir aún entre el equipo) queda
fuera a propósito, para no generar miles de avisos de golpe.

Para no repetir el mismo aviso en cada pasada del scraper, se guarda
ultimo_aviso_inactividad y solo se vuelve a avisar cuando ha pasado otra
vez el umbral de ese contacto desde el aviso anterior. Los recordatorios
solo salen entre las 9:00 y las 21:00 (hora de España).

Para no llenar el móvil de avisos, si a una misma persona le tocan más de
MAX_AVISOS_INDIVIDUALES contactos en la misma pasada, recibe UN solo aviso
resumen ("Tienes 12 contactos por contactar") que abre la pestaña "Sin
contacto", en vez de uno por cada contacto.

Depende de las mismas piezas que notify_push.py (SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY, VAPID_PRIVATE_KEY); si falta alguna, no hace
nada.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

import notify_push as np  # reutiliza _supabase_get, _delete_subscription, _zona_matches, _fmt_money, ADMIN_OWNER_IDS, VAPID_CLAIMS_SUB

REQUEST_TIMEOUT = 20
# Mismos valores que UMBRAL_DIAS_CLIENTE / UMBRAL_DIAS_VENDEDOR_NO_CLIENTE
# en index.html (pestaña "Sin contacto"): si se cambian, cambiarlos en los
# dos sitios.
UMBRAL_DIAS_CLIENTE = 5
UMBRAL_DIAS_VENDEDOR_NO_CLIENTE = 14
# A partir de cuántos contactos pendientes para una misma persona se manda
# un único aviso resumen en vez de uno por contacto.
MAX_AVISOS_INDIVIDUALES = 3
# Los recordatorios solo salen en horario de trabajo (hora de España): el
# scraper pasa también de madrugada y no tiene sentido que suene el móvil
# a las 5 de la mañana por algo que puede esperar a las 9.
HORA_INICIO_AVISOS = 9
HORA_FIN_AVISOS = 21
# Mismo tope que MAX_PISOS_POR_MENSAJE en index.html (el botón WhatsApp de
# la ficha usa esa constante en el cliente): así el mensaje de este script
# y el que de verdad se manda al tocar el botón coinciden en cuántos pisos
# como mucho se ofrecen.
MAX_PISOS_POR_AVISO = 5


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


def umbral_dias(c: dict) -> int:
    """Cada cuántos días toca avisar de este contacto (ver cabecera)."""
    if c.get("rol") == "vendedor" and not c.get("cliente_nuestro"):
        return UMBRAL_DIAS_VENDEDOR_NO_CLIENTE
    return UMBRAL_DIAS_CLIENTE


def dias_pendiente(c: dict, ahora: datetime) -> int | None:
    """Devuelve los días que lleva sin contacto si YA toca avisar de este
    contacto, o None si todavía no toca."""
    umbral = umbral_dias(c)
    dias_desde_aviso = _dias_desde(c.get("ultimo_aviso_inactividad"), ahora)
    if dias_desde_aviso is not None and dias_desde_aviso < umbral:
        return None  # ya se avisó hace poco, no repetir en cada pasada

    dias_sin_contacto = _dias_desde(c.get("ultimo_contacto") or c.get("fecha"), ahora)
    if dias_sin_contacto is None:
        return None

    proxima = c.get("proxima_contacto")  # fecha puesta a mano, "YYYY-MM-DD"
    if proxima:
        # Con fecha a mano manda esa fecha, no el umbral (igual que la web).
        if str(proxima)[:10] > ahora.date().isoformat():
            return None
        return max(int(dias_sin_contacto), 0)

    if dias_sin_contacto < umbral:
        return None
    return int(dias_sin_contacto)


def _enviar(sub: dict, payload: str, vapid_private: str, supa_url: str, supa_key: str) -> bool:
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=payload,
            vapid_private_key=vapid_private,
            vapid_claims={"sub": np.VAPID_CLAIMS_SUB},
            headers={"Urgency": "normal"},
            ttl=60 * 60 * 24,
        )
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            # La suscripción ya no existe: se borra para no reintentar.
            try:
                np._delete_subscription(supa_url, supa_key, sub["id"])
            except requests.RequestException:
                pass
        else:
            print(f"No se pudo mandar el aviso de inactividad: {e}")
        return False


def _payload_individual(c: dict, dias: int) -> str:
    # El aviso lleva a la ficha del contacto dentro de Encaja, no directo a
    # un enlace de WhatsApp: un enlace de WhatsApp abierto automáticamente
    # al pinchar el aviso no abre la app en Android. Dentro de la ficha hay
    # un botón "WhatsApp" que genera el mensaje (pregunta + pisos que
    # encajen si los hay) y, al tocarlo la persona, sí abre la app.
    es_alquiler = c.get("_tabla") == "alquiler_clientes"
    if es_alquiler:
        quien = f"{c['nombre']} (alquiler)"
    elif c.get("rol") == "vendedor" and not c.get("cliente_nuestro"):
        quien = f"{c['nombre']} (vendedor, aún no es cliente)"
    else:
        quien = c["nombre"]
    return json.dumps({
        "title": "Encaja — recordatorio",
        "body": f"Hace {dias} días que no contactas con {quien}. Toca para abrir su ficha y mandarle WhatsApp.",
        "tag": f"encaja-inactividad-{c['id']}",
        # Los de alquiler no tienen ficha en Compradores: se abre la lista
        # "Sin contacto", donde salen con su botón de WhatsApp.
        "url": "/?tab=sincontacto" if es_alquiler else f"/?comprador={c['id']}",
    }, ensure_ascii=False)


def _payload_resumen(pendientes: list[tuple[dict, int]]) -> str:
    nombres = [c["nombre"] for c, _ in pendientes[:3]]
    resto = len(pendientes) - len(nombres)
    lista = ", ".join(nombres) + (f" y {resto} más" if resto > 0 else "")
    return json.dumps({
        "title": f"Encaja — {len(pendientes)} contactos por contactar",
        "body": f"Llevan días sin noticias tuyas: {lista}. Toca para ver la lista.",
        "tag": "encaja-inactividad-resumen",
        "url": "/?tab=sincontacto",
    }, ensure_ascii=False)


def notify_stale_contacts() -> None:
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    if not supa_url or not supa_key or not vapid_private or webpush is None:
        return

    ahora = datetime.now(timezone.utc)
    try:
        hora_local = ahora.astimezone(ZoneInfo("Europe/Madrid")).hour
    except Exception:
        hora_local = (ahora.hour + 1) % 24  # sin base de zonas horarias: aproximación
    if not (HORA_INICIO_AVISOS <= hora_local < HORA_FIN_AVISOS):
        return  # fuera de horario: se avisará en la siguiente pasada de día

    try:
        # select=* a propósito: así, si alguna columna opcional
        # (cliente_nuestro, proxima_contacto) aún no existe en la base, la
        # consulta no falla — simplemente no viene y se trata como vacía.
        contactos = np._supabase_get(
            supa_url, supa_key, "compradores",
            {"select": "*", "estado": "eq.activo"},
        )
        subs = np._supabase_get(
            supa_url, supa_key, "push_subscriptions",
            {"select": "id,owner_id,endpoint,p256dh,auth"},
        )
    except requests.RequestException as e:
        print(f"No se pudieron leer los contactos activos para el aviso de inactividad: {e}")
        return

    # Clientes de alquiler: tabla aparte, mismo aviso. Si la tabla no existe
    # o falla, se sigue solo con compra/venta.
    try:
        alquiler = np._supabase_get(
            supa_url, supa_key, "alquiler_clientes",
            {"select": "*", "estado": "eq.activo"},
        )
    except requests.RequestException:
        alquiler = []
    for c in contactos:
        c["_tabla"] = "compradores"
    for c in alquiler:
        c["_tabla"] = "alquiler_clientes"
    contactos = contactos + alquiler

    if not contactos or not subs:
        return

    # Contactos de los que ya toca avisar, los que más tiempo llevan primero.
    pendientes: list[tuple[dict, int]] = []
    for c in contactos:
        dias = dias_pendiente(c, ahora)
        if dias is not None:
            pendientes.append((c, dias))
    if not pendientes:
        return
    pendientes.sort(key=lambda x: x[1], reverse=True)

    subs_por_owner: dict[str, list[dict]] = {}
    for s in subs:
        subs_por_owner.setdefault(s["owner_id"], []).append(s)

    avisados: set[tuple[str, str]] = set()
    for owner_id, subs_owner in subs_por_owner.items():
        # Cada persona recibe lo suyo; las administradoras, lo de todos.
        es_admin = owner_id in np.ADMIN_OWNER_IDS
        suyos = [(c, d) for c, d in pendientes if es_admin or c.get("owner_id") == owner_id]
        if not suyos:
            continue

        if len(suyos) > MAX_AVISOS_INDIVIDUALES:
            payloads = [_payload_resumen(suyos)]
        else:
            payloads = [_payload_individual(c, d) for c, d in suyos]

        llego = False
        for sub in subs_owner:
            for payload in payloads:
                if _enviar(sub, payload, vapid_private, supa_url, supa_key):
                    llego = True
        if llego:
            avisados.update((c["_tabla"], c["id"]) for c, _ in suyos)

    marca = ahora.isoformat()
    for tabla, cid in avisados:
        try:
            _supabase_patch(
                supa_url, supa_key, tabla,
                {"id": cid}, {"ultimo_aviso_inactividad": marca},
            )
        except requests.RequestException as e:
            print(f"No se pudo guardar ultimo_aviso_inactividad de {cid}: {e}")

    if avisados:
        print(f"Aviso de inactividad enviado para {len(avisados)} contacto(s).")
