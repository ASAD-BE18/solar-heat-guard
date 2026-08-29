"""Bundle each site's boundary + heat-risk GeoJSON into docs/ for the Leaflet
visualization (avoids fetch/CORS issues when opened as a local file or served
via GitHub Pages -- data is just embedded as a JS variable).

One file per site rather than one bundle: the six Fort Stockton farms come to
~9.7 MB together, which is not a page you want to block on. docs/sites.js is a
small index the page always loads; docs/site_<id>.js is fetched only when that
site is selected.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEOJSON_DIR = ROOT / "data" / "geojson"
DOCS_DIR = ROOT / "docs"

# ~0.1 m at this latitude -- far finer than the 60 m tiles being visualised,
# and a fifth off the payload.
COORD_DECIMALS = 6


def round_coords(obj, decimals=COORD_DECIMALS):
    if isinstance(obj, float):
        return round(obj, decimals)
    if isinstance(obj, list):
        return [round_coords(v, decimals) for v in obj]
    if isinstance(obj, dict):
        return {k: (v if k == "properties" else round_coords(v, decimals))
                for k, v in obj.items()}
    return obj


def display_name(site_id, kml_name):
    """A consistent label for the picker.

    The KML placemarks were typed by hand and disagree three ways -- "solar
    farm 2", "solar form 4", "form 6" -- so the label is derived from the site
    id instead. The placemark's own text is kept as source_name so the mapping
    back to the KML stays traceable. ("form" is a typo for "farm" that is
    already baked into the filenames; renaming those would break every
    committed artefact, so it is corrected only for display.)
    """
    trailing = site_id.rsplit("_", 1)[-1]
    return f"Solar farm {trailing}" if trailing.isdigit() else site_id.replace("_", " ")


def site_summary(site_id, name, boundary, heat_risk):
    md = heat_risk.get("metadata") or {}
    xs = [c[0] for c in boundary["features"][0]["geometry"]["coordinates"][0]]
    ys = [c[1] for c in boundary["features"][0]["geometry"]["coordinates"][0]]
    return {
        "id": site_id,
        "name": display_name(site_id, name),
        "source_name": name,
        "bounds": [[min(ys), min(xs)], [max(ys), max(xs)]],
        "date": md.get("date"),
        "n_at_risk": md.get("n_at_risk"),
        "n_scored": md.get("n_scored"),
        "at_risk_area_m2": md.get("at_risk_area_m2"),
        "scored_area_m2": md.get("scored_area_m2"),
        "row_axis": (md.get("row_geometry") or {}).get("row_axis"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson", nargs="*",
                        help="Site boundary GeoJSON files. Each one's heat-risk output is read "
                             "from <stem>_heat_risk.geojson. Default: every site in "
                             "data/geojson/ that has one")
    args = parser.parse_args()

    if args.site_geojson:
        site_paths = [Path(p) for p in args.site_geojson]
    else:
        site_paths = sorted(p.with_name(p.name.replace("_heat_risk", ""))
                            for p in GEOJSON_DIR.glob("*_heat_risk.geojson"))
    if not site_paths:
        parser.error("no sites found -- run heat_risk.py on a site first")

    DOCS_DIR.mkdir(exist_ok=True)
    index = []
    for site_path in site_paths:
        heat_path = site_path.with_name(site_path.stem + "_heat_risk.geojson")
        if not heat_path.exists():
            print(f"skipping {site_path.name} -- no {heat_path.name}")
            continue
        boundary = json.loads(site_path.read_text())
        heat_risk = json.loads(heat_path.read_text())
        site_id = site_path.stem
        name = (boundary["features"][0].get("properties") or {}).get("name") or site_id

        out = DOCS_DIR / f"site_{site_id}.js"
        out.write_text(
            f"SITE_DATA[{json.dumps(site_id)}] = " + json.dumps({
                "boundary": round_coords(boundary),
                "segments": round_coords(heat_risk),
            }) + ";\n")
        index.append(site_summary(site_id, name, boundary, heat_risk))
        print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size / 1e6:.2f} MB)")

    index_path = DOCS_DIR / "sites.js"
    index_path.write_text("const SITES = " + json.dumps(index, indent=1) + ";\n"
                          "const SITE_DATA = {};\n")
    print(f"wrote {index_path.relative_to(ROOT)} ({len(index)} site(s))")


if __name__ == "__main__":
    main()
