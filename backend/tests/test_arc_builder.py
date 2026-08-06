"""
Unit tests for _build_single_node_arc() and the single_node_ellipse_arc
aircraft JSON path in frame_processor.py.

These tests run standalone (no live server required).
"""

import math

import pytest

import services.frame_processor as _fp
from config.constants import (
    ARC_MIN_DIFFERENTIAL_KM,
    DISPLAY_STALE_TRACK_S,
    GATE_MAX_HOLD_S,
)
from core import state
from services.frame_processor import _bearing_deg, _build_single_node_arc, _enu_to_lla

# ─── Minimal fake track ─────────────────────────────────────────────────────

class _FakeTrack:
    def __init__(self, delay_us, alt_m=None):
        self.latest_delay_us = delay_us
        self.alt_m = alt_m


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


# ─── New contract: arc spans the entire detection area ───────────────────────


def _arc_bearings(arc, rx_lat=_NODE_CFG["rx_lat"], rx_lon=_NODE_CFG["rx_lon"]):
    """Bearings (deg from RX) of every arc point."""
    return [_bearing_deg(rx_lat, rx_lon, lat, lon) for lat, lon in arc]


def _angular_spread_deg(bearings):
    """Widest angular extent covered by a bearing list (wrap-safe)."""
    spread = 0.0
    for a in bearings:
        for b in bearings:
            d = abs((a - b + 180.0) % 360.0 - 180.0)
            spread = max(spread, d)
    return spread


class TestArcSpansDetectionArea:
    """The emitted arc is the delay ellipse clipped to the node's detection
    area — NOT trimmed to a blip around the track's own position estimate,
    and NOT clipped to anything narrower than the beam the node genuinely has.
    """

    # Broadside boresight for _NODE_CFG (RX→TX bearing + 90°).
    BORESIGHT = (_bearing_deg(
        _NODE_CFG["rx_lat"], _NODE_CFG["rx_lon"],
        _NODE_CFG["tx_lat"], _NODE_CFG["tx_lon"]) + 90.0) % 360.0

    def test_known_position_no_longer_trims_the_arc(self):
        """A track with a known position used to get a ~25 km blip; the arc
        must now span the full 90° wedge regardless."""
        cfg, touched = dict(_NODE_CFG), set()
        track = _ArcTrack(80.0, 33.65, -84.95)
        arc = _fp._cached_single_node_arc("spanhex", track, cfg, touched)
        assert arc is not None
        spread = _angular_spread_deg(_arc_bearings(arc))
        # Old behaviour: min(90°, ~36°) ≈ 36° sweep.  New: the whole wedge.
        assert spread > 80.0

    def test_omni_node_spans_beyond_any_wedge(self):
        """beam_width >= 360 means omnidirectional: the arc is the full closed
        ellipse, including bearings far outside the old broadside wedge."""
        cfg = {**_NODE_CFG, "beam_width_deg": 360,
               "max_bistatic_range_km": 100.0}
        arc = _build_single_node_arc(80.0, cfg)
        assert arc is not None
        assert len(arc) == 73  # 72 steps + closing point
        # Ring closes on itself.
        assert arc[0][0] == pytest.approx(arc[-1][0], abs=1e-6)
        assert arc[0][1] == pytest.approx(arc[-1][1], abs=1e-6)
        # Points exist well outside the old default wedge around boresight.
        offsets = [abs((b - self.BORESIGHT + 180.0) % 360.0 - 180.0)
                   for b in _arc_bearings(arc)]
        assert max(offsets) > 150.0

    def test_wedge_arc_stays_inside_genuine_beam(self):
        """A genuinely directional node still yields a wedge-clipped arc —
        the detection area itself is a wedge there."""
        cfg = {**_NODE_CFG, "beam_width_deg": 48,
               "max_bistatic_range_km": 100.0}
        arc = _build_single_node_arc(80.0, cfg)
        assert arc is not None
        offsets = [abs((b - self.BORESIGHT + 180.0) % 360.0 - 180.0)
                   for b in _arc_bearings(arc)]
        assert max(offsets) <= 24.0 + 0.5

    def test_delay_beyond_differential_limit_returns_none(self):
        """80 µs is ~24 km of differential range — past a 20 km limit the
        node cannot have made this detection, so there is no arc."""
        cfg = {**_NODE_CFG, "max_bistatic_range_km": 20.0}
        assert _build_single_node_arc(80.0, cfg) is None

    def test_arc_points_lie_on_measured_differential_within_limit(self):
        """Every emitted point sits on the measured-delay ellipse, hence
        inside the declared differential-range limit."""
        from services.geo import C_KM_US, bistatic_differential_km
        limit = 40.0
        cfg = {**_NODE_CFG, "beam_width_deg": 360,
               "max_bistatic_range_km": limit}
        delay_us = 80.0
        arc = _build_single_node_arc(delay_us, cfg)
        assert arc is not None
        expected_km = delay_us * C_KM_US
        for lat, lon in arc:
            d = bistatic_differential_km(
                cfg["tx_lat"], cfg["tx_lon"], cfg["rx_lat"], cfg["rx_lon"],
                lat, lon)
            # ENU-vs-spherical mismatch stays well under 1 km at these ranges.
            assert d == pytest.approx(expected_km, abs=1.0)
            assert d <= limit

    def test_monostatic_range_excludes_out_of_reach_bearings(self):
        """Without a bistatic limit the monostatic circle clips the locus:
        bearings whose ellipse crossing lies beyond max_range_km are dropped,
        the rest survive."""
        cfg = {**_NODE_CFG, "max_range_km": 25}
        arc = _build_single_node_arc(80.0, cfg)
        assert arc is not None
        # Part of the wedge is out of reach — the arc must be a strict subset…
        assert 2 <= len(arc) < 37
        # …and every surviving point is inside the circle.
        for lat, lon in arc:
            dlat = (lat - cfg["rx_lat"]) * 111.0
            dlon = (lon - cfg["rx_lon"]) * 111.0 * math.cos(math.radians(lat))
            assert math.hypot(dlat, dlon) <= 25.0 + 1.0

    def test_point_budget_is_bounded(self):
        """The arc ships every feed tick: <= 37 points for a wedge, <= 73 for
        the full closed ellipse."""
        wedge = _build_single_node_arc(80.0, dict(_NODE_CFG))
        assert wedge is not None and len(wedge) <= 37
        omni = _build_single_node_arc(
            80.0, {**_NODE_CFG, "beam_width_deg": 360,
                   "max_bistatic_range_km": 100.0})
        assert omni is not None and len(omni) <= 73


