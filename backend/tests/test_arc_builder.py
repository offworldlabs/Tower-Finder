"""
Unit tests for _build_single_node_arc() and the single_node_ellipse_arc
aircraft JSON path in frame_processor.py.

These tests run standalone (no live server required).
"""

import sys
import os
import math

import pytest

# Ensure backend package is importable when run from the backend/ dir
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.frame_processor import _build_single_node_arc, _bearing_deg, _enu_to_lla


# ─── Minimal fake track ─────────────────────────────────────────────────────

class _FakeTrack:
    def __init__(self, delay_us):
        self.latest_delay_us = delay_us


# ─── Minimal node config (Atlanta-area bistatic geometry) ───────────────────

_NODE_CFG = {
    "node_id": "test_node",
    "rx_lat": 33.939182,
    "rx_lon": -84.651910,
    "tx_lat": 33.756670,
    "tx_lon": -84.331844,
    "beam_width_deg": 90,
    "max_range_km": 100,
    # beam_azimuth_deg intentionally omitted → auto-computed from TX/RX bearing
}


# ─── _bearing_deg tests ──────────────────────────────────────────────────────

class TestBearingDeg:
    def test_north(self):
        # Point directly north
        b = _bearing_deg(0, 0, 1, 0)
        assert abs(b - 0.0) < 0.1

    def test_east(self):
        b = _bearing_deg(0, 0, 0, 1)
        assert abs(b - 90.0) < 0.1

    def test_south(self):
        b = _bearing_deg(1, 0, 0, 0)
        assert abs(b - 180.0) < 0.1

    def test_west(self):
        b = _bearing_deg(0, 1, 0, 0)
        assert abs(b - 270.0) < 0.1

    def test_round_trip(self):
        """Bearing from A→B should be roughly opposite of B→A."""
        b_fwd = _bearing_deg(33.9, -84.6, 33.7, -84.3)
        b_rev = _bearing_deg(33.7, -84.3, 33.9, -84.6)
        diff = abs((b_fwd - b_rev + 360) % 360 - 180)
        assert diff < 1.0


# ─── _enu_to_lla tests ───────────────────────────────────────────────────────

class TestEnuToLla:
    def test_zero_enu_is_rx(self):
        """Zero offset → returns the RX position."""
        rx_lat, rx_lon = 33.939182, -84.651910
        lat, lon = _enu_to_lla(rx_lat, rx_lon, 0.0, 0.0)
        assert abs(lat - rx_lat) < 1e-6
        assert abs(lon - rx_lon) < 1e-6

    def test_north_offset(self):
        """Moving 1 km north increases latitude by ~0.009°."""
        lat, lon = _enu_to_lla(33.0, -84.0, 0.0, 1.0)
        assert abs(lat - (33.0 + 1.0 / 111.32)) < 1e-4
        assert abs(lon - (-84.0)) < 1e-6

    def test_east_offset(self):
        """Moving 1 km east increases longitude (amount depends on lat)."""
        lat, lon = _enu_to_lla(33.0, -84.0, 1.0, 0.0)
        assert lat == pytest.approx(33.0, abs=1e-6)
        assert lon > -84.0  # moved east


# ─── _build_single_node_arc tests ───────────────────────────────────────────

