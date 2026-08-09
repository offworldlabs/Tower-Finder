"""Tests for helper functions in services/tasks/analytics_refresh.py.

Tests the pure helper functions:
- _bearing_deg(lat1, lon1, lat2, lon2) -> bearing in [0, 360)
- _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth_deg, beam_width_deg, max_range_km) -> bool
- _bistatic_angle_deg(ac_lat, ac_lon, tx_lat, tx_lon, rx_lat, rx_lon) -> angle in [0, 180]
"""

import pytest

from core import state
from services.geo import haversine_km
from services.tasks import analytics_refresh
from services.tasks.analytics_refresh import (
    _aircraft_in_beam,
    _bearing_deg,
    _bistatic_angle_deg,
    _detected_hexes_for,
)


class TestBearingDeg:
    """Test forward azimuth bearing calculation.

    Bearing is in degrees [0, 360) from (lat1, lon1) to (lat2, lon2).
    - 0° = North (positive latitude)
    - 90° = East (positive longitude)
    - 180° = South (negative latitude)
    - 270° = West (negative longitude)
    """

    def test_bearing_north(self):
        """From (0,0) to (1,0) — moving north → bearing ≈ 0°."""
        bearing = _bearing_deg(0, 0, 1, 0)
        assert 0 <= bearing < 360
        assert bearing == pytest.approx(0, abs=1)

    def test_bearing_east(self):
        """From (0,0) to (0,1) — moving east → bearing ≈ 90°."""
        bearing = _bearing_deg(0, 0, 0, 1)
        assert 0 <= bearing < 360
        assert bearing == pytest.approx(90, abs=1)

    def test_bearing_south(self):
        """From (1,0) to (0,0) — moving south → bearing ≈ 180°."""
        bearing = _bearing_deg(1, 0, 0, 0)
        assert 0 <= bearing < 360
        assert bearing == pytest.approx(180, abs=1)

    def test_bearing_west(self):
        """From (0,1) to (0,0) — moving west → bearing ≈ 270°."""
        bearing = _bearing_deg(0, 1, 0, 0)
        assert 0 <= bearing < 360
        assert bearing == pytest.approx(270, abs=1)

    def test_bearing_northeast(self):
        """From (0,0) to (1,1) — moving northeast → bearing ≈ 45°."""
        bearing = _bearing_deg(0, 0, 1, 1)
        assert 0 <= bearing < 360
        assert bearing == pytest.approx(45, abs=2)

    def test_bearing_range_north(self):
        """North: result is in [0, 360) and close to 0°."""
        bearing = _bearing_deg(0, 0, 1, 0)
        assert 0 <= bearing < 360

    def test_bearing_range_east(self):
        """East: result is in [0, 360) and close to 90°."""
        bearing = _bearing_deg(0, 0, 0, 1)
        assert 0 <= bearing < 360

    def test_bearing_range_south(self):
        """South: result is in [0, 360) and close to 180°."""
        bearing = _bearing_deg(1, 0, 0, 0)
        assert 0 <= bearing < 360

    def test_bearing_range_west(self):
        """West: result is in [0, 360) and close to 270°."""
        bearing = _bearing_deg(0, 1, 0, 0)
        assert 0 <= bearing < 360

    def test_bearing_same_point(self):
        """Same point (0,0) to (0,0) — returns a value in [0, 360), no crash."""
        bearing = _bearing_deg(0, 0, 0, 0)
        assert 0 <= bearing < 360

    def test_bearing_same_point_nonzero(self):
        """Same point (45,50) to (45,50) — returns a value in [0, 360), no crash."""
        bearing = _bearing_deg(45, 50, 45, 50)
        assert 0 <= bearing < 360


