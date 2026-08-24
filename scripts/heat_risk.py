"""End-to-end heat-risk check for a solar site.

1. Submit a Create Heatmap job for the site polygon (1 FortyGuard request).
2. Generate approximate panel-row geometry within the same polygon.
3. For each row, look up the heatmap tile it falls in and flag it as at-risk
   (temperature >= threshold for tcm, or exceedance/persistence hours > 0
   for those analytic types -- FortyGuard's own threshold param already
   picks the hours count).

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
import pv_thermal

ROOT = Path(__file__).resolve().parent.parent


# Tile property holding the analytic value differs by analytic_type and isn't
# fully documented for exceedance/persistence -- try known/likely names in
# order rather than assuming one.
VALUE_FIELD_CANDIDATES = [
    "average_temperature", "average_value", "value",
    "exceedance_hours", "persistence_hours", "hours",
]


def pick_value_field(tile_properties):
    for candidate in VALUE_FIELD_CANDIDATES:
        if candidate in tile_properties:
            return candidate
    numeric_keys = [k for k, v in tile_properties.items() if k != "tile_id" and isinstance(v, (int, float))]
    return numeric_keys[0] if numeric_keys else None


def find_tile_properties(centroid, prepared_tiles):
    for prepared, props in prepared_tiles:
        if prepared.contains(centroid):
            return props
    return None


def classify_rows(row_polygons, tile_features, threshold, strict_greater=False):
    value_field = pick_value_field(tile_features[0]["properties"]) if tile_features else None
    prepared_tiles = [(prep(shape(f["geometry"])), f["properties"]) for f in tile_features]

    results = []
    for i, row in enumerate(row_polygons):
        tile_props = find_tile_properties(row.centroid, prepared_tiles)
        value = tile_props.get(value_field) if (tile_props and value_field) else None
        if value is None:
            at_risk = False
        else:
            at_risk = value > threshold if strict_greater else value >= threshold
        results.append({
            "geometry": row,
            "properties": {"row_id": i, "value_field": value_field, "tile_value": value, "at_risk": at_risk},
        })
    return results


def classify_rows_pv(row_polygons, tile_features, ghi_w_m2, noct_c, temp_coeff_pct_per_c, max_operating_temp_c):
    """Classify rows using the NOCT cell-temperature + power-loss model (see pv_thermal.py)."""
    value_field = pick_value_field(tile_features[0]["properties"]) if tile_features else None
    prepared_tiles = [(prep(shape(f["geometry"])), f["properties"]) for f in tile_features]

    results = []
    for i, row in enumerate(row_polygons):
        tile_props = find_tile_properties(row.centroid, prepared_tiles)
        ambient_c = tile_props.get(value_field) if (tile_props and value_field) else None
        if ambient_c is None:
            results.append({
                "geometry": row,
                "properties": {"row_id": i, "ambient_temperature_c": None, "ghi_w_m2": ghi_w_m2,
                               "cell_temperature_c": None, "power_loss_percent": None, "at_risk": False},
            })
            continue
        cell_c = pv_thermal.cell_temperature(ambient_c, ghi_w_m2, noct_c)
        loss_pct = pv_thermal.power_loss_percent(cell_c, temp_coeff_pct_per_c)
        results.append({
            "geometry": row,
            "properties": {
                "row_id": i,
                "ambient_temperature_c": ambient_c,
                "ghi_w_m2": ghi_w_m2,
                "cell_temperature_c": cell_c,
                "power_loss_percent": loss_pct,
                "at_risk": cell_c >= max_operating_temp_c,
            },
        })
    return results


def results_to_geojson(results):
    features = [{"type": "Feature", "properties": r["properties"], "geometry": mapping(r["geometry"])} for r in results]
    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson", help="Path to the site boundary GeoJSON")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (pick a date known to have data, see note above)")
    parser.add_argument("--start-time", default="14:00", help="HH:MM, default 14:00 (afternoon peak)")
    parser.add_argument("--end-time", default=None, help="HH:MM; if given, queries the start-end range (filter_type 2) -- required for exceedance/persistence to be meaningful")
    parser.add_argument("--granularity", type=int, default=100, choices=[60, 80, 100])
    parser.add_argument("--analytic-type", default="tcm", choices=["tcm", "time_of_measure", "exceedance", "persistence"])
    parser.add_argument("--threshold", type=float, default=40.0, help="Risk cutoff, compared to the tile's analytic value (deg C for tcm, hours for exceedance/persistence)")
    parser.add_argument("--direction", default="above", choices=["above", "below"], help="Only used by exceedance/persistence")
    parser.add_argument("--pv-model", action=argparse.BooleanOptionalAction, default=True,
                         help="Use the NOCT cell-temperature + power-loss model (needs analytic-type=tcm; 1 extra FortyGuard request for irradiance)")
    parser.add_argument("--noct", type=float, default=pv_thermal.NOCT_DEFAULT_C, help="Module NOCT spec, deg C (typical default, not this site's real spec)")
    parser.add_argument("--temp-coeff", type=float, default=pv_thermal.TEMP_COEFF_DEFAULT_PCT_PER_C, help="Module temperature coefficient, %%/C (typical default)")
    parser.add_argument("--max-operating-temp", type=float, default=pv_thermal.MAX_OPERATING_TEMP_DEFAULT_C, help="Cell temperature (deg C) at/above which a row is flagged at-risk")
    parser.add_argument("--row-pitch", type=float, default=6.0)
    parser.add_argument("--row-width", type=float, default=2.5)
    parser.add_argument("--row-orientation", type=float, default=0.0)
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    load_dotenv()

    site_path = Path(args.site_geojson)
    site_fc = json.loads(site_path.read_text())
    site_polygon = shape(site_fc["features"][0]["geometry"])

    date_time = {"start_date": args.date, "start_time": args.start_time}
    if args.end_time:
        date_time["end_time"] = args.end_time
        date_time["filter_type"] = 2
    else:
        date_time["filter_type"] = 1

    is_thresholded_analytic = args.analytic_type in ("exceedance", "persistence")

    print("submitting heatmap request...")
    client = FortyGuardClient()
    activity_id = client.submit_heatmap(
        polygon_geojson=site_fc,
        date_time=date_time,
        granularity=args.granularity,
        analytic_type=args.analytic_type,
        threshold=args.threshold if is_thresholded_analytic else None,
        direction=args.direction if is_thresholded_analytic else None,
    )
    print(f"activity_id={activity_id}, polling...")
    result = client.poll_until_complete(activity_id, interval_seconds=5, timeout_seconds=180)

    tile_features = result["map_data"]["features"]
    print(f"received {len(tile_features)} heatmap tiles")
    if not tile_features:
        print("WARNING: 0 tiles returned -- this date/location likely has no data yet. Aborting.", file=sys.stderr)
        sys.exit(1)
    print(f"tile[0] properties: {tile_features[0]['properties']}")

    rows = generate_rows(site_polygon, args.row_pitch, args.row_width, args.row_orientation)
    print(f"generated {len(rows)} panel-row segments")

    use_pv_model = args.pv_model and args.analytic_type == "tcm"
    if args.pv_model and args.analytic_type != "tcm":
        print("--pv-model needs actual temperature values, only available with --analytic-type tcm; skipping.", file=sys.stderr)

    if use_pv_model:
        value_field = pick_value_field(tile_features[0]["properties"])
        temps = [f["properties"][value_field] for f in tile_features if f["properties"].get(value_field) is not None]
        representative_ambient_c = sum(temps) / len(temps)
        centroid = site_polygon.centroid

        print(f"submitting env_params request for irradiance (ambient={representative_ambient_c:.2f} C)...")
        env_activity_id = client.submit_env_params(
            latitude=centroid.y, longitude=centroid.x, temperature=representative_ambient_c,
            date_time=date_time, analysis=["solar_irradiance"],
        )
        env_result = client.poll_until_complete(env_activity_id, interval_seconds=5, timeout_seconds=180)
        ghi_values = [loc["solar_irradiance"]["clear_sky"]["ghi"] for loc in env_result["locations"]]
        ghi_w_m2 = sum(ghi_values) / len(ghi_values)
        print(f"GHI={ghi_w_m2:.1f} W/m^2")

        results = classify_rows_pv(rows, tile_features, ghi_w_m2, args.noct, args.temp_coeff, args.max_operating_temp)
        at_risk_count = sum(1 for r in results if r["properties"]["at_risk"])
        print(f"{at_risk_count}/{len(results)} rows at or above {args.max_operating_temp} C cell temperature")
    else:
        # For exceedance/persistence, FortyGuard's threshold param already
        # picked out hours above/below args.threshold -- here we just report
        # rows with any non-zero hours as "at risk" (strictly > 0, not >=,
        # since 0 hours means "never happened" not "at risk").
        results = classify_rows(
            rows, tile_features,
            threshold=0.0 if is_thresholded_analytic else args.threshold,
            strict_greater=is_thresholded_analytic,
        )
        at_risk_count = sum(1 for r in results if r["properties"]["at_risk"])
        print(f"{at_risk_count}/{len(results)} rows flagged at risk")

    out_path = Path(args.output) if args.output else site_path.with_name(site_path.stem + "_heat_risk.geojson")
    out_path.write_text(json.dumps(results_to_geojson(results), indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