# ─── Altitude-corrected delay→locus conversion ───────────────────────────────


def _differential_3d_km(lat, lon, alt_m, cfg):
    """Ground-truth-style 3-D differential for a point at altitude alt_m.

    Same ENU frame the builder uses; RX/TX altitudes from the node config
    (feet ASL, absent means sea level).
    """
    from services.geo import enu_km
    e_km, n_km = enu_km(cfg["rx_lat"], cfg["rx_lon"], lat, lon)
    te_km, tn_km = enu_km(cfg["rx_lat"], cfg["rx_lon"], cfg["tx_lat"], cfg["tx_lon"])
    rx_alt_km = float(cfg.get("rx_alt_ft") or 0.0) * 0.3048 / 1000.0
    tx_alt_km = float(cfg.get("tx_alt_ft") or 0.0) * 0.3048 / 1000.0
    h_km = alt_m / 1000.0
    r_rx = math.sqrt(e_km**2 + n_km**2 + (h_km - rx_alt_km) ** 2)
    r_tx = math.sqrt((e_km - te_km) ** 2 + (n_km - tn_km) ** 2 + (h_km - tx_alt_km) ** 2)
    baseline_3d = math.sqrt(te_km**2 + tn_km**2 + (tx_alt_km - rx_alt_km) ** 2)
    return r_rx + r_tx - baseline_3d


def _range_from_rx_km(lat, lon, cfg):
    from services.geo import enu_km
    e_km, n_km = enu_km(cfg["rx_lat"], cfg["rx_lon"], lat, lon)
    return math.hypot(e_km, n_km)