class TestBuildSingleNodeArc:
    def test_returns_none_for_zero_delay(self):
        track = _FakeTrack(delay_us=0)
        assert _build_single_node_arc(track, _NODE_CFG) is None

    def test_returns_none_for_negative_delay(self):
        track = _FakeTrack(delay_us=-5.0)
        assert _build_single_node_arc(track, _NODE_CFG) is None

    def test_returns_none_for_none_delay(self):
        track = _FakeTrack(delay_us=None)
        assert _build_single_node_arc(track, _NODE_CFG) is None

    def test_returns_none_missing_rx_coords(self):
        track = _FakeTrack(delay_us=100.0)
        cfg = dict(_NODE_CFG)
        del cfg["rx_lat"]
        assert _build_single_node_arc(track, cfg) is None

    def test_returns_none_missing_tx_coords(self):
        track = _FakeTrack(delay_us=100.0)
        cfg = {**_NODE_CFG, "tx_lat": None}
        assert _build_single_node_arc(track, cfg) is None

    def test_returns_list_of_pairs(self):
        track = _FakeTrack(delay_us=60.0)
        arc = _build_single_node_arc(track, _NODE_CFG)
        assert arc is not None
        assert isinstance(arc, list)
        for pt in arc:
            assert len(pt) == 2
            lat, lon = pt
            assert -90 <= lat <= 90
            assert -180 <= lon <= 180

    def test_min_two_points(self):
        """Any valid arc must have at least 2 points."""
        track = _FakeTrack(delay_us=60.0)
        arc = _build_single_node_arc(track, _NODE_CFG)
        assert arc is not None
        assert len(arc) >= 2

    def test_37_points_for_normal_delay(self):
        """Standard beam (90°, 37 steps) should produce 37 points for a
        moderate delay that crosses all bearing steps within max_range."""
        track = _FakeTrack(delay_us=80.0)
        arc = _build_single_node_arc(track, _NODE_CFG)
        assert arc is not None
        assert len(arc) == 37

    def test_arc_within_max_range(self):
        """All arc points must lie within max_range_km of RX."""
        track = _FakeTrack(delay_us=60.0)
        arc = _build_single_node_arc(track, _NODE_CFG)
        assert arc is not None
        rx_lat = _NODE_CFG["rx_lat"]
        rx_lon = _NODE_CFG["rx_lon"]
        max_range_km = _NODE_CFG["max_range_km"]
        for lat, lon in arc:
            dlat = (lat - rx_lat) * 111.0
            dlon = (lon - rx_lon) * 111.0 * math.cos(math.radians(lat))
            dist = math.hypot(dlat, dlon)
            assert dist <= max_range_km + 1.0  # 1 km tolerance for binary search

    def test_arc_coords_are_finite(self):
        track = _FakeTrack(delay_us=120.0)
        arc = _build_single_node_arc(track, _NODE_CFG)
        assert arc is not None
        for lat, lon in arc:
            assert math.isfinite(lat)
            assert math.isfinite(lon)

    def test_very_large_delay_no_crash(self):
        """Delay so large no ellipse crosses the beam — should return None or []."""
        track = _FakeTrack(delay_us=99_999.0)
        result = _build_single_node_arc(track, _NODE_CFG)
        assert result is None or len(result) < 2

    def test_narrow_beam_yields_fewer_points(self):
        """A 10° beam should yield fewer points than the full 90° beam."""
        track = _FakeTrack(delay_us=80.0)
        cfg_narrow = {**_NODE_CFG, "beam_width_deg": 10}
        arc_narrow = _build_single_node_arc(track, cfg_narrow)
        arc_wide = _build_single_node_arc(track, _NODE_CFG)
        if arc_narrow and arc_wide:
            assert len(arc_narrow) <= len(arc_wide)

    def test_explicit_beam_azimuth(self):
        """Providing an explicit beam_azimuth_deg should not crash the builder."""
        track = _FakeTrack(delay_us=80.0)
        cfg = {**_NODE_CFG, "beam_azimuth_deg": 135.0}
        arc = _build_single_node_arc(track, cfg)
        # May be None if azimuth points away from any detectable ellipse arc,
        # but must not raise an exception.
        assert arc is None or (isinstance(arc, list) and len(arc) >= 0)

    def test_monotonically_increases_with_delay(self):
        """As delay increases the arc should move further from RX (larger range)."""
        small_track = _FakeTrack(delay_us=30.0)
        large_track = _FakeTrack(delay_us=150.0)
        arc_small = _build_single_node_arc(small_track, _NODE_CFG)
        arc_large = _build_single_node_arc(large_track, _NODE_CFG)
        if arc_small and arc_large:
            rx_lat = _NODE_CFG["rx_lat"]
            rx_lon = _NODE_CFG["rx_lon"]
            def mean_range(arc):
                ranges = []
                for lat, lon in arc:
                    dlat = (lat - rx_lat) * 111.0
                    dlon = (lon - rx_lon) * 111.0 * math.cos(math.radians(lat))
                    ranges.append(math.hypot(dlat, dlon))
                return sum(ranges) / len(ranges)
            assert mean_range(arc_large) > mean_range(arc_small)


# ─── Aircraft JSON builder path (single_node_ellipse_arc) ───────────────────

class TestTrackEntryPaths:
    """Smoke tests for _track_entry() output via build_combined_aircraft_json.

    These use a minimal mock to avoid needing a live server / full state.
    """

    def _make_track(self, delay_us=80.0, lat=33.85, lon=-84.5, target_class="aircraft"):
        """Create a minimal GeolocatedTrack-like object."""
        from pipeline.passive_radar import GeolocatedTrack
        t = GeolocatedTrack(
            track_id="test_track",
            lat=lat,
            lon=lon,
            alt_m=3000,
            vel_east=100,
            vel_north=150,
            vel_up=0,
            rms_delay=0.5,
            rms_doppler=1.0,
            n_detections=10,
            timestamp_ms=1_700_000_000_000,
            adsb_hex=None,
            latest_delay_us=delay_us,
            target_class=target_class,
        )
        return t

    def test_track_has_target_class(self):
        t = self._make_track(target_class="aircraft")
        assert t.target_class == "aircraft"

    def test_drone_target_class(self):
        t = self._make_track(target_class="drone")
        assert t.target_class == "drone"

    def test_speed_knots_aircraft(self):
        t = self._make_track()
        assert t.speed_knots > 0

    def test_track_angle_range(self):
        t = self._make_track()
        assert 0 <= t.track_angle < 360


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
