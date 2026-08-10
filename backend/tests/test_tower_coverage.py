"""Tests for coverage-area-added scoring (services/tower_coverage.py) and its
wiring into process_and_rank's sort order."""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

import json

from retina_analytics.association import NodeGeometry

from services.tower_coverage import annotate_coverage_added
from services.tower_ranking import process_and_rank, reload_config

# Tiny grid per the spec: the test must run in milliseconds.
_MAX_RANGE_KM = 20.0
_GRID_STEP_KM = 5.0

# User RX at the equator/prime-meridian — cos(lat) == 1, so lat/lon degrees
# scale identically and the geometry below is easy to reason about in bearings.
_RX_LAT = 0.0
_RX_LON = 0.0


def _geo(node_id, beam_azimuth_deg, beam_width_deg, max_range_km=_MAX_RANGE_KM):
    """A monostatic-looking NodeGeometry sited at the user RX (tx == rx — the
    baseline is irrelevant here since max_bistatic_range_km is left None, so
    footprint_radius_km falls back to max_range_km regardless of tx)."""
    return NodeGeometry(
        node_id=node_id,
        rx_lat=_RX_LAT, rx_lon=_RX_LON, rx_alt_km=0.0,
        tx_lat=_RX_LAT, tx_lon=_RX_LON, tx_alt_km=0.0,
        beam_azimuth_deg=beam_azimuth_deg,
        beam_width_deg=beam_width_deg,
        max_range_km=max_range_km,
    )


def _towers(n=2):
    return [{"callsign": f"T{i}"} for i in range(n)]


class TestNoOp:
    def test_empty_towers_no_op(self):
        towers = []
        geo = _geo("a", 0.0, 80.0)
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, {"a": geo},
                                 grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        assert towers == []

    def test_empty_geometries_no_op(self):
        towers = _towers()
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, {},
                                 grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        for t in towers:
            assert "coverage_area_added_km2" not in t
            assert "coverage_area_n3_km2" not in t
            assert "coverage_best_azimuth_deg" not in t


class TestNoCoverageAnywhere:
    def test_fleet_entirely_out_of_reach_scores_zero(self):
        """A fleet node nowhere near the candidate disk covers none of its
        cells, so every cell is existing==0 and no azimuth can add n>=2 area."""
        towers = _towers()
        far_geo = _geo("far", 0.0, 80.0)
        far_geo.rx_lat, far_geo.rx_lon = 80.0, 80.0  # nowhere near the candidate disk
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, {"far": far_geo},
                                 grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        for t in towers:
            assert t["coverage_area_added_km2"] == 0.0
            assert t["coverage_area_n3_km2"] == 0.0
            assert isinstance(t["coverage_best_azimuth_deg"], float)


class TestThreeRegionGeometry:
    """Two fleet nodes carve the candidate disk into three bands by bearing
    from the user RX: [-20, 80) single-covered (node A only), [80, 140]
    double-covered (both A and B), and the remaining bearings uncovered."""

    def setup_method(self):
        self.node_a = _geo("a", beam_azimuth_deg=60.0, beam_width_deg=160.0)  # [-20, 140]
        self.node_b = _geo("b", beam_azimuth_deg=120.0, beam_width_deg=80.0)  # [80, 160]
        self.geometries = {"a": self.node_a, "b": self.node_b}

    def test_area_added_positive(self):
        towers = _towers()
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, self.geometries,
                                 grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        assert towers[0]["coverage_area_added_km2"] > 0

    def test_best_azimuth_points_into_single_covered_band(self):
        # Single-covered band spans bearings [-20, 80); its midpoint is 30.
        towers = _towers()
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, self.geometries,
                                 grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        az = towers[0]["coverage_best_azimuth_deg"]
        circular_diff = abs((az - 30.0 + 180) % 360 - 180)
        assert circular_diff <= 45.0

    def test_all_towers_get_identical_values(self):
        towers = _towers(3)
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, self.geometries,
                                 grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        added = {t["coverage_area_added_km2"] for t in towers}
        n3 = {t["coverage_area_n3_km2"] for t in towers}
        az = {t["coverage_best_azimuth_deg"] for t in towers}
        assert len(added) == 1
        assert len(n3) == 1
        assert len(az) == 1


