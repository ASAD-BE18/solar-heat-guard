"""Approximate individual panel-table rows within a solar site boundary.

FortyGuard's segmentation model has no "solar panel" class (confirmed), so we
approximate row geometry ourselves: parallel strips at a configurable pitch/
width/orientation, clipped to the site polygon. Defaults (6 m pitch, 2.5 m
width, north-south orientation) are a rough single-axis-tracker assumption
based on the visible row pattern in satellite imagery, not a surveyed layout
-- override them with real spacing if you have it.
"""
import argparse
import json
from pathlib import Path

from shapely.affinity import rotate
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union

from geo_utils import make_projector

ROOT = Path(__file__).resolve().parent.parent


def generate_rows(site_polygon_lonlat, row_pitch_m=6.0, row_width_m=2.5, orientation_deg=0.0):
    """site_polygon_lonlat: shapely Polygon in lon/lat.
    orientation_deg: bearing of the row's long axis, 0 = north-south.
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

    strips = []
    x = minx - (minx % row_pitch_m)
    while x <= maxx:
        strip = box(x, miny, x + row_width_m, maxy)
        clipped = strip.intersection(site_rot)
        if not clipped.is_empty:
            strips.append(clipped)
        x += row_pitch_m

    rows_local = [rotate(s, orientation_deg, origin=(0, 0)) for s in strips]

    rows_lonlat = []
    for geom in rows_local:
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            if poly.area == 0:
                continue
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_geojson", help="Path to a GeoJSON FeatureCollection with the site polygon")
    parser.add_argument("--pitch", type=float, default=6.0, help="Row center-to-center spacing in meters")
    parser.add_argument("--width", type=float, default=2.5, help="Row width in meters")
    parser.add_argument("--orientation", type=float, default=0.0, help="Row bearing in degrees, 0 = north-south")
    parser.add_argument("-o", "--output", default=None, help="Output GeoJSON path")
    args = parser.parse_args()

    site_path = Path(args.site_geojson)
    site_fc = json.loads(site_path.read_text())
    site_polygon = shape(site_fc["features"][0]["geometry"])

    rows = generate_rows(site_polygon, args.pitch, args.width, args.orientation)
    out_geojson = rows_to_geojson(rows)

    out_path = Path(args.output) if args.output else site_path.with_name(site_path.stem + "_panel_rows.geojson")
    out_path.write_text(json.dumps(out_geojson, indent=2))
    print(f"generated {len(rows)} panel-row segments -> {out_path}")


if __name__ == "__main__":
    main()
