"""Regression tests for the geometry, classification, and client-cache logic.

Deliberately offline: nothing here touches the FortyGuard API (the account is
capped at 30 heatmap requests/day). Tile data is synthesised or read from the
committed site boundary.
"""
import json
import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box, mapping, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import heat_risk
import measure_row_geometry as mrg
import numpy as np
import pv_thermal
from fortyguard_client import FortyGuardClient
from geo_utils import make_projector
from panel_rows import MIN_ROW_AREA_M2, generate_rows

SITE_PATH = ROOT / "data" / "geojson" / "solar_form_2.geojson"


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


# --- panel_rows: pitch, width, phase, area filter ---------------------------

def _local_polys(site, rows):
    to_local, _ = make_projector(site.centroid.x, site.centroid.y)
    return [Polygon([to_local(x, y) for x, y in r.exterior.coords]) for r in rows]


def test_rows_lie_inside_the_site(site):
    rows = generate_rows(site, 5.38, 2.82, 0.0, 4.86)
    assert rows
    for row in rows:
        # A tiny tolerance covers the lon/lat <-> metre round trip.
        assert row.difference(site).area < 1e-12


def _full_width_strips(site, rows, width):
    """Only strips the site boundary did not clip in x. Clipped strips have a
    clamped left/right bound and a shifted centroid, so they say nothing about
    the underlying lattice -- these are the ones that do."""
    return [p for p in _local_polys(site, rows)
            if abs((p.bounds[2] - p.bounds[0]) - width) < 1e-6]


def _lattice_phase(poly, pitch, offset):
    centre = (poly.bounds[0] + poly.bounds[2]) / 2
    phase = (centre - offset) % pitch
    return min(phase, pitch - phase)


def test_row_spacing_matches_the_requested_pitch(site):
    pitch, width = 5.38, 2.82
    strips = _full_width_strips(site, generate_rows(site, pitch, width, 0.0, 4.86), width)
    centres = sorted((p.bounds[0] + p.bounds[2]) / 2 for p in strips)
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    assert gaps
    # Clipping can drop a strip entirely, leaving a gap that is a multiple of
    # the pitch -- every gap must still be a whole number of pitches.
    for gap in gaps:
        assert gap / pitch == pytest.approx(round(gap / pitch), abs=0.01)


def test_row_offset_shifts_the_grid(site):
    """The phase argument must actually move the rows -- the bug it fixes was
    a grid silently anchored to the projection origin, which put every strip
    at an arbitrary position relative to the real panel rows."""
    pitch, width = 5.38, 2.82
    for offset in (0.0, 2.0, 4.86):
        strips = _full_width_strips(site, generate_rows(site, pitch, width, 0.0, offset), width)
        assert strips
        for poly in strips:
            assert _lattice_phase(poly, pitch, offset) < 1e-6

    # ...and a different offset really does produce a different lattice.
    shifted = _full_width_strips(site, generate_rows(site, pitch, width, 0.0, 2.0), width)
    assert max(_lattice_phase(p, pitch, 0.0) for p in shifted) > 1.0


def test_orientation_rotates_the_rows(site):
    ns = generate_rows(site, 5.38, 2.82, 0.0, 4.86)
    ew = generate_rows(site, 5.38, 2.82, 90.0, 4.86)
    assert len(ns) != len(ew)
    ns_local, ew_local = _local_polys(site, ns), _local_polys(site, ew)
    # North-south rows are tall and narrow; east-west rows are wide and short.
    def aspect(p):
        minx, miny, maxx, maxy = p.bounds
        return (maxy - miny) / (maxx - minx)
    assert np.median([aspect(p) for p in ns_local]) > 5
    assert np.median([aspect(p) for p in ew_local]) < 0.2