class TestAltitudeCorrectedArc:
    """FIX A: the measured delay is a 3-D differential (the simulator computes
    it from full ENU including target altitude), so solving a 2-D ground-plane
    ellipse drew the arc median 3.6 km too far out (up to 14.9 km); ground
    truth sat median 2.48 km inside it.  With the track's altitude known, the
    per-bearing solve now uses the 3-D differential at that altitude.
    """

    CFG = {**_NODE_CFG, "rx_alt_ft": 1000.0, "tx_alt_ft": 1600.0}
    DELAY_US = 80.0
    ALT_M = 9000.0

    def test_high_altitude_arc_lies_inside_2d_arc(self):
        """The 3-D arc must sit closer to RX than the equivalent 2-D arc, by
        roughly the altitude geometry (~2 km for 9 km ASL at this baseline)."""
        from services.geo import C_KM_US
        arc_2d = _build_single_node_arc(self.DELAY_US, self.CFG)
        arc_3d = _build_single_node_arc(self.DELAY_US, self.CFG, alt_m=self.ALT_M)
        assert arc_2d is not None and arc_3d is not None
        assert len(arc_2d) == len(arc_3d) == 37  # same bearing sweep
        shrinks = []
        for (lat2, lon2), (lat3, lon3) in zip(arc_2d, arc_3d):
            r2 = _range_from_rx_km(lat2, lon2, self.CFG)
            r3 = _range_from_rx_km(lat3, lon3, self.CFG)
            assert r3 < r2  # strictly inside on every bearing
            shrinks.append(r2 - r3)
        # Altitude geometry: each leg shortens by ~h²/2R → ~2 km total here.
        assert min(shrinks) > 0.5 and max(shrinks) < 8.0
        # Sanity: the delay still describes a real locus.
        assert self.DELAY_US * C_KM_US > ARC_MIN_DIFFERENTIAL_KM

    def test_arc_points_satisfy_3d_differential(self):
        """Ground-truth-style check: every point of the altitude-corrected
        arc, lifted to the target altitude, reproduces the measured
        differential."""
        from services.geo import C_KM_US
        measured_km = self.DELAY_US * C_KM_US
        arc = _build_single_node_arc(self.DELAY_US, self.CFG, alt_m=self.ALT_M)
        assert arc is not None
        for lat, lon in arc:
            d = _differential_3d_km(lat, lon, self.ALT_M, self.CFG)
            assert d == pytest.approx(measured_km, abs=0.02)

    def test_2d_arc_fails_3d_differential(self):
        """The bias being fixed: the old 2-D arc's points, lifted to the
        target altitude, overshoot the measured differential (staging: by
        2.4–3.3 km at this geometry) — they are NOT on the 3-D ellipse."""
        from services.geo import C_KM_US
        measured_km = self.DELAY_US * C_KM_US
        arc_2d = _build_single_node_arc(self.DELAY_US, self.CFG)
        assert arc_2d is not None
        for lat, lon in arc_2d:
            d = _differential_3d_km(lat, lon, self.ALT_M, self.CFG)
            assert d > measured_km + 1.0

    def test_track_altitude_is_used(self):
        """A track carrying alt_m must solve identically to the explicit
        alt_m argument."""
        via_track = _build_single_node_arc(_FakeTrack(self.DELAY_US, alt_m=self.ALT_M), self.CFG)
        via_arg = _build_single_node_arc(self.DELAY_US, self.CFG, alt_m=self.ALT_M)
        assert via_track == via_arg
        assert via_track is not None

    def test_no_altitude_falls_back_to_2d(self):
        """A track with no altitude (None/0 — radar-only pr* tracks) must get
        exactly the old 2-D ground-plane arc, not an invented altitude."""
        arc_2d = _build_single_node_arc(self.DELAY_US, self.CFG)
        assert arc_2d is not None
        assert _build_single_node_arc(_FakeTrack(self.DELAY_US), self.CFG) == arc_2d
        assert _build_single_node_arc(_FakeTrack(self.DELAY_US, alt_m=0.0), self.CFG) == arc_2d
        assert _build_single_node_arc(self.DELAY_US, self.CFG, alt_m=None) == arc_2d

    def test_inconsistent_delay_and_altitude_yield_no_arc(self):
        """A differential smaller than the 3-D minimum any point at the
        claimed altitude can produce (12 µs ≈ 3.6 km at 12 km ASL) means the
        delay and altitude are mutually inconsistent — no honest arc exists."""
        cfg = {**self.CFG, "beam_width_deg": 360, "max_bistatic_range_km": 100.0}
        assert _build_single_node_arc(12.0, cfg, alt_m=12000.0) is None

    def test_small_differential_high_altitude_outer_crossing(self):
        """When the sub-RX point falls outside the ellipsoid's altitude-plane
        cross-section (small differential, high altitude) the per-bearing
        bisection must restart its bracket at the dip and still land exactly
        on the 3-D locus for the bearings that reach it."""
        from services.geo import C_KM_US
        cfg = {**self.CFG, "beam_width_deg": 360, "max_bistatic_range_km": 100.0}
        delay_us, alt_m = 20.0, 10000.0
        arc = _build_single_node_arc(delay_us, cfg, alt_m=alt_m)
        assert arc is not None and len(arc) >= 2
        for lat, lon in arc:
            d = _differential_3d_km(lat, lon, alt_m, cfg)
            assert d == pytest.approx(delay_us * C_KM_US, abs=0.02)


# ─── Differential-range floor (blob-stub suppression) ────────────────────────


