"""Regression tests for the detection, segmentation, and client-cache logic.

Deliberately offline: nothing here touches the FortyGuard API (the account is
capped at 30 heatmap requests/day) and nothing fetches satellite imagery --
the detector is exercised against synthetic mosaics with known geometry, and
tile data is synthesised or read from the committed artefacts.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import Point, Polygon, box, mapping, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import detect_panel_rows as detect
import heat_risk
import satellite_tiles as st
from fortyguard_client import FortyGuardClient
from geo_utils import make_projector

SITE_PATH = ROOT / "data" / "geojson" / "solar_form_2.geojson"
PANEL_ROWS_PATH = ROOT / "data" / "geojson" / "solar_form_2_panel_rows.geojson"
HEAT_RISK_PATH = ROOT / "data" / "geojson" / "solar_form_2_heat_risk.geojson"
ZOOM = 19


@pytest.fixture(scope="module")
def site():
    return shape(json.loads(SITE_PATH.read_text())["features"][0]["geometry"])


# --- geo_utils --------------------------------------------------------------

def test_projector_round_trips():
    to_local, to_lonlat = make_projector(-103.28, 30.955)
    for lon, lat in [(-103.28, 30.955), (-103.2803, 30.9559), (-103.2837, 30.9551)]:
        x, y = to_local(lon, lat)
        back_lon, back_lat = to_lonlat(x, y)
        assert back_lon == pytest.approx(lon, abs=1e-12)
        assert back_lat == pytest.approx(lat, abs=1e-12)


def test_projector_origin_is_zero():
    to_local, _ = make_projector(-103.28, 30.955)
    assert to_local(-103.28, 30.955) == pytest.approx((0.0, 0.0))


def test_projector_scale_is_metres():
    """One degree of latitude is ~111 km; one of longitude is that times cos(lat)."""
    lat0 = 30.955
    to_local, _ = make_projector(0.0, lat0)
    x, y = to_local(1.0, lat0 + 1.0)
    assert y == pytest.approx(111_319, rel=0.01)
    assert x == pytest.approx(111_319 * math.cos(math.radians(lat0)), rel=0.01)


def test_polygon_area_m2_matches_a_known_square():
    """A 100 m square must measure 10,000 m^2 through the lon/lat conversion."""
    lat = 30.955
    m_per_deg_lat = math.pi / 180 * 6378137.0
    dlat = 100.0 / m_per_deg_lat
    dlon = 100.0 / (m_per_deg_lat * math.cos(math.radians(lat)))
    square = box(-103.28, lat, -103.28 + dlon, lat + dlat)
    assert detect.polygon_area_m2(square) == pytest.approx(10_000, rel=0.001)


# --- detect_panel_rows: bearing ---------------------------------------------
#
# The bug these cover: the previous implementation asserted rows run exactly
# north-south "by construction of the north-up mosaic". Bearing is a property
# of the site layout, and this site's rows run 1.51 deg off grid north, which
# put the generated strips in the bare gaps between the real rows.

def _mosaic_frame(site_polygon, zoom=ZOOM):
    """Tile-aligned mosaic origin and size for a site, matching fetch_mosaic."""
    minx, miny, maxx, maxy = site_polygon.bounds
    x0f, y0f = st.deg2tile(minx, maxy, zoom)
    x1f, y1f = st.deg2tile(maxx, miny, zoom)
    tx0, ty0 = int(math.floor(x0f)), int(math.floor(y0f))
    tx1, ty1 = int(math.floor(x1f)), int(math.floor(y1f))
    width = (tx1 - tx0 + 1) * st.TILE_SIZE
    height = (ty1 - ty0 + 1) * st.TILE_SIZE
    return tx0, ty0, width, height


def _striped_array(width, height, bearing_deg, pitch_px=22, width_px=10, blank_band=None):
    """Bright ground with dark rows at `bearing_deg`, as a grey uint8 array.

    blank_band: optional (y_start, y_end) left entirely bright, standing in
    for an access road or the gap between two panel blocks.
    """
    shift = detect.shear_shift(height, bearing_deg)
    across = np.arange(width)[None, :] - shift[:, None]
    phase = across % pitch_px
    is_row = (phase <= width_px / 2) | (phase >= pitch_px - width_px / 2)
    grey = np.where(is_row, 60, 215).astype(np.uint8)
    if blank_band is not None:
        grey[blank_band[0]:blank_band[1], :] = 215
    return grey


def _patch_mosaic(monkeypatch, site_polygon, grey, tx0, ty0):
    image = Image.fromarray(grey, mode="L").convert("RGB")
    monkeypatch.setattr(st, "fetch_mosaic",
                        lambda *a, **k: (image, tx0, ty0, ZOOM))
    monkeypatch.setattr(detect.st, "fetch_mosaic",
                        lambda *a, **k: (image, tx0, ty0, ZOOM))


@pytest.fixture(scope="module")
def small_site():
    """A synthetic site shaped like the real one: long across the rows.

    The bearing estimate's precision comes from row length -- a row must run
    far enough for a wrong angle to walk it off the row and into the gap. Over
    the ~1.1 km of latitude here, a 0.25 deg error displaces a row by ~19 px
    against a 22 px pitch, so wrong angles smear and the true one stands out.
    A short test site cannot tell them apart at all: over 512 px every angle
    within 1 deg scores identically.
    """
    return box(-103.2820, 30.9520, -103.2800, 30.9620)


def _masks(site_polygon, grey, tx0, ty0):
    boundary_px = [st.lonlat_to_pixel(lon, lat, tx0, ty0, ZOOM)
                   for lon, lat in site_polygon.exterior.coords]
    inside = st.rasterize_polygon_mask((grey.shape[1], grey.shape[0]), boundary_px) > 0
    return (grey < detect.DEFAULT_PANEL_THRESHOLD) & inside, inside


@pytest.mark.parametrize("true_bearing", [-1.5, 0.0, 1.5])
def test_bearing_scan_recovers_a_known_tilt(small_site, true_bearing):
    tx0, ty0, width, height = _mosaic_frame(small_site)
    grey = _striped_array(width, height, true_bearing)
    panel, inside = _masks(small_site, grey, tx0, ty0)

    found, contrast = detect.estimate_bearing(panel, inside)
    assert found == pytest.approx(true_bearing, abs=0.05)
    assert contrast > 0.5


def test_contrast_collapses_at_the_wrong_bearing(small_site):
    """The discriminator that makes the bearing estimate unambiguous, and the
    signal the old 0-degree assumption was throwing away."""
    tx0, ty0, width, height = _mosaic_frame(small_site)
    grey = _striped_array(width, height, -1.5)
    panel, inside = _masks(small_site, grey, tx0, ty0)

    at_truth = detect.profile_contrast(detect.across_row_profile(panel, inside, -1.5)[0])
    at_zero = detect.profile_contrast(detect.across_row_profile(panel, inside, 0.0)[0])
    assert at_truth > 0.8
    assert at_truth > 4 * at_zero


def test_shear_shift_sign_is_clockwise_from_north():
    """A positive bearing must lean the row east as it runs north. Mosaic y
    increases southward, so the offset has to invert -- getting this backwards
    mislabels the reported bearing without changing the fit quality."""
    shift = detect.shear_shift(100, 1.0)
    assert shift[0] == 0
    # 99 px further south, the row has moved west by tan(1 deg) per pixel.
    assert shift[99] == pytest.approx(-99 * math.tan(math.radians(1.0)), abs=1.0)
    assert np.array_equal(detect.shear_shift(100, 0.0), np.zeros(100, dtype=int))


# --- detect_panel_rows: row centres and pitch -------------------------------

def test_dark_runs_finds_every_row_line():
    profile = np.array([0.0, 0.9, 0.9, 0.0, 0.0, 0.95, 0.95, 0.0])
    assert detect.dark_runs(profile, 0.5) == [(1, 3), (5, 7)]


def test_dark_runs_treats_nan_as_a_break():
    """NaN marks an across-row position with too little of the site behind it;
    it must not silently glue two rows into one."""
    profile = np.array([0.9, 0.9, np.nan, 0.9, 0.9])
    assert detect.dark_runs(profile, 0.5) == [(0, 2), (3, 5)]


def test_measure_pitch_is_unmoved_by_a_missing_row():
    """Real sites have missing rows and access roads, so some consecutive gaps
    are multiples of the pitch. A mean would be dragged up by them."""
    centres = [0, 22, 44, 110, 132, 154]   # one 3-pitch gap
    assert detect.measure_pitch(centres) == 22.0


def test_measure_pitch_needs_two_rows():
    assert detect.measure_pitch([5.0]) is None


# --- detect_panel_rows: along-row presence ----------------------------------
#
# The bug these cover: the lattice was extrapolated across the whole site
# polygon, so bare ground inside a hand-drawn boundary -- margins, access
# roads, gaps between panel blocks -- was labelled as panel.

def test_bare_band_along_a_row_is_rejected(small_site):
    tx0, ty0, width, height = _mosaic_frame(small_site)
    blank = (height // 3, height // 2)
    grey = _striped_array(width, height, 0.0, blank_band=blank)
    panel, inside = _masks(small_site, grey, tx0, ty0)

    profile, origin = detect.across_row_profile(panel, inside, 0.0)
    runs = detect.dark_runs(profile, detect.DEFAULT_MIN_DARK_FRACTION)
    assert runs, "no rows detected in the striped region"
    start, end = runs[len(runs) // 2]
    centre, half_width = (start + end - 1) / 2.0 - origin, (end - start) / 2.0

    bounds, fractions, coverages = detect.row_dark_fractions(
        detect.scanline_prefix_sums(panel, inside), panel.shape[1],
        centre, half_width, detect.shear_shift(height, 0.0), bin_px=20)
    keep = (coverages >= 0.5) & (fractions >= detect.DEFAULT_MIN_DARK_FRACTION)

    def bins_overlapping(y0, y1):
        return [i for i, (b0, b1) in enumerate(bounds) if b0 < y1 and b1 > y0]

    blanked = bins_overlapping(*blank)
    # Every fully-blank bin must be rejected...
    for i in blanked:
        if bounds[i][0] >= blank[0] and bounds[i][1] <= blank[1] and coverages[i] >= 0.5:
            assert not keep[i], f"bin {bounds[i]} is bare ground but was kept"
    # ...and the striped bins outside it must survive, or the test proves nothing.
    assert keep.sum() > 0


def test_merge_kept_bins_splits_a_row_at_the_gap():
    bounds = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)]
    keep = [True, True, False, True, True]
    assert detect.merge_kept_bins(bounds, keep) == [(0, 20), (30, 50)]


def test_merge_kept_bins_returns_nothing_when_all_rejected():
    assert detect.merge_kept_bins([(0, 10), (10, 20)], [False, False]) == []


# --- detect_panel_rows: end to end on synthetic imagery ---------------------

def test_detect_recovers_geometry_and_skips_the_gap(monkeypatch, small_site):
    """The whole detector against a mosaic whose answer we know: tilted rows
    at a known pitch, interrupted by a bare band."""
    tx0, ty0, width, height = _mosaic_frame(small_site)
    blank = (int(height * 0.40), int(height * 0.55))
    grey = _striped_array(width, height, -1.5, pitch_px=22, width_px=10, blank_band=blank)
    _patch_mosaic(monkeypatch, small_site, grey, tx0, ty0)

    segments, stats = detect.detect(small_site, zoom=ZOOM, verbose=False)

    mpp = detect.meters_per_pixel(small_site.centroid.y, ZOOM)
    assert stats["bearing_deg"] == pytest.approx(-1.5, abs=0.05)
    assert stats["pitch_m"] == pytest.approx(22 * mpp, rel=0.02)
    assert stats["row_width_m"] == pytest.approx(10 * mpp, rel=0.25)
    assert stats["profile_contrast"] > 0.8
    assert stats["bins_kept"] < stats["bins_considered"], "the bare band was not rejected"

    # Each row must come back as two runs -- above and below the bare band --
    # which is the behaviour a site-wide lattice cannot produce.
    per_row = {}
    for segment in segments:
        per_row.setdefault(segment["row_index"], []).append(segment)
    assert per_row
    assert max(len(v) for v in per_row.values()) >= 2

    # No segment may sit inside the bare band.
    for segment in segments:
        for lon, lat in segment["geometry"].exterior.coords:
            _, py = st.lonlat_to_pixel(lon, lat, tx0, ty0, ZOOM)
            assert not (blank[0] + 20 < py < blank[1] - 20), \
                "a segment was placed in the bare band"


def test_detect_stays_inside_the_site_boundary(monkeypatch, small_site):
    tx0, ty0, width, height = _mosaic_frame(small_site)
    grey = _striped_array(width, height, 0.0)
    _patch_mosaic(monkeypatch, small_site, grey, tx0, ty0)

    segments, _ = detect.detect(small_site, zoom=ZOOM, bearing_deg=0.0, verbose=False)
    assert segments
    for segment in segments:
        assert segment["geometry"].difference(small_site).area < 1e-12


def test_detect_drops_slivers_below_the_area_floor(monkeypatch, small_site):
    tx0, ty0, width, height = _mosaic_frame(small_site)
    grey = _striped_array(width, height, 0.0)
    _patch_mosaic(monkeypatch, small_site, grey, tx0, ty0)

    generous, _ = detect.detect(small_site, zoom=ZOOM, bearing_deg=0.0,
                                min_area_m2=0.0, verbose=False)
    areas = sorted(detect.polygon_area_m2(s["geometry"]) for s in generous)
    # Calibrate the floor off the actual output so the test cannot silently
    # become a no-op the way a hardcoded threshold can.
    floor = areas[len(areas) // 2]
    filtered, _ = detect.detect(small_site, zoom=ZOOM, bearing_deg=0.0,
                                min_area_m2=floor, verbose=False)
    assert len(filtered) < len(generous)
    assert all(detect.polygon_area_m2(s["geometry"]) >= floor for s in filtered)


def test_detect_refuses_featureless_imagery(monkeypatch, small_site):
    """Uniform ground must fail loudly rather than invent a lattice."""
    tx0, ty0, width, height = _mosaic_frame(small_site)
    grey = np.full((height, width), 215, dtype=np.uint8)
    _patch_mosaic(monkeypatch, small_site, grey, tx0, ty0)

    with pytest.raises(ValueError, match="no panel rows found"):
        detect.detect(small_site, zoom=ZOOM, bearing_deg=0.0, verbose=False)


# --- heat_risk: AOI buffering -----------------------------------------------
#
# FortyGuard returns whole grid cells and drops the ones the AOI only partly
# covers, so the mosaic stopped ~147 m short of this site's south edge and
# 9.4% of the polygon came back with no covering tile. The request polygon is
# grown so that shortfall lands outside the real site.

def _local_geometries(site_fc, buffered_fc):
    """Both polygons in metres, on one projection, for distance assertions."""
    site = shape(site_fc["features"][0]["geometry"])
    grown = shape(buffered_fc["features"][0]["geometry"])
    to_local, _ = make_projector(site.centroid.x, site.centroid.y)
    as_local = lambda g: Polygon([to_local(x, y) for x, y in g.exterior.coords])
    return as_local(site), as_local(grown)


def test_buffered_aoi_contains_the_whole_site():
    site_fc = json.loads(SITE_PATH.read_text())
    site, grown = _local_geometries(site_fc, heat_risk.buffer_aoi(site_fc, 600.0))
    assert grown.contains(site)


def test_buffer_grows_the_aoi_by_the_requested_distance():
    site_fc = json.loads(SITE_PATH.read_text())
    site, grown = _local_geometries(site_fc, heat_risk.buffer_aoi(site_fc, 600.0))
    # Closest approach of the two boundaries is the buffer distance itself;
    # mitred corners reach further out, capped by mitre_limit=2.
    assert grown.exterior.distance(site.exterior) == pytest.approx(600.0, rel=1e-3)
    assert max(site.exterior.distance(Point(c)) for c in grown.exterior.coords) <= 1200.0


def test_buffer_keeps_the_request_polygon_small():
    """Mitre joins, not the default round ones -- a round buffer would emit
    ~16 vertices per corner in the request payload."""
    site_fc = json.loads(SITE_PATH.read_text())
    grown = shape(heat_risk.buffer_aoi(site_fc, 600.0)["features"][0]["geometry"])
    site = shape(site_fc["features"][0]["geometry"])
    assert len(grown.exterior.coords) <= 2 * len(site.exterior.coords)


def test_zero_buffer_sends_the_site_unchanged():
    site_fc = json.loads(SITE_PATH.read_text())
    assert heat_risk.buffer_aoi(site_fc, 0.0) is site_fc


# --- heat_risk: field selection ---------------------------------------------

def test_value_field_is_keyed_off_the_analytic():
    """An exceedance tile that also carries a temperature must not be read as
    a temperature -- that was a silent wrong-metric bug."""
    props = {"tile_id": 1, "average_temperature": 35.5, "exceedance_hours": 2.7}
    assert heat_risk.pick_value_field(props, "exceedance") == "exceedance_hours"
    assert heat_risk.pick_value_field(props, "tcm") == "average_temperature"


def test_value_field_falls_back_to_generic_names():
    assert heat_risk.pick_value_field({"tile_id": 1, "value": 2.5}, "exceedance") == "value"


def test_value_field_falls_back_to_any_numeric():
    field = heat_risk.pick_value_field({"tile_id": 1, "something_odd": 4.0}, "exceedance")
    assert field == "something_odd"


def test_value_field_ignores_tile_id_and_booleans():
    assert heat_risk.pick_value_field({"tile_id": 7, "flag": True}, "exceedance") is None


# --- heat_risk: splitting rows at tile boundaries ---------------------------
#
# The bug these cover: each row was given the value of the single tile its
# centroid landed in. Rows here run up to ~330 m against 60 m tiles, so that
# used 22 of 176 returned tiles and never saw the site's hottest or coolest
# ground.

def _tile(tile_id, x0, y0, size, **props):
    return {"type": "Feature",
            "properties": {"tile_id": tile_id, **props},
            "geometry": mapping(box(x0, y0, x0 + size, y0 + size))}


def _row_feature(polygon, row_index=0):
    return {"type": "Feature",
            "properties": {"row_index": row_index},
            "geometry": mapping(polygon)}


# A degree-sized synthetic grid keeps every piece far above the 10 m^2 floor.
def test_a_row_crossing_tiles_is_split_and_reads_each_one():
    tiles = [_tile(0, 0, 0, 1, value=1.0),
             _tile(1, 0, 1, 1, value=2.0),
             _tile(2, 0, 2, 1, value=3.0)]
    row = _row_feature(box(0.4, 0.1, 0.6, 2.9))

    segments, field = heat_risk.segment_rows_by_tile([row], tiles, "exceedance")

    assert field == "value"
    assert len(segments) == 3
    assert sorted(s["value"] for s in segments) == [1.0, 2.0, 3.0]
    assert sorted(s["tile_id"] for s in segments) == [0, 1, 2]
    assert all(s["row_index"] == 0 for s in segments)


def test_centroid_lookup_would_have_missed_the_extremes():
    """States the defect directly: the old centroid rule saw one of these
    three values; splitting sees all three, so min and max survive."""
    tiles = [_tile(0, 0, 0, 1, value=1.0),
             _tile(1, 0, 1, 1, value=2.0),
             _tile(2, 0, 2, 1, value=3.0)]
    row = box(0.4, 0.1, 0.6, 2.9)

    segments, _ = heat_risk.segment_rows_by_tile([_row_feature(row)], tiles, "exceedance")
    values = [s["value"] for s in segments]
    assert min(values) == 1.0 and max(values) == 3.0

    centroid_tile = [t for t in tiles
                     if shape(t["geometry"]).contains(row.centroid)]
    assert len(centroid_tile) == 1
    assert centroid_tile[0]["properties"]["value"] == 2.0


def test_uncovered_part_of_a_row_is_unknown_not_dropped():
    """Ground with no tile behind it must stay on the map as unknown -- both
    silently dropping it and calling it safe would be wrong."""
    tiles = [_tile(0, 0, 0, 1, value=1.0)]
    row = _row_feature(box(0.4, 0.1, 0.6, 2.9))

    segments, _ = heat_risk.segment_rows_by_tile([row], tiles, "exceedance")
    covered = [s for s in segments if s["value"] is not None]
    uncovered = [s for s in segments if s["value"] is None]
    assert len(covered) == 1
    assert len(uncovered) == 1
    assert uncovered[0]["tile_id"] is None
    # The two together account for the whole row.
    total = sum(s["geometry"].area for s in segments)
    assert total == pytest.approx(shape(row["geometry"]).area, rel=1e-9)


def test_a_row_with_no_tile_at_all_is_one_unknown_segment():
    tiles = [_tile(0, 10, 10, 1, value=1.0)]
    segments, _ = heat_risk.segment_rows_by_tile(
        [_row_feature(box(0.4, 0.1, 0.6, 0.9))], tiles, "exceedance")
    assert len(segments) == 1
    assert segments[0]["value"] is None
    assert segments[0]["at_risk"] if False else segments[0]["tile_id"] is None


def test_sliver_segments_are_dropped():
    """A row barely clipping a neighbouring tile leaves a sliver that is not a
    unit of anything; it would inflate the at-risk denominator.

    Uses realistic degree scales: at this latitude one degree is ~100 km, so a
    fraction of a degree of overhang is not a sliver at all.
    """
    size = 0.0005                      # ~55 m tiles, close to the real 60 m
    overhang = 1e-7                    # ~1 cm across a ~55 m row -> well under 10 m^2
    tiles = [_tile(0, 0, 0, size, value=1.0), _tile(1, size, 0, size, value=9.0)]
    row = _row_feature(box(size / 2, 0.0001, size + overhang, 0.0004))

    generous, _ = heat_risk.segment_rows_by_tile([row], tiles, "exceedance", min_area_m2=0.0)
    filtered, _ = heat_risk.segment_rows_by_tile([row], tiles, "exceedance")

    assert 9.0 in [s["value"] for s in generous], "the overhang was not produced at all"
    assert 9.0 not in [s["value"] for s in filtered]
    assert len(generous) > len(filtered)


# --- heat_risk: classification ----------------------------------------------

def test_unscored_segments_are_unknown_not_safe():
    """Absence of a tile must never render as 'ok'."""
    tiles = [_tile(1, 0, 0, 1, value=5.0)]
    rows = [_row_feature(box(0.1, 0.1, 0.9, 0.9), 0),
            _row_feature(box(10, 10, 10.9, 10.9), 1)]
    segments, field = heat_risk.segment_rows_by_tile(rows, tiles, "exceedance")
    results = heat_risk.classify(segments, field, threshold=1.0)

    by_row = {r["properties"]["row_index"]: r["properties"] for r in results}
    assert by_row[0]["at_risk"] is True
    assert by_row[1]["at_risk"] is None
    assert by_row[1]["tile_value"] is None


def test_segments_record_tile_id_and_area_for_traceability():
    tiles = [_tile(42, 0, 0, 1, value=5.0)]
    segments, field = heat_risk.segment_rows_by_tile(
        [_row_feature(box(0.1, 0.1, 0.9, 0.9))], tiles, "exceedance")
    props = heat_risk.classify(segments, field, 1.0)[0]["properties"]
    assert props["tile_id"] == 42
    assert props["row_index"] == 0
    assert props["area_m2"] > 0


def test_summarize_excludes_no_data_from_the_denominator():
    tiles = [_tile(1, 0, 0, 1, value=5.0), _tile(2, 1, 0, 1, value=0.5)]
    rows = [_row_feature(box(0.1, 0.1, 0.9, 0.9), 0),
            _row_feature(box(1.1, 0.1, 1.9, 0.9), 1),
            _row_feature(box(9, 9, 9.9, 9.9), 2)]
    segments, field = heat_risk.segment_rows_by_tile(rows, tiles, "exceedance")
    at_risk, no_data, scored = heat_risk.summarize(heat_risk.classify(segments, field, 1.0))
    assert (at_risk, no_data, scored) == (1, 1, 2)


def test_classification_is_inclusive_at_the_threshold():
    tiles = [_tile(1, 0, 0, 1, value=2.5)]
    segments, field = heat_risk.segment_rows_by_tile(
        [_row_feature(box(0.1, 0.1, 0.9, 0.9))], tiles, "exceedance")
    assert heat_risk.classify(segments, field, 2.5)[0]["properties"]["at_risk"] is True


def test_area_accounting_matches_the_classification():
    tiles = [_tile(1, 0, 0, 1, value=5.0), _tile(2, 1, 0, 1, value=0.5)]
    rows = [_row_feature(box(0.1, 0.1, 0.9, 0.9), 0),
            _row_feature(box(1.1, 0.1, 1.9, 0.9), 1)]
    segments, field = heat_risk.segment_rows_by_tile(rows, tiles, "exceedance")
    results = heat_risk.classify(segments, field, 1.0)

    hot = heat_risk.area_m2_where(results, lambda v: v is True)
    scored = heat_risk.area_m2_where(results, lambda v: v is not None)
    assert 0 < hot < scored


# --- fortyguard_client: cache -----------------------------------------------

def test_cache_round_trips_and_is_payload_keyed(tmp_path):
    client = FortyGuardClient(api_key="test-key", api_base="https://example.invalid",
                              cache_dir=tmp_path)
    payload = {"granularity": 60, "date_time": {"start_date": "2024-07-15"}}

    assert client.cached_result("heatmap", payload) is None
    client.store_result("heatmap", payload, {"map_data": {"features": []}})
    assert client.cached_result("heatmap", payload) == {"map_data": {"features": []}}
    # A different request must not read the first one's result.
    assert client.cached_result("heatmap", {**payload, "granularity": 100}) is None


def test_cache_key_ignores_dict_ordering(tmp_path):
    client = FortyGuardClient(api_key="k", api_base="https://example.invalid", cache_dir=tmp_path)
    client.store_result("heatmap", {"a": 1, "b": 2}, {"ok": True})
    assert client.cached_result("heatmap", {"b": 2, "a": 1}) == {"ok": True}


def test_cache_never_writes_the_api_key(tmp_path):
    client = FortyGuardClient(api_key="super-secret-key", api_base="https://example.invalid",
                              cache_dir=tmp_path)
    client.store_result("heatmap", {"granularity": 60}, {"map_data": {}})
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "super-secret-key" not in path.read_text()


def test_corrupt_cache_entry_is_ignored_not_fatal(tmp_path):
    client = FortyGuardClient(api_key="k", api_base="https://example.invalid", cache_dir=tmp_path)
    payload = {"granularity": 60}
    client.store_result("heatmap", payload, {"ok": True})
    next(tmp_path.glob("heatmap_*.json")).write_text("{ truncated")
    assert client.cached_result("heatmap", payload) is None


def test_disabling_the_cache_bypasses_it(tmp_path):
    writer = FortyGuardClient(api_key="k", api_base="https://example.invalid", cache_dir=tmp_path)
    writer.store_result("heatmap", {"g": 1}, {"ok": True})
    reader = FortyGuardClient(api_key="k", api_base="https://example.invalid",
                              cache_dir=tmp_path, use_cache=False)
    assert reader.cached_result("heatmap", {"g": 1}) is None


def test_missing_credentials_raise_a_useful_message(monkeypatch):
    monkeypatch.delenv("FORTYGUARD_API_KEY", raising=False)
    monkeypatch.delenv("FORTYGUARD_API_BASE", raising=False)
    with pytest.raises(Exception) as excinfo:
        FortyGuardClient()
    assert "FORTYGUARD_API_KEY" in str(excinfo.value)


# --- committed artefacts ----------------------------------------------------

def test_committed_panel_rows_are_self_consistent(site):
    """The detection artefact is committed rather than regenerated (it needs
    imagery), so its own metadata is the only thing guarding it."""
    fc = json.loads(PANEL_ROWS_PATH.read_text())
    md, feats = fc["metadata"], fc["features"]

    assert md["n_segments"] == len(feats)
    assert md["n_row_lines"] == len({f["properties"]["row_index"] for f in feats})
    assert md["bins_kept"] <= md["bins_considered"]
    assert md["panel_area_m2"] == pytest.approx(
        sum(f["properties"]["area_m2"] for f in feats), rel=0.001)
    assert md["panel_area_fraction"] == pytest.approx(
        md["panel_area_m2"] / md["site_area_m2"], abs=0.001)

    for f in feats:
        assert f["properties"]["area_m2"] >= detect.MIN_SEGMENT_AREA_M2
        assert shape(f["geometry"]).difference(site).area < 1e-10


def test_committed_detection_fits_the_imagery_it_claims_to():
    """The bearing/contrast pair is the whole basis for trusting this geometry.
    A contrast this high cannot be produced by a mis-set bearing -- at 0 deg
    on this site it is 0.10 -- so it is the check that the old north-south
    assumption would have failed."""
    md = json.loads(PANEL_ROWS_PATH.read_text())["metadata"]
    assert md["profile_contrast"] > 0.8
    assert md["bearing_deg"] != 0.0

    # Detected panel area must be close to the imagery's own dark fraction;
    # the superseded lattice claimed 0.524 against a 0.439 dark fraction.
    assert md["panel_area_fraction"] == pytest.approx(md["imagery_dark_fraction"], abs=0.05)
    assert md["panel_area_fraction"] < md["imagery_dark_fraction"]


def test_committed_geometry_bearing_matches_the_emitted_polygons():
    """Guards the reported bearing's sign convention against the geometry it
    describes -- a sign error here is invisible in the fit quality."""
    fc = json.loads(PANEL_ROWS_PATH.read_text())
    reported = fc["metadata"]["bearing_deg"]

    longest = max(fc["features"], key=lambda f: shape(f["geometry"]).bounds[3]
                  - shape(f["geometry"]).bounds[1])
    ring = list(shape(longest["geometry"]).exterior.coords)[:-1]
    south = min(ring, key=lambda p: p[1])
    north = max(ring, key=lambda p: p[1])
    d_east = (north[0] - south[0]) * math.cos(math.radians(south[1]))
    d_north = north[1] - south[1]
    measured = math.degrees(math.atan2(d_east, d_north))
    assert measured == pytest.approx(reported, abs=0.1)


def test_committed_heat_risk_output_is_self_consistent():
    """The output's own metadata must agree with its features -- this is the
    file the demo and the README both quote from."""
    fc = json.loads(HEAT_RISK_PATH.read_text())
    md, feats = fc["metadata"], fc["features"]
    states = [f["properties"]["at_risk"] for f in feats]

    assert md["n_segments"] == len(feats)
    assert md["n_at_risk"] == states.count(True)
    assert md["n_no_data"] == sum(1 for v in states if v is None)
    assert md["n_scored"] == md["n_segments"] - md["n_no_data"]
    assert md["percentile_basis"] == "row-in-tile segments"

    for f in feats:
        p = f["properties"]
        # no-data and scored must be exactly complementary -- an earlier bug
        # had a segment with no tile_value still reporting a boolean verdict.
        assert (p["tile_value"] is None) == (p["at_risk"] is None)
        assert (p["tile_id"] is None) == (p["at_risk"] is None)


def test_committed_output_uses_every_tile_that_covers_the_site():
    """The regression this rework exists for: centroid lookup scored 1,244
    rows from 21 tiles. The AOI is buffered now, so most returned tiles fall
    outside the site by design and n_tiles_with_rows < n_tiles is expected --
    what must still hold is that the tiles actually used blanket the site,
    and that rows were split rather than point-sampled."""
    fc = json.loads(HEAT_RISK_PATH.read_text())
    md = fc["metadata"]
    site = shape(json.loads(SITE_PATH.read_text())["features"][0]["geometry"])
    used = {f["properties"]["tile_id"] for f in fc["features"]
            if f["properties"]["tile_id"] is not None}
    assert len(used) == md["n_tiles_with_rows"]
    # Rows are split at tile boundaries, not matched whole to one tile.
    assert md["n_segments"] > 2 * md["n_panel_rows"]
    # Whatever tiles were used have to add up to at least the site's area;
    # centroid lookup's 21 tiles covered a twelfth of it.
    assert len(used) * md["granularity_m"] ** 2 >= detect.polygon_area_m2(site)


def test_committed_output_has_no_unscored_segment_left():
    """What the AOI buffer bought: the unbuffered run left 173 segments --
    9.4% of the site, up to 147 m deep along the south edge -- with no
    covering tile. Grown past the boundary, the mosaic measures all of it."""
    md = json.loads(HEAT_RISK_PATH.read_text())["metadata"]
    assert md["aoi_buffer_m"] > 0
    assert md["n_no_data"] == 0
    assert md["n_scored"] == md["n_segments"]


def test_committed_output_spans_the_full_tile_range():
    """Centroid lookup silently clipped the site's true 1.98-3.11 h range to
    2.51-2.98 h. The scored segments must reach both ends again."""
    fc = json.loads(HEAT_RISK_PATH.read_text())
    values = [f["properties"]["tile_value"] for f in fc["features"]
              if f["properties"]["tile_value"] is not None]
    assert min(values) < 2.0
    assert max(values) > 3.1


def test_committed_output_matches_the_detection_it_records():
    """Guards the output drifting from the panel-row artefact it was built
    from -- the class of staleness that shipped superseded numbers before."""
    heat = json.loads(HEAT_RISK_PATH.read_text())
    rows = json.loads(PANEL_ROWS_PATH.read_text())
    assert heat["metadata"]["n_panel_rows"] == len(rows["features"])
    assert heat["metadata"]["row_geometry"] == rows["metadata"]


def test_committed_percentile_cutoff_matches_the_features():
    """The cutoff in the metadata must be the one that actually produced the
    at_risk flags, at the percentile it claims."""
    fc = json.loads(HEAT_RISK_PATH.read_text())
    md = fc["metadata"]
    values = [f["properties"]["tile_value"] for f in fc["features"]
              if f["properties"]["tile_value"] is not None]
    assert float(np.percentile(values, md["risk_percentile"])) == pytest.approx(
        md["percentile_cutoff"], abs=1e-6)
    for f in fc["features"]:
        p = f["properties"]
        if p["tile_value"] is not None:
            assert p["at_risk"] == (p["tile_value"] >= md["percentile_cutoff"])


def test_docs_data_js_matches_the_committed_output():
    """docs/data.js is a hand-run build step, so it can silently fall behind
    the GeoJSON it is supposed to bundle."""
    text = (ROOT / "docs" / "data.js").read_text()
    bundled = json.loads(text.split("const ROW_SEGMENTS = ", 1)[1].strip().rstrip(";"))
    assert bundled == json.loads(HEAT_RISK_PATH.read_text())
