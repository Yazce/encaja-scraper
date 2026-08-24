# encaja-scraper

Vigila https://www.inmoparadise.com/for-sale/ cada 6 horas y detecta:

- **pisos nuevos** (referencia que no estaba en el snapshot anterior)
- **bajadas de precio** (misma referencia, precio menor que la última vez)

## Cómo funciona

1. `scraper/scrape_inmoparadise.py` recorre el listado y su paginación
   (`?pag=N`), respetando el `Crawl-delay: 6` de `robots.txt`.
2. Guarda el estado completo actual en `data/snapshot_latest.json`
   (uso interno, solo para comparar en la siguiente ejecución — no lo
   lee ninguna otra app).
3. Compara contra el snapshot anterior y, por cada piso nuevo o
   bajada de precio detectada:
   - inserta una fila directamente en la tabla `pisos` de Supabase
     (la misma base de datos que usa la web de Encaja en
     [`encaja-web`](https://github.com/Yazce/encaja-web)), usando
     `SUPABASE_URL` y `SUPABASE_SERVICE_ROLE_KEY`.
   - crea un GitHub Issue en este repositorio (usando el
     `GITHUB_TOKEN` que ya provee el workflow, sin secretos
     adicionales):
     - `[Piso nuevo] Zona - Precio`
     - `[Bajada] Zona - Precio anterior -> Precio nuevo`

   Si `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` o
   `GITHUB_TOKEN`/`GITHUB_REPOSITORY` no están en el entorno (por
   ejemplo, en una ejecución local sin configurar), ese paso se omite
   sin fallar el resto.
4. El workflow de GitHub Actions (`.github/workflows/scrape.yml`) lo
   ejecuta cada 6 horas, con permiso `issues: write`, y commitea
   `data/snapshot_latest.json` si cambió.

## Configurar los secretos de Supabase

En este repo de GitHub → **Settings** → **Secrets and variables** →
**Actions** → **New repository secret**, añade:

- `SUPABASE_URL` → el Project URL de tu proyecto de Supabase
  (Project Settings → API).
- `SUPABASE_SERVICE_ROLE_KEY` → la clave **service_role** (no la
  `anon`) del mismo sitio. Esta clave sí es sensible — solo debe
  vivir aquí, como secreto de GitHub Actions, nunca en el código del
  frontend.

`GITHUB_TOKEN` no hace falta configurarlo: lo genera GitHub
automáticamente en cada ejecución del workflow.

## Uso local

```bash
pip install -r requirements.txt
python scraper/scrape_inmoparadise.py
```

Sin `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` en el entorno, la
ejecución local sigue funcionando (actualiza el snapshot local y
solo omite la escritura en Supabase y la creación de issues).

## Publicar en GitHub y activar el cron

```bash
git init
git add .
git commit -m "Scraper inicial de Inmoparadise"
git branch -M main
git remote add origin https://github.com/<usuario>/<repo>.git
git push -u origin main
```

En GitHub, en la pestaña **Actions** del repo, comprueba que el
workflow está habilitado (a veces hay que activarlo con un clic la
primera vez en repos nuevos). A partir de ahí se ejecuta solo cada
6 horas; también se puede lanzar a mano desde Actions → Scrape
Inmoparadise → Run workflow.