class TestArcDifferentialFloor:
    """FIX C: a differential range below ARC_MIN_DIFFERENTIAL_KM means the
    delay ellipse collapses onto the TX–RX baseline and the "arc" renders as
    a misleading blob stub (staging: 36/415 emitted arcs were <5 km stubs,
    median differential 2.6 km, many from clutter).  No arc is emitted below
    the floor; the track itself still emits (position, no arc) — see
    TestFloorTrackStillEmits.
    """

    def test_below_floor_returns_none(self):
        # 8 µs ≈ 2.40 km differential — under the 3.0 km floor.
        assert _build_single_node_arc(8.0, _NODE_CFG) is None
        assert _build_single_node_arc(_FakeTrack(8.0), _NODE_CFG) is None

    def test_above_floor_builds(self):
        # 11 µs ≈ 3.30 km — clear of the floor; full wedge arc.
        arc = _build_single_node_arc(11.0, _NODE_CFG)
        assert arc is not None
        assert len(arc) >= 2

    def test_floor_boundary(self):
        from services.geo import C_KM_US
        floor_delay_us = ARC_MIN_DIFFERENTIAL_KM / C_KM_US
        assert _build_single_node_arc(floor_delay_us * 0.999, _NODE_CFG) is None
        assert _build_single_node_arc(floor_delay_us * 1.001, _NODE_CFG) is not None


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
    """The two gates treat the arc differently — deliberately.

    Speed gate: keeps ambiguity_arc on the wire.  A speed-gate revert
    distrusts the association of the arc midpoint to *this track*, not the
    measurement itself, so "radar saw something along this curve" stays
    honest.  (Nulling it on a previous iteration made the testmap look like
    every node had gone idle whenever a single bad frame came through.)

    RMS gate: suppresses the arc for the frame.  rms_delay above the gate is
    the pipeline itself flagging the measurement as unfittable — staging
    diagnosis: mis-associated delays carried rms_delay 11–21 µs against the
    7 µs gate and their wrong-target arcs still drew (42% of ground-truth
    checks failed the on-ellipse test).  The position-revert behaviour is
    unchanged; only the arc is withheld.
    """

    HEX = "rgtest"

    def _setup_state(self, *, rms_delay, prev_lat, prev_lon, now_lat, now_lon,
                     prev_age_s=120.0):
        import time as _time

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
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
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

    def test_rms_gate_suppresses_arc_and_reverts_lat_lon(self):
        """Superseded intent: this test originally asserted the RMS gate
        keeps the arc.  FIX B inverted that on the staging diagnosis quoted
        in the class docstring — an rms_delay past the gate means the
        pipeline distrusts the measurement itself, so emitting geometry
        built from that measurement drew wrong-target arcs.  The gate now
        suppresses the arc for the frame while reverting the position
        exactly as before."""
        self._setup_state(rms_delay=12.5, prev_lat=self.PREV_LAT, prev_lon=self.PREV_LON,
                          now_lat=self.NOW_LAT, now_lon=self.NOW_LON)
        ac = self._build()
        assert ac is not None, "aircraft must still appear in feed"
        # Arc suppressed — the measurement is the thing the gate distrusts.
        assert ac["ambiguity_arc"] is None
        # Position reverted to last good emit, unchanged from before.
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
        # Arc preserved even though the gate fired — the speed gate distrusts
        # the midpoint association, not the measurement, so unlike the RMS
        # gate it must NOT suppress the arc.
        assert ac["ambiguity_arc"] is not None
        assert len(ac["ambiguity_arc"]) >= 2
        # Position reverted to last good emit.
        assert ac["lat"] == self.PREV_LAT
        assert ac["lon"] == self.PREV_LON
        assert ac["position_source"] == "single_node_ellipse_arc"


# ─── Regression: gates must not latch ────────────────────────────────────────

