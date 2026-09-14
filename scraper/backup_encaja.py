"""
Copia de seguridad semanal de la base de datos de Encaja.

Exporta TODAS las filas de las 8 tablas del proyecto a un único archivo JSON
con fecha, usando la service_role key (que salta la RLS a propósito, porque
esto se ejecuta solo dentro de GitHub Actions, nunca en el navegador).

El archivo resultante se sube como "artifact" de la propia ejecución de
GitHub Actions (no se sube al repositorio, que es público) y queda
disponible para descargar desde la pestaña "Actions" durante 90 días.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TABLES = [
    "compradores",
    "contactados",
    "pisos",
    "perfiles",
    "alquiler_clientes",
    "alquiler_contactados",
    "alquiler_pisos",
    "push_subscriptions",
]

HEADERS = {
    "apikey": SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
}

PAGE_SIZE = 1000


def fetch_table(table: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        headers = dict(HEADERS)
        headers["Range-Unit"] = "items"
        headers["Range"] = f"{offset}-{offset + PAGE_SIZE - 1}"
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params={"select": "*", "order": "id"},
            timeout=60,
        )
        if resp.status_code not in (200, 206):
            raise RuntimeError(
                f"Error exportando {table}: HTTP {resp.status_code} — {resp.text[:300]}"
            )
        page = resp.json()
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def main() -> None:
    backup = {
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "proyecto": SUPABASE_URL,
        "tablas": {},
    }

    total_filas = 0
    for table in TABLES:
        try:
            rows = fetch_table(table)
        except Exception as exc:  # noqa: BLE001
            print(f"AVISO: no se pudo exportar '{table}': {exc}", file=sys.stderr)
            rows = []
        backup["tablas"][table] = rows
        total_filas += len(rows)
        print(f"  {table}: {len(rows)} filas")

    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = "backup_output"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"encaja_backup_{fecha}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)

    print(f"\nCopia de seguridad generada: {out_path} ({total_filas} filas en total)")


if __name__ == "__main__":
    main()