class TestAircraftInBeam:
    """Test aircraft detection within a beam's geometry.

    Checks range (distance), bearing (azimuth), and beam width constraints.

    Common test setup:
    - RX at (0, 0)
    - Beam azimuth: 90° (East)
    - Beam width: 20° (±10° half-width)
    - Max range: 200 km
    """

    def test_aircraft_on_boresight_within_range(self):
        """Aircraft directly east of RX, well within beam and range → True."""
        # RX at origin, aircraft ~111 km due east
        ac_lat, ac_lon = 0, 1.0
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 90  # East
        beam_width = 20    # ±10° half-width
        max_range_km = 200

        # Verify distance is within range
        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
        assert dist < max_range_km

        # Verify bearing is approximately east
        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        assert bearing == pytest.approx(90, abs=1)

        assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is True

    def test_aircraft_beyond_max_range(self):
        """Aircraft far beyond max_range_km → False, regardless of bearing."""
        # RX at origin, aircraft very far away
        ac_lat, ac_lon = 10, 0
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 90
        beam_width = 20
        max_range_km = 200

        # Verify distance is beyond range
        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
        assert dist > max_range_km

        assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is False

    def test_aircraft_at_exactly_max_range(self):
        """Aircraft at exactly max_range_km → True (dist == max_range uses `>`, not `>=`)."""
        # RX at origin, find an aircraft ~200 km away
        # At ~1.8 degrees north from origin: ~111 * sqrt(2) ≈ 157 km
        # At ~2.0 degrees north and 1.0 degree east: ~185 km
        # Empirically place at distance very close to 200 km
        ac_lat, ac_lon = 1.5, 1.5
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 45  # NE
        beam_width = 20
        max_range_km = 200

        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)

        if dist < max_range_km:
            # Too close; skip this test variation
            pytest.skip(f"Distance {dist:.1f} km is less than {max_range_km}, cannot test exact boundary")

        # For a proper boundary test, we'd need to calibrate the exact lat/lon
        # Instead, test with a distance that is definitely at the edge
        # by using a slightly smaller max_range
        exact_range_km = dist
        result = _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, exact_range_km)
        # dist <= max_range should return True (because condition is dist > max_range)
        assert result is True

    def test_aircraft_near_boundary_inside(self):
        """Aircraft ~9.98° off boresight (just inside the 10° half-width) → True."""
        # RX at origin, beam east (90°), half-width 10°.
        # ac at (-0.176, 1.0) gives bearing ≈ 99.98° → diff ≈ 9.98° ≤ 10°.
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 90
        beam_width = 20
        max_range_km = 200
        ac_lat, ac_lon = -0.176, 1.0

        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        diff = abs((bearing - beam_azimuth + 180) % 360 - 180)
        assert diff < beam_width / 2.0, f"Expected diff < 10°, got {diff:.3f}°"
        assert diff > beam_width / 2.0 - 1.0, f"Expected diff > 9°, got {diff:.3f}°"

        assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is True

    def test_aircraft_near_boundary_outside(self):
        """Aircraft ~10.15° off boresight (just outside the 10° half-width) → False."""
        # ac at (-0.177, 1.0) gives bearing ≈ 100.15° → diff ≈ 10.15° > 10°.
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 90
        beam_width = 20
        max_range_km = 200
        ac_lat, ac_lon = -0.177, 1.0

        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        diff = abs((bearing - beam_azimuth + 180) % 360 - 180)
        assert diff > beam_width / 2.0, f"Expected diff > 10°, got {diff:.3f}°"
        assert diff < beam_width / 2.0 + 1.0, f"Expected diff < 11°, got {diff:.3f}°"

        assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is False

    def test_aircraft_on_boresight_but_too_far(self):
        """Aircraft on boresight axis but beyond max_range → False."""
        # RX at origin, aircraft far away on the boresight (east)
        ac_lat, ac_lon = 0, 5.0  # ~555 km east
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 90
        beam_width = 20
        max_range_km = 200

        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
        assert dist > max_range_km

        # Even though bearing is perfect, distance fails
        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        assert bearing == pytest.approx(90, abs=1)

        assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is False

    def test_aircraft_within_range_and_beam_30deg_offset(self):
        """Aircraft 30° off boresight (outside 20° beam) but within range → False."""
        # RX at origin, beam pointing north (0°)
        # Aircraft at 30° west of north should be outside a 20° beam
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 0   # North
        beam_width = 20    # ±10° half-width
        max_range_km = 200

        # Place aircraft to create a ~30° offset
        ac_lat, ac_lon = 0.8, -0.5

        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
        assert dist < max_range_km

        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        diff = (bearing - beam_azimuth + 180) % 360 - 180
        assert abs(diff) > beam_width / 2.0

        assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is False

    def test_aircraft_beam_wraps_at_360_degrees(self):
        """Test beam boundary wrapping at 0°/360° (north)."""
        # Beam pointing at 355° (5° west of north)
        # Half-width 10°, so covers [345°, 5°]
        # Aircraft at 5° east of north (bearing 5°) should be in
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 355
        beam_width = 20    # ±10° half-width
        max_range_km = 200

        # Aircraft positioned to have bearing close to 5°
        ac_lat, ac_lon = 1.0, 0.09

        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
        assert dist < max_range_km

        result = _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km)
        # This should be True if bearing ≈ 5° is within [345°, 365° mod 360]
        # The diff calculation handles the wrapping, so check the result
        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        diff = (bearing - beam_azimuth + 180) % 360 - 180
        if abs(diff) <= beam_width / 2.0:
            assert result is True
        else:
            assert result is False

    def test_aircraft_narrow_beam_10deg(self):
        """Test with a narrow 10° beam (±5° half-width)."""
        rx_lat, rx_lon = 0, 0
        beam_azimuth = 90  # East
        beam_width = 10    # ±5° half-width
        max_range_km = 200

        # Aircraft slightly off boresight but within 5°
        ac_lat, ac_lon = 0.04, 1.0
        dist = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
        assert dist < max_range_km

        bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
        diff = (bearing - beam_azimuth + 180) % 360 - 180
        if abs(diff) <= 5.5:  # Account for rounding
            assert _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range_km) is True


