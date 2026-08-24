# encaja-scraper

Vigila https://www.inmoparadise.com/for-sale/ cada 6 horas y detecta:

- **pisos nuevos** (referencia que no estaba en el snapshot anterior)
- **bajadas de precio** (misma referencia, precio menor que la última vez)

## Cómo funciona

1. `scraper/scrape_inmoparadise.py` recorre el listado y su paginación
   (`?pag=N`), respetando el `Crawl-delay: 6` de `robots.txt`.
2. Guarda el estado completo actual en `data/snapshot_latest.json`
   (usado solo para comparar en la siguiente ejecución).
3. Compara contra el snapshot anterior y, si hay pisos nuevos o
   bajadas de precio, añade esos eventos al principio de
   `data/pisos.json` (mismo formato de ítem que usa la lista "pisos"
   de la app Encaja: `id`, `zona`, `tipo`, `precio`, `caract`,
   `agente`, `fecha`, más `referencia`, `precio_anterior`,
   `bajada_pct`, `origen` y `url` para dar más contexto).
   Se guardan como máximo los últimos 500 eventos.
4. El workflow de GitHub Actions (`.github/workflows/scrape.yml`) lo
   ejecuta cada 6 horas y commitea `data/snapshot_latest.json` y
   `data/pisos.json` si algo cambió.

## Lado de Encaja

Encaja (el Artifact de Claude) no tiene una API externa a la que este
script pueda escribir directamente, así que el contrato es este JSON:
Encaja debe leer periódicamente

```
https://raw.githubusercontent.com/<usuario>/<repo>/main/data/pisos.json
```

y, para cada evento cuyo `id` todavía no tenga en su propia lista de
"pisos" (storage), añadirlo. Como `origen` puede ser `"nuevo"` o
`"bajada_precio"`, Encaja puede usarlo para decidir el texto/aviso que
muestra.

## Uso local

```bash
pip install -r requirements.txt
python scraper/scrape_inmoparadise.py
```

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
workflow "Scrape Inmoparadise" está habilitado (en repos nuevos a
veces hay que activarlo con un clic la primera vez). A partir de ahí
se ejecuta solo cada 6 horas; también se puede lanzar a mano desde
Actions → Scrape Inmoparadise → Run workflow.
