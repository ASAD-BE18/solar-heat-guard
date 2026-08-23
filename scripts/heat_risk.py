"""End-to-end heat-risk check for a solar site.

1. Submit a Create Heatmap job for the site polygon (1 FortyGuard request).
2. Generate approximate panel-row geometry within the same polygon.
3. For each row, look up the heatmap tile it falls in and flag it against a
   temperature threshold.

Note (confirmed 2026-08-23): FortyGuard's data has a processing lag -- a
request for "yesterday" returned zero tiles for two different sites, while
2024-07-15 returned real data. Pick --date accordingly; don't assume "today"
or "yesterday" will have data yet.

The risk --threshold is a rough proxy, not a validated damage limit: it
compares FortyGuard's per-tile ambient/land-surface temperature (tcm) against
a configurable cutoff, on the assumption that panel surface temperature runs
well above ambient under full sun. Tune it with real panel specs if available.
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from shapely.geometry import shape, mapping
from shapely.prepared import prep

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fortyguard_client import FortyGuardClient
from panel_rows import generate_rows

ROOT = Path(__file__).resolve().parent.parent


def classify_rows(row_polygons, tile_features, threshold_c):
    prepared_tiles = [(prep(shape(f["geometry"])), f["properties"]) for f in tile_features]

    results = []
    for i, row in enumerate(row_polygons):
        centroid = row.centroid
        tile_props = None
        for prepared, props in prepared_tiles:
            if prepared.contains(centroid):
                tile_props = props
                break
        temp = tile_props["average_temperature"] if tile_props else None
        at_risk = temp is not None and temp >= threshold_c
        results.append({
            "row_id": i,
            "geometry": row,
            "tile_temperature_c": temp,
            "at_risk": at_risk,
        })
    return results


def results_to_geojson(results):
    features = []
    for r in results:
        features.append({
            "type": "Feature",
            "properties": {
                "row_id": r["row_id"],
                "tile_temperature_c": r["tile_temperature_c"],
                "at_risk": r["at_risk"],
            },
            "geometry": mapping(r["geometry"]),
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson", help="Path to the site boundary GeoJSON")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (pick a date known to have data, see note above)")
    parser.add_argument("--start-time", default="14:00", help="HH:MM, default 14:00 (afternoon peak)")
    parser.add_argument("--granularity", type=int, default=100, choices=[60, 80, 100])
    parser.add_argument("--analytic-type", default="tcm", choices=["tcm", "time_of_measure", "exceedance", "persistence"])
    parser.add_argument("--threshold", type=float, default=40.0, help="Risk cutoff in deg C, compared to tile average_temperature (tcm)")
    parser.add_argument("--row-pitch", type=float, default=6.0)
    parser.add_argument("--row-width", type=float, default=2.5)
    parser.add_argument("--row-orientation", type=float, default=0.0)
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    load_dotenv()

    site_path = Path(args.site_geojson)
    site_fc = json.loads(site_path.read_text())
    site_polygon = shape(site_fc["features"][0]["geometry"])

    print("submitting heatmap request...")
    client = FortyGuardClient()
    activity_id = client.submit_heatmap(
        polygon_geojson=site_fc,
        date_time={"start_date": args.date, "start_time": args.start_time, "filter_type": 1},
        granularity=args.granularity,
        analytic_type=args.analytic_type,
    )
    print(f"activity_id={activity_id}, polling...")
    result = client.poll_until_complete(activity_id, interval_seconds=5, timeout_seconds=180)

    tile_features = result["map_data"]["features"]
    print(f"received {len(tile_features)} heatmap tiles")
    if not tile_features:
        print("WARNING: 0 tiles returned -- this date/location likely has no data yet. Aborting.", file=sys.stderr)
        sys.exit(1)

    rows = generate_rows(site_polygon, args.row_pitch, args.row_width, args.row_orientation)
    print(f"generated {len(rows)} panel-row segments")

    results = classify_rows(rows, tile_features, args.threshold)
    at_risk_count = sum(1 for r in results if r["at_risk"])
    print(f"{at_risk_count}/{len(results)} rows at or above {args.threshold} C")

    out_path = Path(args.output) if args.output else site_path.with_name(site_path.stem + "_heat_risk.geojson")
    out_path.write_text(json.dumps(results_to_geojson(results), indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