class TestConcentricGeometryUpgradesN3:
    """One fleet node's whole footprint is a narrow wedge; a second node
    double-covers the middle of it. A candidate beam sized to the wedge can't
    avoid the doubly-covered core, so the winning azimuth carries n3 area too."""

    def test_n3_area_positive_when_unavoidable(self):
        outer = _geo("outer", beam_azimuth_deg=0.0, beam_width_deg=42.0)   # [-21, 21], n>=1
        inner = _geo("inner", beam_azimuth_deg=0.0, beam_width_deg=20.0)  # [-10, 10], n>=2 there
        towers = _towers()
        annotate_coverage_added(
            towers, _RX_LAT, _RX_LON, {"outer": outer, "inner": inner},
            grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM,
        )
        assert towers[0]["coverage_area_added_km2"] > 0
        assert towers[0]["coverage_area_n3_km2"] > 0


# ── process_and_rank integration ─────────────────────────────────────────────

_USER_LAT = 33.749
_USER_LON = -84.388


def _device(freq_mhz, lat, lon, callsign, eirp_watts=60.0):
    # "eirp" (watts) is what eirp_dbm_from_device actually reads — not
    # "eirp_dbm", which process_and_rank never looks at on the input side.
    return {
        "frequency": freq_mhz,
        "callsign": callsign,
        "antennaHeight": 100,
        "location": {
            "geom": f"POINT({lon} {lat})",
            "name": "Test Tower",
            "state": "GA",
        },
        "eirp": eirp_watts,
    }


def _system(devices):
    return {"licence": {"type": "Broadcast", "subtype": "FM"}, "devices": devices}


class TestProcessAndRankCoverageIntegration:
    def test_coverage_scorer_dominates_default_sort_order(self, tmp_path, monkeypatch):
        """Two co-located towers (same distance/band, so only EIRP drives
        received_power_dbm) — the higher-EIRP one sorts first under the
        pre-existing rules. A stub coverage_scorer assigns coverage inversely
        to that old order; with coverage_area_added_km2 prepended
        (descending) to sort_order, the weak tower must now sort first."""
        cfg = {
            "receiver": {"rx_antenna_gain_dbi": 6.0, "sensitivity_dbm": -120.0},
            "broadcast_bands": {"FM": [[87.8, 108.0]]},
            "ranking": {
                "band_priority": {"FM": 0},
                "distance_classes": [{"label": "Ideal", "min_km": 0, "max_km": None}],
                "distance_priority": {"Ideal": 0},
                "sort_order": [
                    {"field": "coverage_area_added_km2", "ascending": False},
                    {"field": "band_priority", "ascending": True},
                    {"field": "distance_priority", "ascending": True},
                    {"field": "received_power_dbm", "ascending": False},
                ],
            },
            "search": {"default_radius_km": 80, "default_limit": 20},
        }
        fake_path = tmp_path / "tower_config.json"
        fake_path.write_text(json.dumps(cfg))

        import services.tower_ranking as tower_ranking

        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", fake_path)
        try:
            reload_config()

            # Same location → identical distance_km/fspl, so received_power_dbm
            # differs only by EIRP.
            strong = _device(95.5, 33.85, -84.388, callsign="KSTRONG", eirp_watts=10000.0)
            weak = _device(95.5, 33.85, -84.388, callsign="KWEAK", eirp_watts=10.0)
            raw = [_system([strong, weak])]

            def stub_scorer(towers):
                for t in towers:
                    t["coverage_area_added_km2"] = 500.0 if t["callsign"] == "KWEAK" else 10.0

            # Sanity check: without the scorer, the strong tower wins on
            # received_power_dbm (the old, coverage-less default order).
            baseline = process_and_rank(raw, _USER_LAT, _USER_LON)
            assert baseline[0]["callsign"] == "KSTRONG"

            result = process_and_rank(raw, _USER_LAT, _USER_LON, coverage_scorer=stub_scorer)
            assert result[0]["callsign"] == "KWEAK", (
                "coverage_area_added_km2 is prepended to sort_order, so the "
                "stub-scored weak tower must now outrank the strong one"
            )
        finally:
            from core.runtime_config import runtime_path
            monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", runtime_path("tower_config.json"))
            reload_config()

    def test_coverage_scorer_exception_does_not_break_ranking(self):
        """A coverage_scorer that raises must not prevent towers from being
        returned — scoring is best-effort, wrapped in try/except."""
        device = _device(95.5, 33.93, -84.388, callsign="WXYZ")
        raw = [_system([device])]

        def broken_scorer(towers):
            raise RuntimeError("boom")

        result = process_and_rank(raw, _USER_LAT, _USER_LON, coverage_scorer=broken_scorer)
        assert len(result) == 1
        assert result[0]["callsign"] == "WXYZ"
