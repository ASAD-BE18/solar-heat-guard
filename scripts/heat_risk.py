"""End-to-end heat-exposure ranking for a solar site.

1. Submit a Create Heatmap job for the site polygon (1 FortyGuard request).
2. Load the panel-row geometry detected from imagery by detect_panel_rows.py.
3. Split each panel row at the heatmap tile boundaries it crosses, so every
   scored unit lies inside exactly one tile.
4. Rank those units against the site's own distribution and flag the top
   --risk-percentile as hotspots.

The question this answers is "which parts of this farm run hotter than the
rest of it, and where should the operator look first" -- not "what
temperature will these panels reach". Nothing here estimates panel
temperature or power loss.

Site-relative by construction, at two levels: FortyGuard's `exceedance`
analytic counts hours above --threshold (set that to the site's own measured
average for the window, so "exceedance" means "hours above this site's own
typical temperature"), and the hotspot cutoff is a percentile of this site's
own scored units rather than an externally chosen number.

Why segments and not whole rows: a panel row here runs up to ~330 m while
tiles are 60 m, so a row spans several tiles. An earlier version assigned
each row the value of the single tile its centroid landed in, which used 21
of the 176 returned tiles and never saw the site's hottest or coolest
ground -- the true range 1.98-3.11 h collapsed to 2.51-2.98 h. Splitting at
tile boundaries uses every tile and gives each segment the measurement that
actually covers it.

Segments are near-uniform in area (one tile's width of one row), so ranking
them is close to ranking by panel area -- which is the right weighting for
"how much of this farm is running hot". Segments with no covering tile are
reported at_risk=null (unknown), never false: absence of data is not
evidence of safety.

Completed API results are cached on disk (data/fortyguard/_response_cache),
keyed by request payload, so re-running this script with the same parameters
costs no quota. Pass --no-cache to force a live request.

Note (confirmed 2026-08-23): FortyGuard's data has a processing lag -- a
request for "yesterday" returned zero tiles for two different sites, while
2024-07-15 returned real data. Pick --date accordingly; don't assume "today"
or "yesterday" will have data yet.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseMultipartGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect_panel_rows import MIN_SEGMENT_AREA_M2, polygon_area_m2
from fortyguard_client import FortyGuardClient


# Which tile property holds the analytic value depends on which analytic was
# requested, and is not fully documented for exceedance/persistence. Keyed by
# analytic_type so an exceedance run can never silently read a temperature
# field that happens to also be present on the tile (and vice versa); the
# generic "value"/"average_value" names are last-resort fallbacks only.
VALUE_FIELDS_BY_ANALYTIC = {
    "tcm": ["average_temperature", "temperature", "average_value", "value"],
    "time_of_measure": ["time_of_measure", "measure_time", "average_value", "value"],
    "exceedance": ["exceedance_hours", "exceedance", "hours", "average_value", "value"],
    "persistence": ["persistence_hours", "persistence", "hours", "average_value", "value"],
}


def pick_value_field(tile_properties, analytic_type):
    for candidate in VALUE_FIELDS_BY_ANALYTIC.get(analytic_type, ["value"]):
        if candidate in tile_properties:
            return candidate
    numeric_keys = [k for k, v in tile_properties.items()
                    if k != "tile_id" and isinstance(v, (int, float)) and not isinstance(v, bool)]
    return numeric_keys[0] if numeric_keys else None


def _polygon_parts(geometry):
    parts = geometry.geoms if isinstance(geometry, BaseMultipartGeometry) else [geometry]
    return [p for p in parts if p.geom_type == "Polygon" and not p.is_empty]


def segment_rows_by_tile(row_features, tile_features, analytic_type,
                         min_area_m2=MIN_SEGMENT_AREA_M2):
    """Split each panel row at the heatmap tile boundaries it crosses.

    Every returned segment lies inside exactly one tile and carries that
    tile's own value, so no segment is coloured by a measurement taken
    somewhere else along the row.

    The part of a row covered by no tile is emitted as its own segment with
    value=None, so ground we have no data for stays on the map as unknown
    rather than quietly disappearing from it.

    Returns (segments, value_field).
    """
    value_field = pick_value_field(tile_features[0]["properties"], analytic_type) if tile_features else None
    tile_geometries = [shape(f["geometry"]) for f in tile_features]
    tile_index = STRtree(tile_geometries)

    segments = []
    for row_feature in row_features:
        row = shape(row_feature["geometry"])
        row_properties = row_feature.get("properties") or {}
        covered = []
        for tile_i in tile_index.query(row):
            piece = tile_geometries[tile_i].intersection(row)
            if piece.is_empty:
                continue
            covered.append(piece)
            tile_properties = tile_features[tile_i]["properties"]
            for part in _polygon_parts(piece):
                if polygon_area_m2(part) < min_area_m2:
                    continue
                segments.append({
                    "geometry": part,
                    "row_index": row_properties.get("row_index"),
                    "tile_id": tile_properties.get("tile_id"),
                    "value": tile_properties.get(value_field) if value_field else None,
                })

        remainder = row.difference(unary_union(covered)) if covered else row
        for part in _polygon_parts(remainder):
            if polygon_area_m2(part) < min_area_m2:
                continue
            segments.append({
                "geometry": part,
                "row_index": row_properties.get("row_index"),
                "tile_id": None,
                "value": None,
            })

    return segments, value_field


def classify(segments, value_field, threshold):
    """Flag segments at or above threshold. at_risk is None where no tile
    covered the segment -- unknown, not safe."""
    results = []
    for i, segment in enumerate(segments):
        value = segment["value"]
        results.append({
            "geometry": segment["geometry"],
            "properties": {
                "segment_id": i,
                "row_index": segment["row_index"],
                "tile_id": segment["tile_id"],
                "area_m2": round(polygon_area_m2(segment["geometry"]), 1),
                "value_field": value_field,
                "tile_value": value,
                "at_risk": None if value is None else bool(value >= threshold),
            },
        })
    return results


def summarize(results):
    """Returns (at_risk, no_data, scored) -- scored excludes no-data segments,
    so the at-risk fraction is never diluted by units we could not evaluate."""
    at_risk = sum(1 for r in results if r["properties"]["at_risk"] is True)
    no_data = sum(1 for r in results if r["properties"]["at_risk"] is None)
    return at_risk, no_data, len(results) - no_data


def area_m2_where(results, predicate):
    return sum(r["properties"]["area_m2"] for r in results if predicate(r["properties"]["at_risk"]))


def results_to_geojson(results, metadata=None):
    features = [{"type": "Feature", "properties": r["properties"], "geometry": mapping(r["geometry"])}
                for r in results]
    fc = {"type": "FeatureCollection", "features": features}
    if metadata is not None:
        fc["metadata"] = metadata
    return fc


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson", help="Path to the site boundary GeoJSON")
    parser.add_argument("--panel-rows", default=None,
                        help="Panel-row GeoJSON from detect_panel_rows.py "
                             "(default: <site>_panel_rows.geojson)")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (pick a date known to have data, see note above)")
    parser.add_argument("--start-time", default="06:00", help="HH:MM, default 06:00")
    parser.add_argument("--end-time", default="18:00",
                        help="HH:MM; the window exceedance-hours are counted over. Pass an empty "
                             "string for a single instant instead (filter_type 1)")
    parser.add_argument("--granularity", type=int, default=60, choices=[60, 80, 100],
                        help="Tile size in metres; 60 is the finest available")
    parser.add_argument("--analytic-type", default="exceedance",
                        choices=["tcm", "time_of_measure", "exceedance", "persistence"])
    parser.add_argument("--threshold", type=float, default=35.5,
                        help="FortyGuard exceedance/persistence threshold, deg C -- set this to the "
                             "site's own measured average temperature for the window, not an "
                             "arbitrary number (see README)")
    parser.add_argument("--direction", default="above", choices=["above", "below"],
                        help="Only used by exceedance/persistence")
    parser.add_argument("--risk-percentile", type=float, default=75.0,
                        help="Flag segments at or above this percentile of THIS SITE's own scored "
                             "segments (site-relative hotspot detection, not an absolute cutoff)")
    parser.add_argument("--min-segment-area", type=float, default=MIN_SEGMENT_AREA_M2,
                        help="Drop segments smaller than this, in m^2")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore the on-disk response cache and spend a real API request")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    load_dotenv()

    site_path = Path(args.site_geojson)
    site_fc = json.loads(site_path.read_text())

    rows_path = Path(args.panel_rows) if args.panel_rows else site_path.with_name(
        site_path.stem + "_panel_rows.geojson")
    if not rows_path.exists():
        print(f"ERROR: {rows_path} not found -- run detect_panel_rows.py on this site first.",
              file=sys.stderr)
        sys.exit(1)
    rows_fc = json.loads(rows_path.read_text())
    row_features = rows_fc["features"]
    row_geometry = rows_fc.get("metadata") or {}
    print(f"loaded {len(row_features)} detected panel-row segments from {rows_path.name}")

    date_time = {"start_date": args.date, "start_time": args.start_time}
    if args.end_time:
        date_time["end_time"] = args.end_time
        date_time["filter_type"] = 2
    else:
        date_time["filter_type"] = 1

    is_thresholded_analytic = args.analytic_type in ("exceedance", "persistence")
    if is_thresholded_analytic and not args.end_time:
        print("WARNING: exceedance/persistence count hours over a window; without --end-time "
              "this asks for a single instant.", file=sys.stderr)

    print("submitting heatmap request...")
    client = FortyGuardClient(use_cache=not args.no_cache)
    heatmap_payload = client.heatmap_payload(
        polygon_geojson=site_fc,
        date_time=date_time,
        granularity=args.granularity,
        analytic_type=args.analytic_type,
        threshold=args.threshold if is_thresholded_analytic else None,
        direction=args.direction if is_thresholded_analytic else None,
    )
    result = client.run("heatmap", heatmap_payload, interval_seconds=5, timeout_seconds=180)

    tile_features = (result.get("map_data") or {}).get("features") or []
    print(f"received {len(tile_features)} heatmap tiles")
    if not tile_features:
        print("WARNING: 0 tiles returned -- this date/location likely has no data yet. Aborting.",
              file=sys.stderr)
        sys.exit(1)
    print(f"tile[0] properties: {tile_features[0]['properties']}")

    segments, value_field = segment_rows_by_tile(
        row_features, tile_features, args.analytic_type, args.min_segment_area)
    print(f"split into {len(segments)} row-in-tile segments; value field: {value_field}")

    tiles_used = {s["tile_id"] for s in segments if s["tile_id"] is not None}
    unmatched = sum(1 for s in segments if s["value"] is None)
    if unmatched == len(segments):
        print("ERROR: no segment matched any tile -- check that the heatmap actually covers "
              "the site polygon.", file=sys.stderr)
        sys.exit(1)
    print(f"{len(tiles_used)}/{len(tile_features)} returned tiles contain panel rows")
    if unmatched:
        print(f"NOTE: {unmatched}/{len(segments)} segments are covered by no returned tile -- "
              f"reported as at_risk=null (unknown), not as safe.", file=sys.stderr)

    percentile_cutoff = None
    if is_thresholded_analytic:
        # FortyGuard's threshold param already counted hours above
        # args.threshold; here we flag the segments whose count sits in the
        # top --risk-percentile of the site's own segments. Segments with no
        # tile are excluded from the percentile and stay at_risk=null.
        values = [s["value"] for s in segments if s["value"] is not None]
        percentile_cutoff = float(np.percentile(values, args.risk_percentile))
        print(f"{len(values)} scored segments over {len(tiles_used)} tiles, "
              f"values {min(values):.2f}-{max(values):.2f}, "
              f"p{args.risk_percentile:.0f} cutoff={percentile_cutoff:.2f}")
        results = classify(segments, value_field, percentile_cutoff)
    else:
        results = classify(segments, value_field, args.threshold)

    at_risk_count, no_data, scored = summarize(results)
    at_risk_area = area_m2_where(results, lambda v: v is True)
    scored_area = area_m2_where(results, lambda v: v is not None)
    if is_thresholded_analytic:
        print(f"{at_risk_count}/{scored} scored segments flagged as hotspots "
              f"(>= p{args.risk_percentile:.0f} of this site's own segments)")
    else:
        print(f"{at_risk_count}/{scored} scored segments flagged at risk")
    print(f"hotspot panel area {at_risk_area / 1e4:.2f} ha of {scored_area / 1e4:.2f} ha scored")
    if no_data:
        print(f"{no_data} segments have no data (at_risk=null)")

    metadata = {
        "date": args.date,
        "start_time": args.start_time,
        "end_time": args.end_time or None,
        "granularity_m": args.granularity,
        "analytic_type": args.analytic_type,
        "threshold": args.threshold,
        "direction": args.direction if is_thresholded_analytic else None,
        "risk_percentile": args.risk_percentile if is_thresholded_analytic else None,
        "percentile_cutoff": percentile_cutoff,
        "percentile_basis": "row-in-tile segments" if is_thresholded_analytic else None,
        "n_tiles": len(tile_features),
        "n_tiles_with_rows": len(tiles_used),
        "n_panel_rows": len(row_features),
        "n_segments": len(results),
        "n_scored": scored,
        "n_no_data": no_data,
        "n_at_risk": at_risk_count,
        "at_risk_area_m2": round(at_risk_area, 1),
        "scored_area_m2": round(scored_area, 1),
        "row_geometry": row_geometry,
    }

    out_path = Path(args.output) if args.output else site_path.with_name(site_path.stem + "_heat_risk.geojson")
    out_path.write_text(json.dumps(results_to_geojson(results, metadata), indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
