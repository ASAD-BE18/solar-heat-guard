"""Detect panel-row geometry from satellite imagery.

Replaces the earlier "measure a pitch, then extrapolate a uniform lattice
across the whole site polygon" approach (measure_row_geometry.py +
panel_rows.py, both removed). That approach had three failure modes, all
visible when the output was overlaid on the imagery:

  1. It assumed rows run exactly north-south, on the reasoning that rows
     "appear vertical in a north-up Web Mercator mosaic". Row bearing is a
     property of the site's layout, not of the projection. This site's rows
     run 1.50 deg off grid north, so a vertical strip crossed a real row
     roughly every half pitch and the generated strips sat in the bare gaps
     between rows.
  2. Because pitch, width and phase were all fitted to a column profile
     smeared by that unaccounted tilt, they were biased too (5.38 m pitch /
     2.82 m width against 5.63 m / 2.56 m once the tilt is removed). The
     smear also showed up as a weak 0.34 autocorrelation, which was recorded
     but not treated as the warning it was.
  3. The lattice was extrapolated across the entire site polygon, so bare
     ground inside a hand-drawn boundary -- margins, access roads, the gaps
     between panel blocks -- was labelled as panel. It claimed 52.4% panel
     coverage where the imagery shows 43.9%.

This script instead takes the imagery as the authority at every step:

  - bearing: scan candidate angles, shear the panel mask by each, and keep
    the angle that maximises the contrast of the across-row profile. At the
    right angle a row line reads 94% dark and a gap 2%; at 0 deg both smear
    to ~0.4. That 9x contrast gap is what makes the estimate unambiguous.
  - pitch/width: measured from the profile at that angle, and reported --
    but only as descriptive statistics. Nothing downstream extrapolates
    from them.
  - row centre lines: taken directly from the profile's dark runs, so a
    missing or irregularly spaced row is represented as it actually is
    rather than forced onto a perfect lattice.
  - row extent: each row is walked end to end in bins, and a bin is kept
    only where the imagery under it is actually dark. This is what stops
    rows at the block gaps and at the edge of the array instead of at the
    drawn boundary.

Imagery is Esri World Imagery XYZ tiles, cached on disk under
data/fortyguard/_tile_cache (gitignored) -- no FortyGuard quota is involved.
The detected geometry is written to GeoJSON and committed, so heat_risk.py
and the test suite never need the imagery or the network.

Pass --overlay to render the detection on top of the imagery as a PNG. The
three failure modes above were all found by looking at that overlay, not in
the numbers, so it is worth looking at whenever the site or imagery changes.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseMultipartGeometry

sys.path.insert(0, str(Path(__file__).resolve().parent))
import satellite_tiles as st
from satellite_tiles import rasterize_polygon_mask

# Panels read dark against bare desert soil. The brightness histogram inside
# this site is strongly bimodal -- a panel mode near 64 and a soil mode near
# 215 -- with a wide, flat valley between them, so the exact cut is not
# critical; 128 sits in that valley. --panel-threshold overrides it, and
# --overlay is how you check the choice on a new site.
DEFAULT_PANEL_THRESHOLD = 128

# A row line vs a gap separate at 0.94 / 0.02 dark once the bearing is right,
# so a half-and-half cut is comfortably clear of both modes. Used both to pick
# row centre lines out of the across-row profile and to decide whether a bin
# along a row is really panel.
DEFAULT_MIN_DARK_FRACTION = 0.5

# Along-row bin length. Short enough to cut a row at a block gap or an access
# road (both a few metres wide here), long enough that a single bin still
# averages over hundreds of pixels.
DEFAULT_BIN_M = 5.0

# Clipping a detected row against the site boundary can leave corner slivers
# of negligible area. They are not rows -- they inflate the row count and the
# at-risk denominator downstream.
MIN_SEGMENT_AREA_M2 = 10.0

METERS_PER_PIXEL_AT_EQUATOR_Z0 = 156543.03392


def meters_per_pixel(lat_deg, zoom):
    return METERS_PER_PIXEL_AT_EQUATOR_Z0 * math.cos(math.radians(lat_deg)) / (2 ** zoom)


def shear_shift(height, bearing_deg):
    """Per-scanline x offset of a row line at the given bearing.

    bearing_deg is measured clockwise from grid north, so a positive bearing
    leans east as the row runs north. Mosaic y increases southward, hence the
    negative sign: a row centred on x = c is at x = c + shift[y] on scanline y.

    Both the profile accumulation and the output geometry read their offsets
    from here, so they cannot drift apart.
    """
    return np.rint(-np.arange(height) * math.tan(math.radians(bearing_deg))).astype(int)


def across_row_profile(panel_mask, inside_mask, bearing_deg):
    """Dark fraction as a function of across-row position, at a given bearing.

    Accumulates the panel mask along lines tilted to `bearing_deg`: a row line
    lies at constant x - shift[y], so summing into those bins integrates each
    row along its own length instead of across it.

    Returns (profile, x_origin), where profile[i] is the dark fraction at
    x - shift[y] = i - x_origin, restricted to bins with enough in-boundary
    pixels to be meaningful.
    """
    height, width = panel_mask.shape
    shift = shear_shift(height, bearing_deg)
    origin = int(np.abs(shift).max()) + 1

    dark_sum = np.zeros(width + 2 * origin)
    inside_sum = np.zeros_like(dark_sum)
    # shift is integer and monotone in y, so scanlines sharing an offset are
    # contiguous. Summing each such block first turns one shifted add per
    # scanline (thousands) into one per distinct offset (a few hundred).
    block_starts = np.concatenate([[0], np.flatnonzero(np.diff(shift)) + 1])
    dark_blocks = np.add.reduceat(panel_mask, block_starts, axis=0)
    inside_blocks = np.add.reduceat(inside_mask, block_starts, axis=0)
    for block, y in enumerate(block_starts):
        start = origin - shift[y]
        dark_sum[start:start + width] += dark_blocks[block]
        inside_sum[start:start + width] += inside_blocks[block]

    # Bins near the sheared edges only see a sliver of the site and would
    # read as spuriously bright or dark; require at least half the coverage
    # of the best-covered bin.
    keep = inside_sum > 0.5 * inside_sum.max()
    profile = np.full(dark_sum.shape, np.nan)
    profile[keep] = dark_sum[keep] / inside_sum[keep]
    return profile, origin


def profile_contrast(profile):
    """How cleanly the profile separates rows from gaps.

    p90 - p10 of the dark fraction: ~0.92 at the correct bearing here, ~0.10
    at a wrong one. Used as the objective for the bearing scan and reported
    so a bad fit on a new site is visible rather than silent. A site whose
    rows do not all share one bearing would show up here as a low contrast at
    every angle.
    """
    valid = profile[~np.isnan(profile)]
    return float(np.percentile(valid, 90) - np.percentile(valid, 10))


def estimate_bearing(panel_mask, inside_mask, limit_deg=3.0, coarse_step=0.25, fine_step=0.02):
    """Bearing of the rows' long axis, in degrees clockwise from grid north.

    Coarse sweep to find the basin, then a fine sweep inside it. Web Mercator
    is conformal, so an angle measured in mosaic pixels is the true bearing at
    this scale.
    """
    def best_over(angles):
        """Highest-contrast angle, taking the centre of a tied plateau.

        Several angles can score identically -- the shear offsets are whole
        pixels, so a range of angles can align the rows the same way. Taking
        whichever tied angle happens to sort highest biases the answer by up
        to the width of that plateau; its midpoint is the actual peak.
        """
        scored = []
        for angle in angles:
            profile, _ = across_row_profile(panel_mask, inside_mask, float(angle))
            scored.append((profile_contrast(profile), float(angle)))
        best = max(contrast for contrast, _ in scored)
        tied = [angle for contrast, angle in scored if best - contrast < 1e-9]
        return best, float(np.median(tied))

    coarse_contrast, coarse_angle = best_over(np.arange(-limit_deg, limit_deg + 1e-9, coarse_step))
    fine_contrast, fine_angle = best_over(
        np.arange(coarse_angle - coarse_step, coarse_angle + coarse_step + 1e-9, fine_step))
    if fine_contrast >= coarse_contrast:
        return fine_angle, fine_contrast
    return coarse_angle, coarse_contrast


def dark_runs(profile, min_dark_fraction):
    """Contiguous runs of the profile that read as panel, as (start, end_exclusive).

    NaN bins (outside the usable range) break a run rather than extending it.
    """
    is_dark = ~np.isnan(profile) & (profile > min_dark_fraction)
    runs, start = [], None
    for i, dark in enumerate(is_dark):
        if dark and start is None:
            start = i
        elif not dark and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(is_dark)))
    return runs


def measure_pitch(centres):
    """Median centre-to-centre spacing, over consecutive pairs only.

    Median, not mean: real sites have missing rows and access roads, so some
    consecutive gaps are two or three pitches wide and would drag a mean up.
    """
    if len(centres) < 2:
        return None
    return float(np.median(np.diff(sorted(centres))))


def scanline_prefix_sums(panel_mask, inside_mask):
    """Prefix sums along x, so any row's window on any scanline is a subtraction.

    Computed once for the whole site and reused for every row -- they do not
    depend on which row is being measured.
    """
    height = panel_mask.shape[0]
    zeros = np.zeros((height, 1))
    return (np.concatenate([zeros, np.cumsum(panel_mask, axis=1)], axis=1),
            np.concatenate([zeros, np.cumsum(inside_mask, axis=1)], axis=1))


def row_dark_fractions(prefix_sums, width, centre, half_width, shift, bin_px):
    """Dark fraction of each along-row bin of one row, plus its in-boundary coverage.

    prefix_sums: the (dark, inside) pair from scanline_prefix_sums.

    Returns (bin_bounds, dark_fraction, coverage) where bin_bounds[i] is
    (y_start, y_end) and coverage is the share of the bin's nominal footprint
    that lies inside the site boundary.
    """
    dark_cumulative, inside_cumulative = prefix_sums
    height = dark_cumulative.shape[0]

    xc = centre + shift
    lo = np.clip(np.floor(xc - half_width).astype(int), 0, width)
    hi = np.clip(np.ceil(xc + half_width).astype(int), 0, width)
    rows_index = np.arange(height)
    dark_per_scanline = dark_cumulative[rows_index, hi] - dark_cumulative[rows_index, lo]
    inside_per_scanline = inside_cumulative[rows_index, hi] - inside_cumulative[rows_index, lo]

    bounds, fractions, coverages = [], [], []
    for y0 in range(0, height, bin_px):
        y1 = min(y0 + bin_px, height)
        inside_total = inside_per_scanline[y0:y1].sum()
        nominal = (y1 - y0) * 2.0 * half_width
        bounds.append((y0, y1))
        fractions.append(dark_per_scanline[y0:y1].sum() / inside_total if inside_total else 0.0)
        coverages.append(inside_total / nominal if nominal else 0.0)
    return bounds, np.array(fractions), np.array(coverages)


def merge_kept_bins(bounds, keep):
    """Merge adjacent kept bins into contiguous (y_start, y_end) extents."""
    extents = []
    for (y0, y1), keeping in zip(bounds, keep):
        if not keeping:
            continue
        if extents and extents[-1][1] == y0:
            extents[-1][1] = y1
        else:
            extents.append([y0, y1])
    return [tuple(e) for e in extents]


def extent_to_polygon(centre, half_width, y0, y1, shift, tile_x0, tile_y0, zoom):
    """Convert one along-row extent into a lon/lat quadrilateral.

    The row's edges are straight lines in pixel space, so the four corners
    fully describe it; converting the corners rather than the whole outline
    keeps the tilt exact.
    """
    y_last = min(y1, len(shift) - 1)
    corners_px = [
        (centre + shift[y0] - half_width, y0),
        (centre + shift[y0] + half_width, y0),
        (centre + shift[y_last] + half_width, y1),
        (centre + shift[y_last] - half_width, y1),
    ]
    ring = [st.pixel_to_lonlat(px, py, tile_x0, tile_y0, zoom) for px, py in corners_px]
    return shape({"type": "Polygon", "coordinates": [ring + [ring[0]]]})


def detect(site_polygon, zoom=19, panel_threshold=DEFAULT_PANEL_THRESHOLD,
           min_dark_fraction=DEFAULT_MIN_DARK_FRACTION, bin_m=DEFAULT_BIN_M,
           min_area_m2=MIN_SEGMENT_AREA_M2, bearing_deg=None, verbose=True):
    """Detect panel-row segments inside site_polygon from satellite imagery.

    Returns (segments, stats): segments is a list of dicts with a lon/lat
    shapely Polygon plus its row index, and stats describes the fit.
    """
    minx, miny, maxx, maxy = site_polygon.bounds
    if verbose:
        print(f"fetching satellite mosaic at zoom {zoom}...")
    mosaic, tile_x0, tile_y0, zoom = st.fetch_mosaic(minx, miny, maxx, maxy, zoom)

    boundary_px = [st.lonlat_to_pixel(lon, lat, tile_x0, tile_y0, zoom)
                   for lon, lat in site_polygon.exterior.coords]
    inside_mask = (rasterize_polygon_mask(mosaic.size, boundary_px) > 0)
    grey = np.asarray(mosaic.convert("L"))
    panel_mask = (grey < panel_threshold) & inside_mask

    imagery_dark_fraction = float(panel_mask.sum() / inside_mask.sum())
    mpp = meters_per_pixel(site_polygon.centroid.y, zoom)

    if bearing_deg is None:
        bearing_deg, contrast = estimate_bearing(panel_mask, inside_mask)
    else:
        profile, _ = across_row_profile(panel_mask, inside_mask, bearing_deg)
        contrast = profile_contrast(profile)
    if verbose:
        print(f"bearing: {bearing_deg:+.2f} deg from grid north (profile contrast {contrast:.3f})")

    profile, origin = across_row_profile(panel_mask, inside_mask, bearing_deg)
    runs = dark_runs(profile, min_dark_fraction)
    if not runs:
        raise ValueError(
            "no panel rows found -- the imagery under this boundary has no dark/bright "
            "row structure at any bearing. Check --panel-threshold and --overlay.")

    centres = [(start + end - 1) / 2.0 - origin for start, end in runs]
    widths_px = [end - start for start, end in runs]
    pitch_px = measure_pitch(centres)
    median_width_px = float(np.median(widths_px))
    if verbose:
        print(f"detected {len(centres)} row centre lines; "
              f"pitch {pitch_px * mpp:.2f} m, width {median_width_px * mpp:.2f} m")

    height, width = panel_mask.shape
    shift = shear_shift(height, bearing_deg)
    bin_px = max(1, int(round(bin_m / mpp)))
    prefix_sums = scanline_prefix_sums(panel_mask, inside_mask)

    segments = []
    kept_bins = considered_bins = 0
    for row_index, (centre, width_px) in enumerate(zip(centres, widths_px)):
        half_width = width_px / 2.0
        bounds, fractions, coverages = row_dark_fractions(
            prefix_sums, width, centre, half_width, shift, bin_px)
        # A bin mostly outside the drawn boundary is not evidence either way,
        # so it is neither kept nor counted as a rejection.
        evaluable = coverages >= 0.5
        keep = evaluable & (fractions >= min_dark_fraction)
        considered_bins += int(evaluable.sum())
        kept_bins += int(keep.sum())

        for y0, y1 in merge_kept_bins(bounds, keep):
            polygon = extent_to_polygon(centre, half_width, y0, y1, shift,
                                        tile_x0, tile_y0, zoom)
            clipped = polygon.intersection(site_polygon)
            if clipped.is_empty:
                continue
            parts = clipped.geoms if isinstance(clipped, BaseMultipartGeometry) else [clipped]
            for part in parts:
                if part.geom_type != "Polygon":
                    continue
                if polygon_area_m2(part) < min_area_m2:
                    continue
                segments.append({"geometry": part, "row_index": row_index})

    panel_area_m2 = sum(polygon_area_m2(s["geometry"]) for s in segments)
    site_area_m2 = float(inside_mask.sum()) * mpp * mpp
    if verbose:
        print(f"along-row bins kept: {kept_bins}/{considered_bins} "
              f"({100 * kept_bins / considered_bins:.1f}%) -- "
              f"{considered_bins - kept_bins} rejected as bare ground or block gap")
        print(f"{len(segments)} panel-row segments covering "
              f"{panel_area_m2 / 1e4:.2f} ha ({100 * panel_area_m2 / site_area_m2:.1f}% of site); "
              f"imagery dark fraction {100 * imagery_dark_fraction:.1f}%")

    stats = {
        "bearing_deg": round(bearing_deg, 3),
        "profile_contrast": round(contrast, 4),
        "pitch_m": round(pitch_px * mpp, 3),
        "row_width_m": round(median_width_px * mpp, 3),
        "n_row_lines": len(centres),
        "n_segments": len(segments),
        "along_row_bin_m": round(bin_px * mpp, 3),
        "bins_kept": kept_bins,
        "bins_considered": considered_bins,
        "panel_area_m2": round(panel_area_m2, 1),
        "site_area_m2": round(site_area_m2, 1),
        "panel_area_fraction": round(panel_area_m2 / site_area_m2, 4),
        "imagery_dark_fraction": round(imagery_dark_fraction, 4),
        "panel_threshold": panel_threshold,
        "min_dark_fraction": min_dark_fraction,
        "zoom": zoom,
        "meters_per_pixel": round(mpp, 4),
        "imagery": "Esri World Imagery",
    }
    return segments, stats


def polygon_area_m2(polygon_lonlat):
    """Area of a small lon/lat polygon in square metres.

    Local equirectangular scaling about the polygon's own centroid; accurate
    well past the ~1 km scale these segments live at.
    """
    lat = polygon_lonlat.centroid.y
    m_per_deg_lat = math.pi / 180 * 6378137.0
    return polygon_lonlat.area * m_per_deg_lat * m_per_deg_lat * math.cos(math.radians(lat))


def segments_to_geojson(segments, stats):
    features = []
    for i, segment in enumerate(segments):
        centroid = segment["geometry"].centroid
        features.append({
            "type": "Feature",
            "properties": {
                "segment_id": i,
                "row_index": segment["row_index"],
                "area_m2": round(polygon_area_m2(segment["geometry"]), 1),
                "centroid_lon": centroid.x,
                "centroid_lat": centroid.y,
            },
            "geometry": mapping(segment["geometry"]),
        })
    return {"type": "FeatureCollection", "metadata": stats, "features": features}


def render_overlay(site_polygon, segments, zoom, out_path):
    """Draw the detected segments over the imagery, for eyeballing the fit."""
    from PIL import Image, ImageDraw

    minx, miny, maxx, maxy = site_polygon.bounds
    mosaic, tile_x0, tile_y0, zoom = st.fetch_mosaic(minx, miny, maxx, maxy, zoom)
    base = mosaic.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for segment in segments:
        ring = [st.lonlat_to_pixel(lon, lat, tile_x0, tile_y0, zoom)
                for lon, lat in segment["geometry"].exterior.coords]
        draw.polygon(ring, fill=(0, 255, 60, 140))
    Image.alpha_composite(base, layer).convert("RGB").save(out_path)
    print(f"wrote {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson", help="Path to the site boundary GeoJSON")
    parser.add_argument("--zoom", type=int, default=19, help="Imagery zoom level")
    parser.add_argument("--panel-threshold", type=int, default=DEFAULT_PANEL_THRESHOLD,
                        help="Grey level below which a pixel reads as panel")
    parser.add_argument("--min-dark-fraction", type=float, default=DEFAULT_MIN_DARK_FRACTION,
                        help="Dark share needed for a row line, and for an along-row bin to count as panel")
    parser.add_argument("--bin-m", type=float, default=DEFAULT_BIN_M,
                        help="Along-row bin length in metres")
    parser.add_argument("--min-area", type=float, default=MIN_SEGMENT_AREA_M2,
                        help="Drop clipped segments smaller than this, in m^2")
    parser.add_argument("--bearing", type=float, default=None,
                        help="Skip the bearing scan and use this bearing, in degrees clockwise from grid north")
    parser.add_argument("--overlay", default=None,
                        help="Also render the detection over the imagery to this PNG path")
    parser.add_argument("-o", "--output", default=None, help="Output GeoJSON path")
    args = parser.parse_args()

    site_path = Path(args.site_geojson)
    site_polygon = shape(json.loads(site_path.read_text())["features"][0]["geometry"])

    segments, stats = detect(
        site_polygon, zoom=args.zoom, panel_threshold=args.panel_threshold,
        min_dark_fraction=args.min_dark_fraction, bin_m=args.bin_m,
        min_area_m2=args.min_area, bearing_deg=args.bearing)

    out_path = Path(args.output) if args.output else site_path.with_name(
        site_path.stem + "_panel_rows.geojson")
    out_path.write_text(json.dumps(segments_to_geojson(segments, stats), indent=2))
    print(f"wrote {out_path}")

    if args.overlay:
        render_overlay(site_polygon, segments, args.zoom, args.overlay)


if __name__ == "__main__":
    main()