def test_min_area_filter_drops_slivers(site):
    """Clipping leaves corner slivers that are not rows; they inflated the
    at-risk denominator before the filter existed."""
    unfiltered = _local_polys(site, generate_rows(site, 5.38, 2.82, 0.0, 4.86, min_area_m2=0.0))
    filtered = _local_polys(site, generate_rows(site, 5.38, 2.82, 0.0, 4.86))
    assert min(p.area for p in unfiltered) < MIN_ROW_AREA_M2
    assert min(p.area for p in filtered) >= MIN_ROW_AREA_M2
    assert len(filtered) < len(unfiltered)


def test_row_width_is_respected(site):
    width = 2.82
    for poly in _local_polys(site, generate_rows(site, 5.38, width, 0.0, 4.86)):
        minx, _, maxx, _ = poly.bounds
        assert maxx - minx <= width + 1e-6


# --- measure_row_geometry: phase recovery -----------------------------------

def _synthetic_profile(n, pitch, width, phase):
    """Bright background with a dark band of `width` px centred every `pitch`
    px, the first centred at `phase`."""
    profile = np.ones(n)
    for i in range(n):
        d = (i - phase) % pitch
        if min(d, pitch - d) <= width / 2:
            profile[i] = -1.0
    return profile


@pytest.mark.parametrize("phase", [0, 3, 7, 10, 15, 20])
def test_dark_centre_bin_recovers_known_phase(phase):
    pitch, width = 21, 11
    profile = _synthetic_profile(2000, pitch, width, phase)
    cycle = mrg.fold_cycle(profile, pitch, slice(0, 2000))
    found = mrg.dark_centre_bin(cycle, width)
    err = min((found - phase) % pitch, (phase - found) % pitch)
    assert err <= 1.0, f"phase {phase} recovered as {found}"


def test_dark_centre_bin_handles_wraparound():
    """The dark run straddling the end of the bin array is exactly what a
    non-circular estimator gets wrong."""
    pitch, width = 21, 9
    cycle = mrg.fold_cycle(_synthetic_profile(2000, pitch, width, 0), pitch, slice(0, 2000))
    found = mrg.dark_centre_bin(cycle, width)
    assert min(found, pitch - found) <= 1.0


def test_width_fraction_matches_the_dark_share():
    pitch, width = 21, 11
    cycle = mrg.fold_cycle(_synthetic_profile(2000, pitch, width, 5), pitch, slice(0, 2000))
    assert mrg.width_fraction(cycle) == pytest.approx(width / pitch, abs=0.06)


def test_estimate_pitch_finds_the_known_period():
    pitch = 21
    profile = _synthetic_profile(4000, pitch, 11, 4)
    found, confidence = mrg.estimate_pitch_px(profile)
    assert found == pitch
    assert confidence > 0.5


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


# --- heat_risk: matching and classification ---------------------------------

def _tile(tile_id, x0, y0, size, **props):
    return {"type": "Feature",
            "properties": {"tile_id": tile_id, **props},
            "geometry": mapping(box(x0, y0, x0 + size, y0 + size))}


def test_unmatched_rows_are_unknown_not_safe():
    """Absence of a tile must never render as 'ok' -- the original code set
    at_risk=False for rows it could not evaluate at all."""
    tiles = [_tile(1, 0, 0, 1, value=5.0)]
    rows = [box(0.1, 0.1, 0.2, 0.2), box(10, 10, 10.1, 10.1)]
    matches, field = heat_risk.match_rows_to_tiles(rows, tiles, "exceedance")
    results = heat_risk.classify(matches, field, threshold=1.0)

    assert results[0]["properties"]["at_risk"] is True
    assert results[1]["properties"]["at_risk"] is None
    assert results[1]["properties"]["tile_value"] is None


def test_matches_record_tile_id_for_traceability():
    tiles = [_tile(42, 0, 0, 1, value=5.0)]
    matches, _ = heat_risk.match_rows_to_tiles([box(0.1, 0.1, 0.2, 0.2)], tiles, "exceedance")
    assert matches[0]["tile_id"] == 42