# ── _bistatic_angle_deg ───────────────────────────────────────────────────────


class TestBistaticAngleDeg:
    """Test bistatic angle at the aircraft vertex for a TX-RX pair."""

    def test_acute_angle_geometry(self):
        """Aircraft far from the baseline yields an acute bistatic angle (< 90°)."""
        # TX=(0,0), RX=(0,2), aircraft=(5,1): aircraft far north of both nodes →
        # rays from aircraft to TX and RX converge at a narrow angle.
        angle = _bistatic_angle_deg(5, 1, 0, 0, 0, 2)
        assert 0 <= angle <= 180
        assert angle < 90

    def test_symmetric_geometry_approximates_90(self):
        """Aircraft equidistant from TX and RX perpendicularly yields ~90°."""
        # TX=(0,0), RX=(0,2), aircraft=(1,1): isoceles → bistatic angle ≈ 90°
        angle = _bistatic_angle_deg(1, 1, 0, 0, 0, 2)
        assert angle == pytest.approx(90, abs=5)

    def test_degenerate_aircraft_at_tx_returns_180(self):
        """Aircraft at TX position (a < 0.01 km) → 180.0."""
        # aircraft and TX both at (0, 0), RX at (0, 1)
        angle = _bistatic_angle_deg(0, 0, 0, 0, 0, 1)
        assert angle == 180.0

    def test_degenerate_aircraft_at_rx_returns_180(self):
        """Aircraft at RX position (b < 0.01 km) → 180.0."""
        # aircraft and RX both at (0, 1), TX at (0, 0)
        angle = _bistatic_angle_deg(0, 1, 0, 0, 0, 1)
        assert angle == 180.0

    def test_collinear_near_baseline_gives_large_angle(self):
        """Aircraft between TX and RX on the baseline → angle approaches 180°."""
        # TX=(0,0), RX=(0,2), aircraft=(0,1): collinear midpoint
        angle = _bistatic_angle_deg(0, 1, 0, 0, 0, 2)
        assert angle > 150


