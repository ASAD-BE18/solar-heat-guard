# solar-heat-guard

A FortyGuard hackathon entry: flag which rows of a solar farm are at elevated
heat-damage risk, using [FortyGuard](https://www.fortyguard.com)'s hyperlocal
Temperature API.

**Site:** a utility-scale solar farm at 293 Mendel Rd, Fort Stockton, TX 79735.

## Why

Extended heat exposure accelerates degradation and can push panel surface
temperatures into damaging territory. This project pulls FortyGuard's
per-tile temperature/heat-exceedance data for the site, overlays it against
the array's actual row layout, and flags exactly which rows ran hottest --
turning a site-wide "it's hot today" into a "these specific rows are at
risk" signal.

## How it works

```
┌──────────────┐    ┌───────────────────┐    ┌─────────────────────┐
│ site KML/     │ →  │ FortyGuard         │ →  │ overlay + classify   │
│ GeoJSON       │    │ Create Heatmap API │    │ per panel row        │
│ boundary      │    │ (temperature tiles)│    │ → heat_risk.geojson  │
└──────────────┘    └───────────────────┘    └─────────────────────┘
       ↑ panel rows approximated from row spacing/orientation
```

1. **Boundary** -- the site polygon comes from a KML export (Google Earth),
   converted to GeoJSON.
2. **Panel rows** -- FortyGuard's satellite segmentation has no "solar panel"
   class, so individual rows are approximated ourselves: parallel strips at
   a configurable pitch/width/orientation, clipped to the site boundary.
3. **Heatmap** -- one request to FortyGuard's `Create Heatmap` endpoint
   returns a grid of temperature tiles across the whole site (`tcm` snapshot,
   or `exceedance`/`persistence` hours-above-threshold over a time window).
4. **Overlay** -- each panel row is matched to the tile it falls in and
   flagged against a risk threshold.

## Setup

```bash
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

cp .env.example .env
# fill in FORTYGUARD_API_KEY
```

## Usage

```bash
# 1. Convert every KML in data/kml/ to GeoJSON
python scripts/kml_to_geojson.py

# 2. (optional, heat_risk.py does this too) preview the approximated panel rows
python scripts/panel_rows.py data/geojson/solar_form_2.geojson

# 3. Run the heat-risk check for a given date
python scripts/heat_risk.py data/geojson/solar_form_2.geojson \
    --date 2024-07-15 --start-time 06:00 --end-time 18:00 \
    --analytic-type exceedance --threshold 40 --direction above
```

This submits one request to FortyGuard's API, polls until complete, and
writes `data/geojson/<site>_heat_risk.geojson` -- a FeatureCollection of
panel-row polygons, each tagged with the analytic value from its tile and an
`at_risk` flag. Load it into any GeoJSON-aware map viewer (geojson.io,
kepler.gl, QGIS, Leaflet) to see risk spatially.

**Picking `--date`:** FortyGuard's data has a processing lag -- very recent
dates (e.g. "yesterday") can return zero tiles even though the request
succeeds. `2024-07-15` is confirmed to return real data; prefer dates that
are safely in the past over "today"/"yesterday".

**Picking `--threshold`:** on `2024-07-15` this site's ambient temperature
(`tcm`) peaked around 35.5°C at 14:00, and an `exceedance` check for 40°C
across 06:00-18:00 came back with 0 hours for every row -- a real result,
not a bug, but it means 40°C is too high a bar to show any risk on that
particular day. A threshold closer to the site's actual peak (e.g. 34-35°C)
will show more differentiation for a demo.

## Known limitations (be upfront about these in the demo)

- **Panel rows are approximated, not surveyed.** Default pitch/width/
  orientation (6 m / 2.5 m / north-south) come from eyeballing the row
  pattern in satellite imagery, not a real site layout.
- **The risk threshold is a proxy.** FortyGuard's `tcm` layer reports
  ambient/land-surface temperature, not panel surface temperature. The
  threshold assumes panel surfaces run well above ambient under full sun --
  it isn't a validated damage spec. Swap in real panel thermal ratings if
  available.
- **FortyGuard's satellite segmentation has no "solar panel" class**
  (confirmed directly), which is why this project overlays heatmap tiles
  onto approximated geometry instead of using their segmentation endpoint.
- **API quota:** the FortyGuard account used here is capped at 30 heatmap
  requests/day. Each `heat_risk.py` run costs exactly one.

## Project layout

```
data/kml/            KML site boundaries (Google Earth exports)
data/geojson/         Converted boundaries + generated panel rows + risk output
scripts/
  kml_to_geojson.py   KML -> GeoJSON boundary conversion
  geo_utils.py        Local lon/lat <-> meters projection (site-scale accuracy)
  panel_rows.py       Approximate panel-row geometry generator
  fortyguard_client.py FortyGuard API client (submit heatmap, poll status)
  heat_risk.py        End-to-end pipeline: heatmap -> rows -> risk classification
```
