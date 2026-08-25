"""Approximate individual panel-table rows within a solar site boundary.

FortyGuard's segmentation model has no "solar panel" class (confirmed), so we
approximate row geometry ourselves: parallel strips at a configurable pitch/
width/orientation/phase, clipped to the site polygon. Defaults (5.38 m pitch,
2.82 m width, 4.86 m offset, north-south orientation) come from
measure_row_geometry.py's column-brightness-profile analysis of real satellite
imagery over this site -- still a site-wide average, not a per-row survey, but
grounded in the actual imagery rather than eyeballed. Override them with real
spacing if you have a surveyed layout.

The offset matters as much as the pitch: without it the strip grid is anchored
to the projection origin, which has no relation to where the real rows sit, so
every generated strip can straddle a real row and a gap. measure_row_geometry.py
recovers the phase from the same brightness profile it measures pitch from.
"""
import argparse
import json
import math
from pathlib import Path

from shapely.affinity import rotate
from shapely.geometry import box, shape, mapping
from shapely.geometry.base import BaseMultipartGeometry

from geo_utils import make_projector

ROOT = Path(__file__).resolve().parent.parent

# Clipping a strip against the site boundary can leave corner slivers with
# negligible area. They are not rows -- they inflate the row count and the
# at-risk denominator. A real edge row is hundreds of m^2 (site median is
# ~2400 m^2), so anything under this is geometric noise.
MIN_ROW_AREA_M2 = 10.0


def generate_rows(site_polygon_lonlat, row_pitch_m=6.0, row_width_m=2.5,
                  orientation_deg=0.0, row_offset_m=0.0,
                  min_area_m2=MIN_ROW_AREA_M2):
    """site_polygon_lonlat: shapely Polygon in lon/lat.
    orientation_deg: bearing of the row's long axis, 0 = north-south.
    row_offset_m: position of a row *centre* along the across-row axis, in the
        local metre frame (origin = site centroid). Row centres sit at
        row_offset_m + k * row_pitch_m.
    min_area_m2: drop clipped fragments smaller than this (see above).
    Returns a list of shapely Polygons (lon/lat) for each row segment.
    """
    lon0, lat0 = site_polygon_lonlat.centroid.x, site_polygon_lonlat.centroid.y
    to_local, to_lonlat = make_projector(lon0, lat0)

    local_coords = [to_local(lon, lat) for lon, lat in site_polygon_lonlat.exterior.coords]
    site_local = shape({"type": "Polygon", "coordinates": [local_coords]})

    # Rotate into a frame where rows are simple vertical strips, so a north-
    # south orientation needs no rotation and an east-west one needs 90 deg.
    site_rot = rotate(site_local, -orientation_deg, origin=(0, 0))
    minx, miny, maxx, maxy = site_rot.bounds

    half_width = row_width_m / 2.0
    k = math.floor((minx - half_width - row_offset_m) / row_pitch_m)

    strips = []
    while True:
        centre = row_offset_m + k * row_pitch_m
        if centre - half_width > maxx:
            break
        clipped = box(centre - half_width, miny, centre + half_width, maxy).intersection(site_rot)
        # Filter by area here, in the local metre frame, where the threshold
        # is a real physical quantity (doing it in lon/lat degrees would make
        # it latitude-dependent and meaningless).
        parts = clipped.geoms if isinstance(clipped, BaseMultipartGeometry) else [clipped]
        for part in parts:
            if part.area >= min_area_m2:
                strips.append(part)
        k += 1

    rows_lonlat = []
    for strip in strips:
        poly = rotate(strip, orientation_deg, origin=(0, 0))
        lonlat_coords = [to_lonlat(x, y) for x, y in poly.exterior.coords]
        rows_lonlat.append(shape({"type": "Polygon", "coordinates": [lonlat_coords]}))
    return rows_lonlat


def rows_to_geojson(rows):
    features = []
    for i, poly in enumerate(rows):
        c = poly.centroid
        features.append({
            "type": "Feature",
            "properties": {"row_id": i, "centroid_lon": c.x, "centroid_lat": c.y},
            "geometry": mapping(poly),
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson", help="Path to a GeoJSON FeatureCollection with the site polygon")
    parser.add_argument("--pitch", type=float, default=5.38, help="Row center-to-center spacing in meters")
    parser.add_argument("--width", type=float, default=2.82, help="Row width in meters")
    parser.add_argument("--orientation", type=float, default=0.0, help="Row bearing in degrees, 0 = north-south")
    parser.add_argument("--offset", type=float, default=4.86,
                        help="Row-centre phase in meters along the across-row axis (measure_row_geometry.py)")
    parser.add_argument("--min-area", type=float, default=MIN_ROW_AREA_M2,
                        help="Drop clipped row fragments smaller than this, in m^2")
    parser.add_argument("-o", "--output", default=None, help="Output GeoJSON path")
    args = parser.parse_args()

    site_path = Path(args.site_geojson)
    site_fc = json.loads(site_path.read_text())
    site_polygon = shape(site_fc["features"][0]["geometry"])

    rows = generate_rows(site_polygon, args.pitch, args.width, args.orientation,
                         args.offset, args.min_area)
    out_geojson = rows_to_geojson(rows)

    out_path = Path(args.output) if args.output else site_path.with_name(site_path.stem + "_panel_rows.geojson")
    out_path.write_text(json.dumps(out_geojson, indent=2))
    print(f"generated {len(rows)} panel-row segments -> {out_path}")


if __name__ == "__main__":
    main()