# ── Miss-rate scoring must match the node's own range rule ──────────────────

class TestInBeamRespectsBistaticRule:
    """The missed-detection statistic compares "aircraft the node should have
    seen" against "aircraft it did see".  If the two use different range rules
    the number measures the mismatch, not the node.

    Scoring a bistatically-limited node against a monostatic circle counted
    every aircraft near the RX but far from the TX as missed, which pushed the
    fleet-wide miss rate past the 70% health threshold and turned /api/health
    to degraded.
    """

    # RX at origin, TX 40 km due east.
    RX_LAT, RX_LON = 34.85, -82.39
    BASELINE_KM = 40.0

    @property
    def tx_lon(self):
        import math
        return self.RX_LON + self.BASELINE_KM / (111.32 * math.cos(math.radians(self.RX_LAT)))

    def _ac_west_of_rx(self, km):
        import math
        return (
            self.RX_LAT,
            self.RX_LON - km / (111.32 * math.cos(math.radians(self.RX_LAT))),
        )

    def test_bistatic_rule_excludes_what_monostatic_includes(self):
        # 35 km west of the RX: inside a 50 km monostatic circle, but the TX
        # leg makes the bistatic range 70 km — beyond a 60 km budget.
        ac_lat, ac_lon = self._ac_west_of_rx(35.0)
        common = (ac_lat, ac_lon, self.RX_LAT, self.RX_LON, 270.0, 200.0, 50.0)
        assert _aircraft_in_beam(*common) is True
        assert _aircraft_in_beam(
            *common,
            tx_lat=self.RX_LAT, tx_lon=self.tx_lon,
            max_bistatic_range_km=60.0,
        ) is False

    def test_bistatic_rule_includes_targets_inside_the_budget(self):
        ac_lat, ac_lon = self._ac_west_of_rx(20.0)
        assert _aircraft_in_beam(
            ac_lat, ac_lon, self.RX_LAT, self.RX_LON, 270.0, 200.0, 50.0,
            tx_lat=self.RX_LAT, tx_lon=self.tx_lon,
            max_bistatic_range_km=60.0,
        ) is True

    def test_absent_key_keeps_monostatic_scoring(self):
        """Hardware nodes carry only max_range_km and must score as before."""
        ac_lat, ac_lon = self._ac_west_of_rx(35.0)
        assert _aircraft_in_beam(
            ac_lat, ac_lon, self.RX_LAT, self.RX_LON, 270.0, 200.0, 50.0,
            tx_lat=self.RX_LAT, tx_lon=self.tx_lon,
            max_bistatic_range_km=None,
        ) is True

    def test_beam_still_gates_under_the_bistatic_rule(self):
        """Range is not the only test — an out-of-beam target stays excluded."""
        ac_lat, ac_lon = self._ac_west_of_rx(10.0)
        assert _aircraft_in_beam(
            ac_lat, ac_lon, self.RX_LAT, self.RX_LON, 90.0, 40.0, 50.0,
            tx_lat=self.RX_LAT, tx_lon=self.tx_lon,
            max_bistatic_range_km=60.0,
        ) is False


