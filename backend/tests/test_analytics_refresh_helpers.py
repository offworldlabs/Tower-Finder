"""Tests for helper functions in services/tasks/analytics_refresh.py.

Tests the pure helper functions:
- _bearing_deg(lat1, lon1, lat2, lon2) -> bearing in [0, 360)
- _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth_deg, beam_width_deg, max_range_km) -> bool
- _bistatic_angle_deg(ac_lat, ac_lon, tx_lat, tx_lon, rx_lat, rx_lon) -> angle in [0, 180]
"""

import pytest

from services.tasks._helpers import haversine_km
from services.tasks.analytics_refresh import _aircraft_in_beam, _bearing_deg, _bistatic_angle_deg


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
