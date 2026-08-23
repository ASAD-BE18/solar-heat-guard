# solar-heat-guard

FortyGuard hackathon entry: detect which parts of a solar farm are at elevated
heat-damage risk, using FortyGuard's Temperature API.

Target site: a utility-scale solar farm at 293 Mendel Rd, Fort Stockton, TX
79735 (`data/kml/solar_form_2.kml`).

## Setup

```
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt   # requests, python-dotenv, shapely
cp .env.example .env   # fill in FORTYGUARD_API_KEY
```

## Pipeline

```
python scripts/kml_to_geojson.py                 # data/kml/*.kml -> data/geojson/*.geojson
python scripts/panel_rows.py data/geojson/solar_form_2.geojson   # approximate panel rows
python scripts/heat_risk.py data/geojson/solar_form_2.geojson --date YYYY-MM-DD
```

- `scripts/fortyguard_client.py` — client for FortyGuard's `POST /v1/heatmap`
  (submit a job for a polygon + date/time + analytic_type) and
  `GET /v1/status/{activity_id}` (poll until `Completed`, then read
  `result.map_data` tiles and `result.stats_data`).
- `scripts/panel_rows.py` — FortyGuard's satellite segmentation has no "solar
  panel" class, so individual panel rows are approximated ourselves: parallel
  strips at a configurable pitch/width/orientation (default 6m / 2.5m /
  north-south, a rough read of the visible row pattern in satellite imagery,
  not a surveyed layout), clipped to the site polygon.
- `scripts/heat_risk.py` — ties it together: submits one heatmap request for
  the site, generates panel rows, looks up which tile each row's centroid
  falls in, and flags rows against a temperature threshold (also a rough
  proxy — it compares ambient/land-surface temperature to a cutoff, not a
  validated panel damage spec).
- `scripts/geo_utils.py` — local equirectangular lon/lat <-> meters projection,
  accurate enough at single-site scale (~1km).

## FortyGuard API gotchas (confirmed by live testing)

- **30 heatmap requests/day limit.** Never call the live API without checking
  with the developer first — always name what a call would confirm and get
  a go-ahead, even for a single diagnostic request.
- **Recent dates return empty tiles.** A request for "yesterday" relative to
  the current date came back `200 Completed` with zero tiles, for both this
  site and FortyGuard's own documented example polygon — a backend processing
  lag, not a coverage gap. Pick a `--date` safely in the past (e.g.
  `2024-07-15`, confirmed working) rather than assuming "today"/"yesterday"
  has data yet.
- Their docs site (docs-api.fortyguard.com) is a JS-rendered SPA; a plain
  fetch only returns the empty shell. The real endpoint docs are embedded as
  string literals in the app's `main.*.js` bundle if they ever need
  re-checking — `curl` that file directly and search for the endpoint id.

## Conventions

- Never call the FortyGuard API without explicit go-ahead (quota is scarce
  and shared with the rest of the team).
- Follow the user's global git/security conventions (commit only, never
  push; no secrets committed — `.env` is gitignored).