class TestGateHoldIsBounded:
    """A gate may suppress a bad frame; it may not freeze the track forever.

    Both gates revert the emitted position to ``track_last_emit`` — and the
    emit path then writes that reverted value *back* into ``track_last_emit``
    with a fresh timestamp.  That made both gates absorbing states: every
    subsequent frame was compared against a frozen reference, so it kept
    reverting indefinitely.  Measured on staging before the fix: every
    single-node arc track pinned in place for up to 141 s while its aircraft
    flew on (error growing at the target's own ground speed), then a 19.6 km
    teleport when the gate finally cleared.

    The hold is now bounded by GATE_MAX_HOLD_S, and the held position is
    dead-reckoned from the track's velocity so it coasts rather than freezes.
    """

    HEX = "gholdt"

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()

    PREV_LAT, PREV_LON = 33.70, -84.85
    NOW_LAT, NOW_LON = 33.65, -84.95

    def _setup(self, *, rms_delay, hold_age_s=None):
        """Install a track whose RMS sits above the gate threshold.

        ``hold_age_s`` backdates the gate-hold anchor to simulate a hold that
        has already been running that long.
        """
        import time as _time

        from pipeline.passive_radar import GeolocatedTrack

        track = GeolocatedTrack(
            track_id=f"track-{self.HEX}",
            lat=self.NOW_LAT,
            lon=self.NOW_LON,
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
        track.wall_clock_ts = _time.time()
        state.active_geo_aircraft[self.HEX] = (track, dict(_NODE_CFG))
        # prev_age 120 s keeps the speed gate (0 < dt < 60) out of it, so this
        # exercises the RMS gate in isolation.
        state.track_last_emit[self.HEX] = [
            self.PREV_LAT, self.PREV_LON, _time.time() - 120.0,
        ]
        if hold_age_s is not None:
            state.track_gate_hold[self.HEX] = (
                _time.time() - hold_age_s, self.PREV_LAT, self.PREV_LON,
            )
        return track

    def _build(self):
        import types

        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config=dict(_NODE_CFG))
        result = build_combined_aircraft_json(pipeline)
        return next((a for a in result["aircraft"] if a["hex"] == self.HEX), None)

    def test_hold_engages_and_records_anchor(self):
        self._setup(rms_delay=12.5)
        ac = self._build()
        assert ac is not None
        # First held frame still reverts, exactly as before.
        assert (ac["lat"], ac["lon"]) == (self.PREV_LAT, self.PREV_LON)
        # ...but now records when the hold started, so it can be bounded.
        assert self.HEX in state.track_gate_hold

    def test_hold_releases_after_max_hold(self):
        # The bug: this stayed at PREV forever.  Past GATE_MAX_HOLD_S the
        # measurement is accepted — a noisy position beats a confidently
        # wrong stationary one.
        self._setup(rms_delay=12.5, hold_age_s=GATE_MAX_HOLD_S + 5.0)
        ac = self._build()
        assert ac is not None
        assert (ac["lat"], ac["lon"]) != (self.PREV_LAT, self.PREV_LON)

    def test_expired_hold_also_restores_arc(self):
        # FIX B ties arc suppression to the same condition that reverts the
        # position (RMS distrust while the hold is live).  Past
        # GATE_MAX_HOLD_S the measurement is accepted — position AND arc —
        # for the same reason: noisy beats confidently absent.
        self._setup(rms_delay=12.5, hold_age_s=GATE_MAX_HOLD_S + 5.0)
        ac = self._build()
        assert ac is not None
        assert ac["ambiguity_arc"] is not None

    def test_held_position_coasts_instead_of_freezing(self):
        # Mid-hold the icon must advance along the track's velocity rather
        # than sit still while the aircraft flies away.
        self._setup(rms_delay=12.5, hold_age_s=5.0)
        ac = self._build()
        assert ac is not None
        assert (ac["lat"], ac["lon"]) != (self.PREV_LAT, self.PREV_LON)
        # vel_north is +50 m/s, so it must have moved north of the anchor.
        assert ac["lat"] > self.PREV_LAT

    def test_hold_clears_when_gate_stops_firing(self):
        self._setup(rms_delay=12.5, hold_age_s=3.0)
        self._build()
        assert self.HEX in state.track_gate_hold
        # Same track, now with clean RMS — the hold must not persist.
        self._setup(rms_delay=1.2, hold_age_s=3.0)
        ac = self._build()
        assert ac is not None
        assert self.HEX not in state.track_gate_hold

    def test_stale_track_is_not_rendered(self):
        # A track with no fresh detections used to be painted at its last
        # position for STALE_TRACK_S (120 s).  It must stop rendering once it
        # passes DISPLAY_STALE_TRACK_S, while the tracker keeps its state for
        # re-acquisition.
        import time as _time

        track = self._setup(rms_delay=1.2)
        track.wall_clock_ts = _time.time() - (DISPLAY_STALE_TRACK_S + 5.0)
        assert self._build() is None
        assert self.HEX in state.active_geo_aircraft

    def test_seen_reports_real_age(self):
        # "seen" was hardcoded to 0, so a frozen track claimed to be fresh and
        # no downstream staleness check could ever catch it.
        import time as _time

        track = self._setup(rms_delay=1.2)
        track.wall_clock_ts = _time.time() - 6.0
        ac = self._build()
        assert ac is not None
        assert 5.0 <= ac["seen"] <= 7.5


# ─── Below-floor differential: track emits, arc does not ─────────────────────