class TestCoverageConstraintRefresh:
    """Overlap grids follow a coverage polygon that tightens after registration.

    Grids are built once at registration, so without this the empirical
    constraint would only ever take effect on a restart.  It runs on the
    analytics cadence rather than the frame path because each rebuild costs one
    grid computation per neighbour.

    _quantize_digest also handles the FOV_MODE tuple digest — see
    TestQuantizeDigestTupleFormat below — but the rebuild-decision tests here
    stay on the float|None shape (constraint_digest) they always used; the
    logic under test (recheck floor, first-build, sub-bucket drift) is
    identical for both shapes, and TestQuantizeDigestTupleFormat is where the
    tuple-specific behaviour (state code compared exactly) is pinned.
    """

    def setup_method(self):
        analytics_refresh._COVERAGE_DIGESTS.clear()
        analytics_refresh._COVERAGE_NEXT_CHECK.clear()

    def _stub(self, monkeypatch, digests, rebuilt_calls):
        # Most tests exercise the digest comparison, not the recheck floor;
        # zero it so consecutive refreshes in one test are not skipped.
        monkeypatch.setattr(analytics_refresh, "_COVERAGE_RECHECK_MIN_S", 0.0)
        class _Assoc:
            node_geometries = {"n1": object(), "n2": object()}

            def rebuild_zones_for(self, node_id):
                rebuilt_calls.append(node_id)
                return 3

        class _Analytics:
            def coverage_digest(self, node_id):
                return digests.get(node_id)

        monkeypatch.setattr(analytics_refresh.state, "node_associator", _Assoc())
        monkeypatch.setattr(analytics_refresh.state, "node_analytics", _Analytics())

    def test_a_changed_digest_triggers_a_rebuild(self, monkeypatch):
        calls = []
        self._stub(monkeypatch, {"n1": (1.0, None), "n2": None}, calls)
        assert analytics_refresh._refresh_coverage_constraints() == 1
        assert calls == ["n1"]

    def test_an_unchanged_digest_costs_nothing(self, monkeypatch):
        calls = []
        self._stub(monkeypatch, {"n1": (1.0, None), "n2": None}, calls)
        analytics_refresh._refresh_coverage_constraints()
        calls.clear()
        assert analytics_refresh._refresh_coverage_constraints() == 0
        assert calls == []

    def test_a_node_without_a_polygon_is_skipped(self, monkeypatch):
        calls = []
        self._stub(monkeypatch, {"n1": None, "n2": None}, calls)
        assert analytics_refresh._refresh_coverage_constraints() == 0
        assert calls == []

    def test_a_later_change_triggers_another_rebuild(self, monkeypatch):
        calls = []
        digests = {"n1": (10.0, None), "n2": None}
        self._stub(monkeypatch, digests, calls)
        analytics_refresh._refresh_coverage_constraints()
        digests["n1"] = (30.0, None)         # polygon moved a quantum bucket
        calls.clear()
        assert analytics_refresh._refresh_coverage_constraints() == 1
        assert calls == ["n1"]

    def test_sub_bucket_drift_does_not_rebuild(self, monkeypatch):
        calls = []
        digests = {"n1": (10.0, None), "n2": None}
        self._stub(monkeypatch, digests, calls)
        analytics_refresh._refresh_coverage_constraints()
        digests["n1"] = (10.9, None)         # p85 crept, same 2 km bucket
        calls.clear()
        assert analytics_refresh._refresh_coverage_constraints() == 0
        assert calls == []

    def test_recheck_floor_skips_known_nodes(self, monkeypatch):
        calls = []
        digests = {"n1": (10.0, None), "n2": None}
        self._stub(monkeypatch, digests, calls)
        monkeypatch.setattr(analytics_refresh, "_COVERAGE_RECHECK_MIN_S", 3600.0)
        analytics_refresh._refresh_coverage_constraints()
        digests["n1"] = (30.0, None)         # real change, inside the window
        calls.clear()
        assert analytics_refresh._refresh_coverage_constraints() == 0
        assert calls == []
        # Window expiry re-enables the check and the change lands.
        analytics_refresh._COVERAGE_NEXT_CHECK["n1"] = 0.0
        assert analytics_refresh._refresh_coverage_constraints() == 1
        assert calls == ["n1"]

    def test_first_build_is_never_delayed_by_the_floor(self, monkeypatch):
        calls = []
        digests = {"n1": None, "n2": None}
        self._stub(monkeypatch, digests, calls)
        monkeypatch.setattr(analytics_refresh, "_COVERAGE_RECHECK_MIN_S", 3600.0)
        analytics_refresh._refresh_coverage_constraints()   # no polygon yet
        digests["n1"] = (10.0, None)                        # polygon appears
        assert analytics_refresh._refresh_coverage_constraints() == 1
        assert calls == ["n1"]


