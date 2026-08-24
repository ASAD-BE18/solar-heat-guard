"""Bundle the site boundary + heat-risk GeoJSON into docs/data.js for the
Leaflet visualization (avoids fetch/CORS issues when opened as a local file
or served via GitHub Pages -- data is just embedded as a JS variable)."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_geojson", help="Path to the site boundary GeoJSON")
    parser.add_argument("heat_risk_geojson", help="Path to the *_heat_risk.geojson output")
    args = parser.parse_args()

    site = json.loads(Path(args.site_geojson).read_text())
    heat_risk = json.loads(Path(args.heat_risk_geojson).read_text())

    DOCS_DIR.mkdir(exist_ok=True)
    out_path = DOCS_DIR / "data.js"
    out_path.write_text(
        "const SITE_BOUNDARY = " + json.dumps(site) + ";\n"
        "const PANEL_ROWS = " + json.dumps(heat_risk) + ";\n"
    )
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
