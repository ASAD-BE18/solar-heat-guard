"""Convert every KML polygon in data/kml/ to a GeoJSON FeatureCollection in data/geojson/."""
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
KML_DIR = ROOT / "data" / "kml"
GEOJSON_DIR = ROOT / "data" / "geojson"

NS = {"kml": "http://www.opengis.net/kml/2.2"}


def parse_coordinates(text):
    points = []
    for token in text.strip().split():
        parts = token.split(",")
        lon, lat = float(parts[0]), float(parts[1])
        points.append([lon, lat])
    return points


def kml_to_geojson(kml_path):
    tree = ET.parse(kml_path)
    features = []
    for placemark in tree.getroot().iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name_el = placemark.find("kml:name", NS)
        name = name_el.text if name_el is not None else kml_path.stem
        ring = placemark.find(".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", NS)
        if ring is None:
            continue
        coords = parse_coordinates(ring.text)
        features.append({
            "type": "Feature",
            "properties": {"name": name},
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    GEOJSON_DIR.mkdir(parents=True, exist_ok=True)
    kml_files = sorted(KML_DIR.glob("*.kml"))
    if not kml_files:
        print(f"no .kml files found in {KML_DIR}", file=sys.stderr)
        return
    for kml_path in kml_files:
        geojson = kml_to_geojson(kml_path)
        out_path = GEOJSON_DIR / (kml_path.stem + ".geojson")
        out_path.write_text(json.dumps(geojson, indent=2))
        print(f"{kml_path.name} -> {out_path.relative_to(ROOT)} ({len(geojson['features'])} feature(s))")


if __name__ == "__main__":
    main()