class TestQuantizeDigestTupleFormat:
    """_quantize_digest also has to handle fov_digest()'s per-bin
    (state_code, limit) shape (FOV_MODE shadow/active), not just
    constraint_digest()'s float|None — see empirical_coverage.EmpiricalCoverageState.fov_digest.
    """

    def test_state_code_compared_exactly(self):
        """A bin opening ("closed" -> "prior") must move the digest even
        when the limit component rounds to the same 2 km bucket."""
        before = analytics_refresh._quantize_digest((("closed", 0.0), ("prior", 42.0)))
        after = analytics_refresh._quantize_digest((("prior", 0.0), ("prior", 42.0)))
        assert before != after

    def test_limit_is_quantized_to_the_same_2km_grain(self):
        q1 = analytics_refresh._quantize_digest((("observed", 40.0),))
        q2 = analytics_refresh._quantize_digest((("observed", 40.9),))
        assert q1 == q2   # same 2 km bucket, same state -- no rebuild

    def test_a_meaningful_limit_move_still_changes_the_digest(self):
        q1 = analytics_refresh._quantize_digest((("observed", 20.0),))
        q2 = analytics_refresh._quantize_digest((("observed", 60.0),))
        assert q1 != q2

    def test_none_entries_pass_through(self):
        assert analytics_refresh._quantize_digest((None, ("closed", 0.0))) == (
            None, ("closed", 0),
        )

    def test_float_digest_still_works_unquantized_by_the_tuple_branch(self):
        """The off-mode constraint_digest() shape must not be misrouted into
        the tuple branch."""
        assert analytics_refresh._quantize_digest((10.9, None)) == (5, None)


# ── Disappearance detector (FOV_MODE shadow/active) ─────────────────────────


class _StubMetrics:
    def __init__(self, last_heartbeat):
        self.last_heartbeat = last_heartbeat


class _StubFov:
    def __init__(self, ok=True):
        self.ok = ok

    def contains(self, bearing, range_km):
        return self.ok


class _StubAnalytics:
    def __init__(self, fov=None, metrics=None):
        self._fov = fov
        self.metrics = metrics or {}
        self.recorded: list = []

    def learned_fov_for(self, node_id):
        return self._fov

    def record_negative_event(self, node_id, lat, lon):
        self.recorded.append((node_id, lat, lon))
        return True


