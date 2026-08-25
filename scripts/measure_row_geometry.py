"""Measure real panel-row pitch/width from satellite imagery.

An earlier attempt did per-pixel color segmentation (Otsu threshold +
connected components) to vectorize individual row polygons directly. That
turned out too noisy to trust: at this zoom, row width (~10px) is close to
the satellite tile imagery's JPEG compression block size, so per-pixel
classification mostly picked up small bright artifacts rather than clean
row outlines (see git history for that attempt, since removed).

This script instead exploits the structure's strong regularity: it averages
pixel brightness column-wise across the whole site (thousands of pixels per
column), which cancels out the per-pixel compression noise and leaves a
clean periodic signal -- one dip per row. From that signal it measures:

  - row pitch: the dominant spatial period, via autocorrelation of the
    detrended column profile.
  - row width: the fraction of each period that reads darker than the
    period's mean (the row itself vs. the bare-soil gap), via phase-folding
    the profile over one measured period.
  - row phase (offset): where in that period the dark part actually sits,
    as a circular mean of the folded cycle weighted by darkness. Without
    this, pitch and width are correct but the generated strip grid is
    anchored to an arbitrary origin, so every strip can straddle a real row
    and a gap. Reported in metres along the across-row axis, in the same
    local frame panel_rows.py generates geometry in.
  - row orientation: rows appear as vertical lines in this north-up Web
    Mercator mosaic, so orientation is 0 deg (north-south) by construction;
    this script doesn't re-derive it, it's a property of how the tiles are
    projected, not the site.

These measured numbers replace the eyeballed 6m/2.5m defaults previously
used in heat_risk.py/panel_rows.py -- still an aggregate site-wide estimate,
not a per-row survey, but grounded in the actual imagery rather than a
guess from looking at a screenshot.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import satellite_tiles as st
from geo_utils import make_projector
from satellite_tiles import rasterize_polygon_mask

METERS_PER_PIXEL_AT_EQUATOR_Z0 = 156543.03392


def meters_per_pixel(lat_deg, zoom):
    import math
    return METERS_PER_PIXEL_AT_EQUATOR_Z0 * math.cos(math.radians(lat_deg)) / (2 ** zoom)


def column_profile(mosaic, boundary_mask):
    gray = np.asarray(mosaic.convert("L")).astype(np.float64)
    masked = np.where(boundary_mask > 0, gray, np.nan)
    with np.errstate(invalid="ignore"):
        return np.nanmean(masked, axis=0)


def detrend(profile, window=101):
    profile = np.nan_to_num(profile, nan=np.nanmean(profile))
    smooth = np.convolve(profile, np.ones(window) / window, mode="same")
    return profile - smooth


def estimate_pitch_px(detrended, edge_trim=150, light_smooth=3, min_lag=5, max_lag=90):
    light = np.convolve(detrended, np.ones(light_smooth) / light_smooth, mode="same")
    d = light[edge_trim:-edge_trim]
    d = d - d.mean()
    ac = np.correlate(d, d, mode="full")
    ac = ac[len(ac) // 2:]
    ac_norm = ac / ac[0]

    best_lag, best_val = None, -np.inf
    for lag in range(min_lag, max_lag):
        if ac_norm[lag] > ac_norm[lag - 1] and ac_norm[lag] > ac_norm[lag + 1] and ac_norm[lag] > best_val:
            best_lag, best_val = lag, ac_norm[lag]
    return best_lag, best_val


def fold_cycle(detrended, pitch_px, sample_slice):
    """Phase-folds the profile over one period to get an average cycle shape.
    Bins 1:1 with pixels (pitch_px is already an integer pixel count) -- using
    more bins than that leaves most of them empty/nan, since i % pitch_px only
    ever takes pitch_px distinct integer values.

    Bin b corresponds to mosaic pixel columns congruent to
    (sample_slice.start + b) modulo pitch_px."""
    seg = detrended[sample_slice]
    acc, cnt = np.zeros(pitch_px), np.zeros(pitch_px)
    for i, v in enumerate(seg):
        b = i % pitch_px
        acc[b] += v
        cnt[b] += 1
    return acc / cnt


def width_fraction(avg_cycle):
    """Fraction of the cycle reading darker than the cycle mean -- the row
    itself, as opposed to the bare-soil gap between rows."""
    return float(np.mean(avg_cycle < 0))


def dark_centre_bin(avg_cycle, width_bins):
    """Bin at the centre of the dark (panel) part of the cycle.

    Slides a circular boxcar the width of the measured row footprint over the
    folded cycle and takes the position capturing the most darkness -- i.e.
    "where would a row of this width have to sit to explain the dip?", which
    is exactly what panel_rows.py needs. A darkness-weighted centroid was
    tried first and sat ~8% of a pitch off a brute-force sweep, because the
    bright gap's tails drag the centroid; this estimator is not sensitive to
    them. Circular so a dark run wrapping the end of the array still resolves.
    """
    n = len(avg_cycle)
    width_bins = max(1, min(n, int(round(width_bins))))
    doubled = np.concatenate([avg_cycle, avg_cycle])
    sums = [doubled[start:start + width_bins].sum() for start in range(n)]
    start = int(np.argmin(sums))
    return float((start + (width_bins - 1) / 2.0) % n)


def phase_offset_m(centre_px, site, tile_x0, tile_y0, zoom, pitch_m):
    """Convert a mosaic pixel column that sits at a row centre into an offset
    in panel_rows.py's local metre frame (origin = site centroid), reduced
    modulo the pitch since only the phase matters."""
    lon, _ = st.pixel_to_lonlat(centre_px, 0, tile_x0, tile_y0, zoom)
    to_local, _ = make_projector(site.centroid.x, site.centroid.y)
    x_local, _ = to_local(lon, site.centroid.y)
    return float(x_local % pitch_m)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site_geojson")
    parser.add_argument("--zoom", type=int, default=19)
    parser.add_argument("--sample-start", type=int, default=700, help="Column range (px) known to avoid the mosaic's edge/gap artifacts")
    parser.add_argument("--sample-end", type=int, default=1700)
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    from shapely.geometry import shape
    site_path = Path(args.site_geojson)
    site = shape(json.loads(site_path.read_text())["features"][0]["geometry"])
    minx, miny, maxx, maxy = site.bounds

    print(f"fetching satellite mosaic at zoom {args.zoom}...")
    mosaic, tile_x0, tile_y0, zoom = st.fetch_mosaic(minx, miny, maxx, maxy, args.zoom)
    boundary_px = [st.lonlat_to_pixel(lon, lat, tile_x0, tile_y0, zoom) for lon, lat in site.exterior.coords]
    boundary_mask = rasterize_polygon_mask(mosaic.size, boundary_px)

    profile = column_profile(mosaic, boundary_mask)
    detrended = detrend(profile)

    pitch_px, confidence = estimate_pitch_px(detrended)
    if pitch_px is None:
        print("ERROR: no clear periodic signal found -- imagery may not show a regular row pattern here.", file=sys.stderr)
        sys.exit(1)

    sample = slice(args.sample_start, args.sample_end)
    avg_cycle = fold_cycle(detrended, pitch_px, sample)
    frac = width_fraction(avg_cycle)
    centre_bin = dark_centre_bin(avg_cycle, frac * pitch_px)

    mpp = meters_per_pixel(site.centroid.y, zoom)
    pitch_m = pitch_px * mpp
    width_m = frac * pitch_px * mpp
    offset_m = phase_offset_m(args.sample_start + centre_bin, site,
                              tile_x0, tile_y0, zoom, pitch_m)

    print(f"pitch: {pitch_px} px ({pitch_m:.2f} m), autocorrelation={confidence:.3f}")
    print(f"width: {frac:.2f} of cycle ({width_m:.2f} m)")
    print(f"offset: dark centre at bin {centre_bin:.2f}/{pitch_px} -> {offset_m:.2f} m")
    print("orientation: 0 deg (north-south, by construction of the north-up mosaic)")

    result = {
        "pitch_m": round(pitch_m, 2),
        "width_m": round(width_m, 2),
        "offset_m": round(offset_m, 2),
        "orientation_deg": 0.0,
        "pitch_px": pitch_px,
        "dark_centre_bin": round(centre_bin, 2),
        "autocorrelation": round(float(confidence), 4),
        "zoom": zoom,
        "meters_per_pixel": round(mpp, 4),
    }
    out_path = Path(args.output) if args.output else site_path.with_name(site_path.stem + "_row_geometry.json")
    out_path.write_text(json.dumps(result, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