class TestFloorTrackStillEmits:
    """FIX C, feed side: a track whose differential sits under the arc floor
    keeps emitting — position only, no arc.  The pre-existing 'valid delay
    but no arc → suppress track' rule is for solves outside the antenna
    beam; a below-floor measurement is fine, only its arc is withheld.
    """

    HEX = "flrtst"

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        for d in (state.active_geo_aircraft, state.track_last_emit,
                  state.track_gate_hold, state.adsb_aircraft,
                  state.external_adsb_cache, state.multinode_tracks,
                  state.track_histories):
            d.clear()
        yield
        for d in (state.active_geo_aircraft, state.track_last_emit,
                  state.track_gate_hold, state.adsb_aircraft,
                  state.external_adsb_cache, state.multinode_tracks,
                  state.track_histories):
            d.clear()

    def _setup(self, delay_us):
        import time as _time

        from pipeline.passive_radar import GeolocatedTrack
        track = GeolocatedTrack(
            track_id=f"track-{self.HEX}",
            lat=33.65, lon=-84.95, alt_m=3000,
            vel_east=0.0, vel_north=0.0, vel_up=0.0,
            rms_delay=1.0, rms_doppler=1.0, n_detections=10,
            timestamp_ms=int(_time.time() * 1000),
            adsb_hex=None, latest_delay_us=delay_us, target_class="aircraft",
        )
        track.wall_clock_ts = _time.time()
        state.active_geo_aircraft[self.HEX] = (track, dict(_NODE_CFG))

    def _build(self):
        import types

        from services.frame_processor import build_combined_aircraft_json
        pipeline = types.SimpleNamespace(geolocated_tracks={}, config=dict(_NODE_CFG))
        result = build_combined_aircraft_json(pipeline)
        return next((a for a in result["aircraft"] if a["hex"] == self.HEX), None)

    def test_below_floor_track_emits_without_arc(self):
        # 8 µs ≈ 2.40 km differential < 3.0 km floor.
        self._setup(delay_us=8.0)
        ac = self._build()
        assert ac is not None, "below-floor track must still emit"
        assert ac["ambiguity_arc"] is None
        # No arc midpoint to snap to → the solver position is displayed.
        assert ac["position_source"] == "solver_single_node"
        assert ac["lat"] == pytest.approx(33.65, abs=1e-4)
        assert ac["lon"] == pytest.approx(-84.95, abs=1e-4)

    def test_above_floor_track_gets_arc(self):
        # Same track, honest differential — arc present, midpoint displayed.
        self._setup(delay_us=80.0)
        ac = self._build()
        assert ac is not None
        assert ac["ambiguity_arc"] is not None
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
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.track_arc_motion.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.track_arc_motion.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def test_estimator_returns_none_without_log(self):
        from services.frame_processor import _estimate_velocity_from_motion
        assert _estimate_velocity_from_motion("nope", 33.7, -84.85, 1_700_000_000) is None

    def test_estimator_returns_gs_and_track_from_two_samples(self):
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
        from services.frame_processor import _estimate_velocity_from_motion
        now = 1_700_000_000.0
        # Sample 5 s old — below the 15 s minimum window.
        state.track_arc_motion[self.HEX] = [(33.70, -84.85, now - 5.0)]
        assert _estimate_velocity_from_motion(self.HEX, 33.75, -84.80, now) is None

    def test_estimator_accepts_supersonic_displacement(self):
        """Supersonic targets are in scope: the estimator must report a fast
        displacement as-is rather than reject it as implausible.  (The 340 m/s
        upper bound this used to assert was removed deliberately.)"""
        from services.frame_processor import _estimate_velocity_from_motion
        now = 1_700_000_000.0
        # 100 km north in 20 s = 5000 m/s = ~9700 kt.
        state.track_arc_motion[self.HEX] = [(33.70, -84.85, now - 20.0)]
        est = _estimate_velocity_from_motion(self.HEX, 34.60, -84.85, now)
        assert est is not None
        gs, track_deg = est
        assert gs == pytest.approx(5000 * 1.94384, rel=0.02)
        assert track_deg == pytest.approx(0.0, abs=2.0)


# ─── Regression: position-jump (teleport) anomaly ────────────────────────────