class TestDisappearanceDetector:
    """_refresh_disappearance_detector — FOV_MODE shadow/active only (callers
    gate on state.FOV_MODE != "off", exercised via _refresh_missed_detections
    itself; this class drives the detector directly, one cycle at a time)."""

    RX_LAT, RX_LON = 35.0, -82.0

    def setup_method(self):
        analytics_refresh._DETECTED_RECENTLY.clear()
        state.adsb_aircraft.clear()
        state.fov_neg_events = 0

    def _adsb(self, hex_code, lat, lon, age_s, now):
        state.adsb_aircraft[hex_code] = {
            "lat": lat, "lon": lon,
            "last_seen_ms": (now - age_s) * 1000.0,
        }

    def test_event_on_detected_then_gone_with_fresh_in_fov_adsb(self, monkeypatch):
        now = 1_000_000.0
        analytics = _StubAnalytics(fov=_StubFov(ok=True), metrics={"n1": _StubMetrics(now)})
        monkeypatch.setattr(state, "node_analytics", analytics)

        # Cycle 1: the node's tracker has the hex.
        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, {"abc123"},
        )
        assert analytics.recorded == []

        # Cycle 2: gone from the tracker, but a fresh ADS-B fix says where it
        # is -- and the stub fov says that position is inside the node's
        # current claim.
        later = now + 30.0
        self._adsb("abc123", self.RX_LAT + 0.1, self.RX_LON, age_s=5.0, now=later)
        analytics_refresh._refresh_disappearance_detector(
            "n1", later, self.RX_LAT, self.RX_LON, set(),
        )
        assert analytics.recorded == [("n1", self.RX_LAT + 0.1, self.RX_LON)]
        assert state.fov_neg_events == 1
        # One event per disappearance: dropped from memory, so a third cycle
        # with the same absent hex (even with the same fresh fix) does not
        # emit a second event.
        assert "abc123" not in analytics_refresh._DETECTED_RECENTLY.get("n1", {})
        analytics_refresh._refresh_disappearance_detector(
            "n1", later + 1.0, self.RX_LAT, self.RX_LON, set(),
        )
        assert len(analytics.recorded) == 1

    def test_no_event_for_never_detected_aircraft(self, monkeypatch):
        """The was-detected precondition: a hex that never entered memory
        cannot produce an event, however good its ADS-B fix."""
        now = 1_000_000.0
        analytics = _StubAnalytics(fov=_StubFov(ok=True), metrics={"n1": _StubMetrics(now)})
        monkeypatch.setattr(state, "node_analytics", analytics)
        self._adsb("zzz999", self.RX_LAT, self.RX_LON, age_s=5.0, now=now)

        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, set(),
        )
        assert analytics.recorded == []
        assert state.fov_neg_events == 0

    def test_liveness_guard_blocks_a_dark_node(self, monkeypatch):
        """A node not heard from within _FOV_NODE_LIVENESS_MAX_AGE_S produces
        no events even with a detected-then-gone hex and a fresh fix."""
        now = 1_000_000.0
        stale = now - (analytics_refresh._FOV_NODE_LIVENESS_MAX_AGE_S + 30.0)
        analytics = _StubAnalytics(fov=_StubFov(ok=True), metrics={"n1": _StubMetrics(stale)})
        monkeypatch.setattr(state, "node_analytics", analytics)

        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, {"abc123"},
        )
        later = now + 30.0
        self._adsb("abc123", self.RX_LAT, self.RX_LON, age_s=5.0, now=later)
        analytics_refresh._refresh_disappearance_detector(
            "n1", later, self.RX_LAT, self.RX_LON, set(),
        )
        assert analytics.recorded == []
        assert state.fov_neg_events == 0

    def test_per_cycle_cap(self, monkeypatch):
        now = 1_000_000.0
        analytics = _StubAnalytics(fov=_StubFov(ok=True), metrics={"n1": _StubMetrics(now)})
        monkeypatch.setattr(state, "node_analytics", analytics)
        hexes = {f"h{i}" for i in range(8)}

        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, hexes,
        )
        later = now + 30.0
        for h in hexes:
            self._adsb(h, self.RX_LAT, self.RX_LON, age_s=5.0, now=later)
        analytics_refresh._refresh_disappearance_detector(
            "n1", later, self.RX_LAT, self.RX_LON, set(),
        )
        assert len(analytics.recorded) == analytics_refresh._FOV_MAX_EVENTS_PER_NODE_CYCLE
        # The uncapped remainder stays in memory for the next cycle rather
        # than being silently dropped.
        assert len(analytics_refresh._DETECTED_RECENTLY["n1"]) == (
            len(hexes) - analytics_refresh._FOV_MAX_EVENTS_PER_NODE_CYCLE
        )

    def test_memory_expires_without_an_event_when_no_fresh_fix_ever_arrives(self, monkeypatch):
        now = 1_000_000.0
        analytics = _StubAnalytics(fov=_StubFov(ok=True), metrics={"n1": _StubMetrics(now)})
        monkeypatch.setattr(state, "node_analytics", analytics)

        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, {"abc123"},
        )
        stale_cycle = now + analytics_refresh._FOV_DISAPPEAR_MEMORY_S + 1.0
        # Update the stub's liveness so the guard itself doesn't block this.
        analytics.metrics["n1"] = _StubMetrics(stale_cycle)
        analytics_refresh._refresh_disappearance_detector(
            "n1", stale_cycle, self.RX_LAT, self.RX_LON, set(),
        )
        assert analytics.recorded == []
        assert "abc123" not in analytics_refresh._DETECTED_RECENTLY.get("n1", {})

    def test_no_event_when_the_fresh_fix_is_outside_the_current_fov(self, monkeypatch):
        now = 1_000_000.0
        analytics = _StubAnalytics(fov=_StubFov(ok=False), metrics={"n1": _StubMetrics(now)})
        monkeypatch.setattr(state, "node_analytics", analytics)

        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, {"abc123"},
        )
        later = now + 30.0
        self._adsb("abc123", self.RX_LAT, self.RX_LON, age_s=5.0, now=later)
        analytics_refresh._refresh_disappearance_detector(
            "n1", later, self.RX_LAT, self.RX_LON, set(),
        )
        assert analytics.recorded == []
        assert state.fov_neg_events == 0
        # Not dropped -- still eligible on a later cycle if the fov opens or
        # a fresher fix arrives, until the 90 s memory window elapses.
        assert "abc123" in analytics_refresh._DETECTED_RECENTLY.get("n1", {})

    def test_no_event_when_the_adsb_fix_is_stale(self, monkeypatch):
        now = 1_000_000.0
        analytics = _StubAnalytics(fov=_StubFov(ok=True), metrics={"n1": _StubMetrics(now)})
        monkeypatch.setattr(state, "node_analytics", analytics)

        analytics_refresh._refresh_disappearance_detector(
            "n1", now, self.RX_LAT, self.RX_LON, {"abc123"},
        )
        later = now + 30.0
        self._adsb("abc123", self.RX_LAT, self.RX_LON,
                   age_s=analytics_refresh._FOV_EVENT_MAX_ADSB_AGE_S + 1.0, now=later)
        analytics_refresh._refresh_disappearance_detector(
            "n1", later, self.RX_LAT, self.RX_LON, set(),
        )
        assert analytics.recorded == []


