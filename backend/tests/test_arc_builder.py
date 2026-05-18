"""
Unit tests for _build_single_node_arc() and the single_node_ellipse_arc
aircraft JSON path in frame_processor.py.

These tests run standalone (no live server required).
"""

import math

import pytest

from services.frame_processor import _bearing_deg, _build_single_node_arc, _enu_to_lla

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


# ─── Regression: gates revert position but preserve arc ──────────────────────

class TestGatePreservesArc:
    """Speed and RMS gates must keep ambiguity_arc on the wire.

    On a previous iteration both gates set ambiguity_arc = None when the
    measurement looked noisy.  That made the testmap look like every node
    had gone idle whenever a single bad frame came through (~90% of
    synthetic single-node tracks routinely sit above the 7 µs RMS threshold).
    The gates now revert only the displayed lat/lon to the last good emit;
    the arc itself is still emitted so the user can see what the radar saw.
    """

    HEX = "rgtest"

    def _setup_state(self, *, rms_delay, prev_lat, prev_lon, now_lat, now_lon,
                     prev_age_s=120.0):
        import time as _time

        from core import state
        from pipeline.passive_radar import GeolocatedTrack

        # Geolocated track positioned where this frame's solver landed.
        track = GeolocatedTrack(
            track_id=f"track-{self.HEX}",
            lat=now_lat,
            lon=now_lon,
            alt_m=3000,
            vel_east=100.0,
            vel_north=50.0,
            vel_up=0.0,
            rms_delay=rms_delay,
            rms_doppler=1.0,
            n_detections=10,
            timestamp_ms=int(_time.time() * 1000),
            adsb_hex=None,
            latest_delay_us=80.0,
            target_class="aircraft",
        )
        # Match the wall-clock so the staleness filter doesn't drop us.
        track.wall_clock_ts = _time.time()

        node_cfg = dict(_NODE_CFG)
        state.active_geo_aircraft[self.HEX] = (track, node_cfg)
        # Last good emit anchored a bit away from the current position.
        # prev_age_s defaults to 120 s so the speed gate (which only acts on
        # 0 < dt < 60 s) doesn't fire on the test inputs unless we want it to.
        state.track_last_emit[self.HEX] = [
            prev_lat, prev_lon, _time.time() - prev_age_s,
        ]
        return track

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        from core import state
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def _build(self):
        import types

        from services.frame_processor import build_combined_aircraft_json
        # Minimal pipeline shim — build_combined_aircraft_json reads
        # default_pipeline.geolocated_tracks and .config only.
        pipeline = types.SimpleNamespace(geolocated_tracks={}, config=dict(_NODE_CFG))
        result = build_combined_aircraft_json(pipeline)
        return next((a for a in result["aircraft"] if a["hex"] == self.HEX), None)

    # _NODE_CFG beam_azimuth auto-computes from RX→TX bearing + 90° → roughly
    # 220° (south-west), so both prev and current positions live south-west
    # of the RX to stay inside the beam.
    PREV_LAT, PREV_LON = 33.70, -84.85
    NOW_LAT, NOW_LON = 33.65, -84.95

    def test_rms_gate_keeps_arc_and_reverts_lat_lon(self):
        self._setup_state(rms_delay=12.5, prev_lat=self.PREV_LAT, prev_lon=self.PREV_LON,
                          now_lat=self.NOW_LAT, now_lon=self.NOW_LON)
        ac = self._build()
        assert ac is not None, "aircraft must still appear in feed"
        # Arc preserved — the whole point of this regression test.
        assert ac["ambiguity_arc"] is not None
        assert len(ac["ambiguity_arc"]) >= 2
        # Position reverted to last good emit.
        assert ac["lat"] == self.PREV_LAT
        assert ac["lon"] == self.PREV_LON
        assert ac["position_source"] == "single_node_ellipse_arc"

    def test_clean_rms_keeps_arc_and_new_position(self):
        # Sanity baseline: when rms_delay is well within tolerance the gate
        # never fires and lat/lon track the current solve.
        self._setup_state(rms_delay=1.2, prev_lat=self.PREV_LAT, prev_lon=self.PREV_LON,
                          now_lat=self.NOW_LAT, now_lon=self.NOW_LON)
        ac = self._build()
        assert ac is not None
        assert ac["ambiguity_arc"] is not None
        # New emit, not reverted to last_emit.
        assert (ac["lat"], ac["lon"]) != (self.PREV_LAT, self.PREV_LON)
        assert ac["position_source"] == "single_node_ellipse_arc"

    def test_speed_gate_keeps_arc_and_reverts_lat_lon(self):
        # Speed gate fires when arc midpoint jumps > 800 m/s from last emit
        # within the last 60 s.  Set prev_age = 1 s with ~10 km of move so
        # the gate trips on speed alone (rms_delay stays clean).
        self._setup_state(rms_delay=1.2, prev_lat=self.PREV_LAT, prev_lon=self.PREV_LON,
                          now_lat=self.NOW_LAT, now_lon=self.NOW_LON,
                          prev_age_s=1.0)
        ac = self._build()
        assert ac is not None
        # Arc preserved even though the gate fired.
        assert ac["ambiguity_arc"] is not None
        # Position reverted to last good emit.
        assert ac["lat"] == self.PREV_LAT
        assert ac["lon"] == self.PREV_LON
        assert ac["position_source"] == "single_node_ellipse_arc"


# ─── Regression: arc-motion gs/heading fallback ──────────────────────────────

class TestArcMotionVelocityFallback:
    """When ADS-B is unavailable and the LM solver gives track.speed_knots
    ~ 0 (typical for synthetic single-node tracks), the gs/heading fields
    must be reconstructed from arc-midpoint displacement so the aircraft
    visibly moves on the map.
    """

    HEX = "amvtst"

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        from core import state
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_arc_motion.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_arc_motion.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def test_estimator_returns_none_without_log(self):
        from services.frame_processor import _estimate_velocity_from_motion
        assert _estimate_velocity_from_motion("nope", 33.7, -84.85, 1_700_000_000) is None

    def test_estimator_returns_gs_and_track_from_two_samples(self):
        from core import state
        from services.frame_processor import _estimate_velocity_from_motion
        # Aircraft was at (33.70, -84.85) 40 s ago, now at (33.75, -84.80) —
        # roughly 7 km north-east → ~340 kt heading ~45°.
        now = 1_700_000_000.0
        state.track_arc_motion[self.HEX] = [(33.70, -84.85, now - 40.0)]
        result = _estimate_velocity_from_motion(self.HEX, 33.75, -84.80, now)
        assert result is not None
        gs, track_deg = result
        assert 200 < gs < 500
        # Heading ENU: dlat>0 (north), dlon>0 (east) → bearing in [0, 90°]
        assert 30 < track_deg < 60

    def test_estimator_rejects_too_recent(self):
        from core import state
        from services.frame_processor import _estimate_velocity_from_motion
        now = 1_700_000_000.0
        # Sample 5 s old — below the 15 s minimum window.
        state.track_arc_motion[self.HEX] = [(33.70, -84.85, now - 5.0)]
        assert _estimate_velocity_from_motion(self.HEX, 33.75, -84.80, now) is None

    def test_estimator_rejects_supersonic_clamp(self):
        from core import state
        from services.frame_processor import _estimate_velocity_from_motion
        now = 1_700_000_000.0
        # 100 km in 20 s = 5000 m/s = ~9700 kt — way above the 800 kt clamp.
        state.track_arc_motion[self.HEX] = [(33.70, -84.85, now - 20.0)]
        assert _estimate_velocity_from_motion(self.HEX, 34.60, -84.85, now) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