class TestPositionJumpAnomaly:
    """A track whose emitted position teleports across the map must be flagged
    is_anomalous with a "position_jump" type — even without ADS-B and even
    when the tracker itself saw nothing wrong.  The speed gate only reverts
    arc-track positions (silently) and is skipped at dt ≥ 60 s; multinode
    tracks have no gate at all.
    """

    HEX = "jmptst"

    def _setup(self, *, now_lat, now_lon, prev_lat, prev_lon, prev_age_s):
        import time as _time

        from pipeline.passive_radar import GeolocatedTrack
        track = GeolocatedTrack(
            track_id=f"track-{self.HEX}",
            lat=now_lat, lon=now_lon, alt_m=3000,
            vel_east=0.0, vel_north=0.0, vel_up=0.0,
            rms_delay=1.0, rms_doppler=1.0, n_detections=10,
            timestamp_ms=int(_time.time() * 1000),
            adsb_hex=None, latest_delay_us=80.0, target_class="aircraft",
        )
        track.wall_clock_ts = _time.time()
        state.active_geo_aircraft[self.HEX] = (track, dict(_NODE_CFG))
        state.track_last_emit[self.HEX] = [prev_lat, prev_lon, _time.time() - prev_age_s]

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        for d in (state.active_geo_aircraft, state.track_last_emit,
                  state.track_arc_motion, state.adsb_aircraft,
                  state.external_adsb_cache, state.multinode_tracks,
                  state.track_histories):
            d.clear()
        state.anomaly_hexes.discard(self.HEX)
        yield
        for d in (state.active_geo_aircraft, state.track_last_emit,
                  state.track_arc_motion, state.adsb_aircraft,
                  state.external_adsb_cache, state.multinode_tracks,
                  state.track_histories):
            d.clear()
        state.anomaly_hexes.discard(self.HEX)

    def _build(self):
        import types

        from services.frame_processor import build_combined_aircraft_json
        pipeline = types.SimpleNamespace(geolocated_tracks={}, config=dict(_NODE_CFG))
        result = build_combined_aircraft_json(pipeline)
        return next((a for a in result["aircraft"] if a["hex"] == self.HEX), None)

    def test_teleport_flagged(self):
        # Previous emit ~200 km south, 90 s ago (dt ≥ 60 so the speed gate is
        # skipped → the jump is emitted, not reverted).  Current arc midpoint
        # lands SW of the Atlanta RX.  Absolute leap >> 30 km → flagged.
        self._setup(now_lat=33.70, now_lon=-84.85,
                    prev_lat=31.90, prev_lon=-84.85, prev_age_s=90.0)
        ac = self._build()
        assert ac is not None
        assert ac["is_anomalous"] is True
        assert "position_jump" in ac["anomaly_types"]
        assert self.HEX in state.anomaly_hexes

    def test_normal_motion_not_flagged(self):
        # Previous emit ~1 km away, 3 s ago → ~330 kt, well within normal.
        self._setup(now_lat=33.70, now_lon=-84.85,
                    prev_lat=33.691, prev_lon=-84.85, prev_age_s=3.0)
        ac = self._build()
        assert ac is not None
        assert "position_jump" not in (ac["anomaly_types"] or [])
        assert self.HEX not in state.anomaly_hexes


# ─── Single-node arc cache ───────────────────────────────────────────────────


class _ArcTrack:
    """Minimal track exposing the fields the arc cache fingerprints."""

    def __init__(self, delay_us, lat, lon, alt_m=None):
        self.latest_delay_us = delay_us
        self.lat = lat
        self.lon = lon
        self.alt_m = alt_m


def _spy_build_count(monkeypatch):
    """Wrap _build_single_node_arc to count how often it actually rebuilds.

    Patched on services.track_gates — the module the cache lives in and calls
    through — not on the frame_processor re-export, which is just a binding.
    """
    from services import track_gates as _tg
    calls = {"n": 0}
    real = _tg._build_single_node_arc

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(_tg, "_build_single_node_arc", _spy)
    return calls