def test_summarize_excludes_no_data_from_the_denominator():
    tiles = [_tile(1, 0, 0, 1, value=5.0), _tile(2, 1, 0, 1, value=0.5)]
    rows = [box(0.1, 0.1, 0.2, 0.2), box(1.1, 0.1, 1.2, 0.2), box(9, 9, 9.1, 9.1)]
    matches, field = heat_risk.match_rows_to_tiles(rows, tiles, "exceedance")
    at_risk, no_data, scored = heat_risk.summarize(heat_risk.classify(matches, field, 1.0))
    assert (at_risk, no_data, scored) == (1, 1, 2)


def test_classification_is_inclusive_at_the_threshold():
    tiles = [_tile(1, 0, 0, 1, value=2.5)]
    matches, field = heat_risk.match_rows_to_tiles([box(0.1, 0.1, 0.2, 0.2)], tiles, "exceedance")
    assert heat_risk.classify(matches, field, 2.5)[0]["properties"]["at_risk"] is True


def test_pv_classification_marks_unmatched_rows_unknown():
    tiles = [_tile(1, 0, 0, 1, average_temperature=35.5)]
    rows = [box(0.1, 0.1, 0.2, 0.2), box(9, 9, 9.1, 9.1)]
    matches, _ = heat_risk.match_rows_to_tiles(rows, tiles, "tcm")
    results = heat_risk.classify_pv(matches, 864.0, 44.0, -0.38, 85.0)
    assert results[0]["properties"]["cell_temperature_c"] is not None
    assert results[1]["properties"]["at_risk"] is None
    assert results[1]["properties"]["cell_temperature_c"] is None


def test_row_weighted_percentile_differs_from_tile_weighted():
    """Documents why the label matters: one hot tile covering many rows moves
    the row-weighted cutoff but barely moves the tile-weighted one."""
    values = [3.0] * 30 + [1.0, 1.1, 1.2, 1.3]
    assert float(np.percentile(values, 75)) != pytest.approx(
        float(np.percentile(sorted(set(values)), 75)))


# --- pv_thermal -------------------------------------------------------------

def test_cell_temperature_at_noct_reference_conditions():
    """At 800 W/m^2 the cell sits (NOCT - 20) above ambient, by definition."""
    assert pv_thermal.cell_temperature(20.0, 800.0, 44.0) == pytest.approx(44.0)


def test_cell_temperature_equals_ambient_without_irradiance():
    assert pv_thermal.cell_temperature(35.5, 0.0) == pytest.approx(35.5)


def test_power_loss_is_zero_at_stc_and_negative_when_hot():
    assert pv_thermal.power_loss_percent(25.0) == pytest.approx(0.0)
    assert pv_thermal.power_loss_percent(61.4) < 0


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

def test_committed_panel_rows_match_the_current_defaults(site):
    """Guards the stale-artefact failure: the committed row file was generated
    with superseded pitch/width defaults and silently contradicted the code."""
    committed = json.loads((ROOT / "data" / "geojson" / "solar_form_2_panel_rows.geojson").read_text())
    assert len(committed["features"]) == len(generate_rows(site, 5.38, 2.82, 0.0, 4.86))


def test_committed_row_geometry_matches_the_defaults():
    """The measured geometry file and the CLI defaults must not drift apart."""
    measured = json.loads((ROOT / "data" / "geojson" / "solar_form_2_row_geometry.json").read_text())
    parser_defaults = {a.dest: a.default for a in _heat_risk_parser()._actions}
    assert parser_defaults["row_pitch"] == measured["pitch_m"]
    assert parser_defaults["row_width"] == measured["width_m"]
    assert parser_defaults["row_offset"] == measured["offset_m"]


def _heat_risk_parser():
    import argparse
    import contextlib
    import io
    holder = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, *a, **k):
        holder["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
            heat_risk.main()
    finally:
        argparse.ArgumentParser.parse_args = original
    return holder["parser"]