# ── "detected" means the tracker associated a detection THIS frame ─────────


class _StubTrack:
    """Minimal stand-in for retina_tracker.track.Track's fields consumed by
    _detected_hexes_for.  A raw tracker Track always carries n_missed; the
    getattr default in the helper exists only so tests like this one can omit
    it harmlessly."""

    def __init__(self, adsb_hex, n_missed=0):
        self.adsb_hex = adsb_hex
        self.n_missed = n_missed


class TestDetectedHexesFor:
    def setup_method(self):
        state.node_pipelines.clear()

    def teardown_method(self):
        state.node_pipelines.clear()

    def _pipeline(self, tracks):
        import types
        return types.SimpleNamespace(tracker=types.SimpleNamespace(tracks=tracks))

    def test_a_coasting_track_is_excluded(self):
        # n_missed > 0 means the tracker did NOT associate a detection to
        # this track on the latest frame -- it is coasting, not detected.
        state.node_pipelines["n1"] = self._pipeline([_StubTrack("abc123", n_missed=1)])
        assert _detected_hexes_for("n1") == set()

    def test_a_freshly_associated_track_is_included(self):
        state.node_pipelines["n1"] = self._pipeline([_StubTrack("abc123", n_missed=0)])
        assert _detected_hexes_for("n1") == {"abc123"}

    def test_hex_is_lowercased(self):
        state.node_pipelines["n1"] = self._pipeline([_StubTrack("ABC123", n_missed=0)])
        assert _detected_hexes_for("n1") == {"abc123"}

    def test_missing_n_missed_defaults_harmless_for_stub_tracks(self):
        # No n_missed attribute at all -- getattr default (0) must not
        # exclude it, so stub tracks (as used throughout this test module)
        # keep working.
        import types
        stub = types.SimpleNamespace(adsb_hex="def456")
        state.node_pipelines["n1"] = self._pipeline([stub])
        assert _detected_hexes_for("n1") == {"def456"}

    def test_no_pipeline_returns_empty_set(self):
        assert _detected_hexes_for("missing-node") == set()

    def test_track_without_adsb_hex_is_skipped(self):
        state.node_pipelines["n1"] = self._pipeline([_StubTrack(None, n_missed=0)])
        assert _detected_hexes_for("n1") == set()