class TestSingleNodeArcCache:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        _fp._single_node_arc_cache.clear()
        yield
        _fp._single_node_arc_cache.clear()

    def test_identical_inputs_build_once(self, monkeypatch):
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        a1 = _fp._cached_single_node_arc("abc123", _ArcTrack(80.0, 33.65, -84.95), cfg, touched)
        a2 = _fp._cached_single_node_arc("abc123", _ArcTrack(80.0, 33.65, -84.95), cfg, touched)
        assert calls["n"] == 1
        assert a1 == a2

    def test_delay_change_rebuilds(self, monkeypatch):
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95), cfg, touched)
        _fp._cached_single_node_arc("abc", _ArcTrack(85.0, 33.65, -84.95), cfg, touched)
        assert calls["n"] == 2

    def test_position_change_hits_cache(self, monkeypatch):
        # The arc is the full detection-area locus of the measured delay; it
        # no longer depends on where the track estimate sits on that locus,
        # so a moving track with an unchanged delay must be a cache hit.
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        a1 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.6500, -84.95), cfg, touched)
        a2 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.6502, -84.95), cfg, touched)
        assert calls["n"] == 1
        assert a1 == a2

    def test_jitter_within_epsilon_hits(self, monkeypatch):
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95), cfg, touched)
        # delay jitter < 5e-4 rounds to the same fingerprint, so this is a
        # hit, not a rebuild.
        _fp._cached_single_node_arc("abc", _ArcTrack(80.0004, 33.6500001, -84.9500001), cfg, touched)
        assert calls["n"] == 1

    def test_none_delay_caches_and_hits(self, monkeypatch):
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        a1 = _fp._cached_single_node_arc("abc", _ArcTrack(None, 33.65, -84.95), cfg, touched)
        a2 = _fp._cached_single_node_arc("abc", _ArcTrack(None, 33.65, -84.95), cfg, touched)
        assert a1 is None  # builder returns None when delay_us is None
        assert a2 is None  # cache hit returns the same (cached) None
        assert calls["n"] == 1  # built once; the None result is cached and hit

    def test_cached_equals_fresh(self):
        cfg, touched = dict(_NODE_CFG), set()
        track = _ArcTrack(80.0, 33.65, -84.95)
        cached = _fp._cached_single_node_arc("abc", track, cfg, touched)
        fresh = _fp._build_single_node_arc(track, cfg)
        assert cached == fresh
        assert cached is not None  # sanity: these inputs produce a real arc

    # ── Altitude bucket in the fingerprint (FIX A cache correctness) ────────

    def test_alt_bucket_change_rebuilds(self, monkeypatch):
        # 3000 m and 3600 m land in different ARC_ALT_BUCKET_M (500 m)
        # buckets — the altitude-corrected arc differs, so it must rebuild.
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        a1 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95, alt_m=3000.0), cfg, touched)
        a2 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95, alt_m=3600.0), cfg, touched)
        assert calls["n"] == 2
        assert a1 != a2

    def test_alt_within_bucket_hits(self, monkeypatch):
        # A climb inside one 500 m bucket (3000 → 3200 m) must stay a cache
        # hit — that is the whole point of quantising the altitude.
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        a1 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95, alt_m=3000.0), cfg, touched)
        a2 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95, alt_m=3200.0), cfg, touched)
        assert calls["n"] == 1
        assert a1 == a2

    def test_cached_solve_uses_bucket_centre(self):
        # The solve runs at the bucket centre, keeping the cached arc an
        # exact function of the fingerprint: 3200 m quantises to 3000 m.
        cfg, touched = dict(_NODE_CFG), set()
        cached = _fp._cached_single_node_arc(
            "abc", _ArcTrack(80.0, 33.65, -84.95, alt_m=3200.0), cfg, touched)
        fresh = _fp._build_single_node_arc(80.0, cfg, alt_m=3000.0)
        assert cached == fresh
        assert cached is not None

    def test_low_altitude_folds_into_ground_plane(self, monkeypatch):
        # Bucket 0 (< 250 m) is the 2-D ground-plane solve, sharing its
        # fingerprint with the no-altitude case — one entry, one build.
        calls = _spy_build_count(monkeypatch)
        cfg, touched = dict(_NODE_CFG), set()
        a1 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95, alt_m=200.0), cfg, touched)
        a2 = _fp._cached_single_node_arc("abc", _ArcTrack(80.0, 33.65, -84.95), cfg, touched)
        assert calls["n"] == 1
        assert a1 == a2 == _fp._build_single_node_arc(80.0, cfg)


# ─── Arc cache wired into build_combined_aircraft_json ──────────────────────


class TestArcCacheIntegration:
    HEX_A = "aaa111"
    HEX_B = "bbb222"

    @pytest.fixture(autouse=True)
    def _clean(self):
        _fp._single_node_arc_cache.clear()
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.track_histories.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()
        yield
        _fp._single_node_arc_cache.clear()
        state.active_geo_aircraft.clear()
        state.track_last_emit.clear()
        state.track_gate_hold.clear()
        state.track_histories.clear()
        state.adsb_aircraft.clear()
        state.external_adsb_cache.clear()
        state.multinode_tracks.clear()

    def _put(self, hexid, lat, lon):
        import time as _time

        from pipeline.passive_radar import GeolocatedTrack
        track = GeolocatedTrack(
            track_id=f"t-{hexid}", lat=lat, lon=lon, alt_m=3000,
            vel_east=100.0, vel_north=50.0, vel_up=0.0,
            rms_delay=1.0, rms_doppler=1.0, n_detections=10,
            timestamp_ms=int(_time.time() * 1000), adsb_hex=None,
            latest_delay_us=80.0, target_class="aircraft",
        )
        track.wall_clock_ts = _time.time()
        state.active_geo_aircraft[hexid] = (track, dict(_NODE_CFG))

    def _build(self):
        import types
        pipeline = types.SimpleNamespace(geolocated_tracks={}, config=dict(_NODE_CFG))
        return _fp.build_combined_aircraft_json(pipeline)

    def test_dropped_hex_is_evicted(self):
        self._put(self.HEX_A, 33.65, -84.95)
        self._put(self.HEX_B, 33.66, -84.96)
        self._build()
        assert (self.HEX_A, "test_node") in _fp._single_node_arc_cache
        assert (self.HEX_B, "test_node") in _fp._single_node_arc_cache
        # HEX_B leaves the feed; next build must prune its key.
        state.active_geo_aircraft.pop(self.HEX_B)
        self._build()
        assert (self.HEX_A, "test_node") in _fp._single_node_arc_cache
        assert (self.HEX_B, "test_node") not in _fp._single_node_arc_cache

    def test_unchanged_fleet_second_build_zero_rebuilds(self, monkeypatch):
        self._put(self.HEX_A, 33.65, -84.95)
        self._build()  # first build populates the cache
        calls = _spy_build_count(monkeypatch)
        self._build()  # fleet unchanged -> every arc is a cache hit
        assert calls["n"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
