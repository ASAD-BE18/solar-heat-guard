# solar-heat-guard

FortyGuard hackathon entry: detect which parts of a solar farm are at elevated
heat-damage risk, using FortyGuard's Temperature API.

Target sites: six utility-scale solar farms around 293 Mendel Rd, Fort
Stockton, TX 79735 (`data/kml/solar_farm_{1..6}.kml`). Farm 1 is the original
site; 2-6 were added later from a Google Earth export. The KML placemarks were
named inconsistently ("solar farm 2", "solar form 4", "form 6") and numbered
from 2; both were normalised, so the id, the filename and the label all agree.
Five farms resolve; farm 5's tiles come back with a single value and the run
is refused rather than published.

## Setup

```
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt   # requests, python-dotenv, shapely
cp .env.example .env   # fill in FORTYGUARD_API_KEY
```

## Pipeline

```
python scripts/kml_to_geojson.py                 # data/kml/*.kml -> data/geojson/*.geojson
python scripts/detect_panel_rows.py data/geojson/solar_farm_N.geojson --overlay /tmp/rows.png
python scripts/heat_risk.py data/geojson/solar_farm_1.geojson --date YYYY-MM-DD     --start-time 06:00 --end-time 18:00 --granularity 60
python scripts/build_viz_data.py                  # every site with a heat-risk output
python -m pytest tests/ -q                       # offline; no API, no imagery
```

- `scripts/fortyguard_client.py` — client for FortyGuard's `POST /v1/heatmap`
  (submit a job for a polygon + date/time + analytic_type) and
  `GET /v1/status/{activity_id}` (poll until `Completed`, then read
  `result.map_data` tiles and `result.stats_data`).
- `scripts/detect_panel_rows.py` — FortyGuard's satellite segmentation has no
  "solar panel" class, so row geometry is detected from Esri World Imagery
  (cached under `data/fortyguard/_tile_cache/`, no FortyGuard quota). Panels
  read dark against bare soil; the script scans for the row **bearing** that
  maximises across-row profile contrast, reads row centre lines off that
  profile, then walks each row in ~5 m bins and keeps only the bins where the
  imagery is actually dark. This site: bearing -1.51°, pitch 5.63 m, width
  2.56 m, 135 row lines, 1244 runs, 41.2% of the site.
  Three things this fixed, all of which shipped before — do not reintroduce
  any of them:
    1. **Bearing is not 0°.** The old code asserted rows run north-south "by
       construction of the north-up mosaic". Bearing is a property of the
       site, not the projection. Contrast at the true bearing is 0.92 against
       0.10 at 0° — a mis-set bearing is loud, if you look.
    2. **Never fit pitch/width/phase before the bearing.** A tilt smears the
       profile and biases both (5.38/2.82 vs 5.63/2.56). Weak autocorrelation
       (0.34) was the tell and was ignored.
    3. **Rows do not all run north-south.** The scan shears by tan(bearing),
       so it reaches only angles near the scanline axis; farms 3 and 4 run
       east-west and were silently fitted to their 400 m block structure
       (contrast 0.20 and 0.15 against 0.92 for a real fit). Both
       orientations are scanned now -- the transposed mask covers east-west
       -- and the higher contrast wins. Never widen `limit_deg` instead:
       tan blows up toward 90 deg.
    4. **Never extrapolate a lattice across the site polygon.** The boundary
       is hand-drawn and contains bare ground; panel presence must be tested
       per location against the imagery. The lattice claimed 52.4% coverage
       against a 43.9% imagery dark fraction.
  `--overlay` renders the detection on the imagery. All three faults were
  found by eye there, not in the numbers — always look before trusting a run.
  Output is committed so downstream steps and tests need no imagery.
- `scripts/heat_risk.py` — ties it together: submits one heatmap request,
  loads the detected rows, splits each row where it crosses a tile boundary so
  every scored segment lies inside exactly one tile, and flags segments
  against a site-relative percentile cutoff (a rough proxy — it ranks
  exceedance-hours within the site, not a validated panel damage spec).
  `at_risk` is three-state: segments with no covering tile are `null`
  (unknown), never `false`. Do not go back to matching a row centroid to a
  tile: rows run up to ~330 m against 60 m tiles, so that used 21 of 176
  tiles and clipped the site's true 1.98-3.11 h range to 2.51-2.98 h. The
  percentile basis is row-in-tile segments, and every label must say so —
  tile-weighting gives a materially different cutoff.
  `--threshold` defaults to `auto`: one extra `tcm` request measures this
  site's own temperature for the same window and the mean of the tiles'
  **peak** (`max_temperature`) becomes the threshold, so no run carries a
  hand-picked number. Calibrate on the peak, never the window mean --
  exceedance counts hours above the threshold, so the threshold has to sit
  where the diurnal curve is flat. Measured on farm 4, 2025-07-15: the
  window mean (29.09 C) sits on the morning ramp and returned exactly
  7.00 h for every tile; the mean of peaks (34.18 C) returned -0.10 to
  0.40. Negative means that spot never reached the threshold -- FortyGuard
  interpolates the curve and reports the shortfall rather than clamping.
  A run whose scored values are all identical is refused, not published.
  The AOI is buffered (`--aoi-buffer`, default 600 m) before the request:
  FortyGuard returns whole grid cells and drops the ones the AOI only
  partly covers, so the unbuffered run's mosaic stopped 147 m short of the
  south edge and left 9.4% of the site (6.4 ha) with no covering tile.
  Buffer by a whole multiple of `--granularity`. Scoring still uses the
  unbuffered site; the extra tiles hold no panel rows and drop out at
  segmentation. Confirmed live on 2026-08-29: the 600 m request
  returned 1,040 tiles, 213 of which hold rows, and left zero unknown
  segments against 173 unbuffered.
- `scripts/geo_utils.py` — local equirectangular lon/lat <-> meters projection,
  accurate enough at single-site scale (~1km).
- Panel temperature / power-loss modelling was removed deliberately (issue #1):
  the question is "which areas run hotter than the rest", which does not need
  it. Do not add it back without asking.

## FortyGuard API gotchas (confirmed by live testing)

- **30 heatmap requests/day limit.** Never call the live API without checking
  with the developer first â€” always name what a call would confirm and get
  a go-ahead, even for a single diagnostic request. Completed results are
  cached under `data/fortyguard/_response_cache/` (gitignored), so re-running
  an identical request is free; `--no-cache` forces a live call.
- **Recent dates return empty tiles.** A request for "yesterday" relative to
  the current date came back `200 Completed` with zero tiles, for both this
  site and FortyGuard's own documented example polygon â€” a backend processing
  lag, not a coverage gap. Pick a `--date` safely in the past (e.g.
  `2024-07-15`, confirmed working) rather than assuming "today"/"yesterday"
  has data yet.
- Their docs site (docs-api.fortyguard.com) is a JS-rendered SPA; a plain
  fetch only returns the empty shell. The real endpoint docs are embedded as
  string literals in the app's `main.*.js` bundle if they ever need
  re-checking â€” `curl` that file directly and search for the endpoint id.

## Conventions

- Never call the FortyGuard API without explicit go-ahead (quota is scarce
  and shared with the rest of the team).
- Follow the user's global git/security conventions (commit only, never
  push; no secrets committed â€” `.env` is gitignored).
