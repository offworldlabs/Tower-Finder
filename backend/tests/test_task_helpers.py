"""Unit tests for services/tasks/_helpers.py — haversine_km and bistatic_delay_us."""

import pytest

from config.constants import C_KM_US
from services.tasks._helpers import bistatic_delay_us, haversine_km


class TestHaversineKm:
    def test_same_point_returns_zero(self):
        """Same lat/lon → 0.0 km."""
        assert haversine_km(33.749, -84.388, 33.749, -84.388) == 0.0

    def test_equator_one_degree_lon(self):
        """1° longitude along equator ≈ 111.19 km."""
        dist = haversine_km(0.0, 0.0, 0.0, 1.0)
        assert dist == pytest.approx(111.19, abs=0.5)

    def test_one_degree_lat(self):
        """1° latitude along a meridian ≈ 111.19 km."""
        dist = haversine_km(0.0, 0.0, 1.0, 0.0)
        assert dist == pytest.approx(111.19, abs=0.5)

    def test_known_city_pair(self):
        """Atlanta → Birmingham straight-line distance ≈ 226 km (within 5 km)."""
        dist = haversine_km(33.749, -84.388, 33.520, -86.802)
        assert dist == pytest.approx(226.0, abs=5.0)

    def test_symmetry(self):
        """haversine_km(a,b,c,d) == haversine_km(c,d,a,b)."""
        d1 = haversine_km(33.749, -84.388, 33.520, -86.802)
        d2 = haversine_km(33.520, -86.802, 33.749, -84.388)
        assert d1 == pytest.approx(d2)

    def test_nonnegative(self):
        """Result is always ≥ 0."""
        pairs = [
            (0.0, 0.0, 0.0, 0.0),
            (10.0, 20.0, -10.0, -20.0),
            (89.9, 179.9, -89.9, -179.9),
        ]
        for lat1, lon1, lat2, lon2 in pairs:
            assert haversine_km(lat1, lon1, lat2, lon2) >= 0.0


class TestBistaticDelayUs:
    def test_aircraft_on_direct_path_has_zero_delay(self):
        """Aircraft at TX position: d_ta=0, d_ar=d_tr → delay=0."""
        delay = bistatic_delay_us(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        assert delay == pytest.approx(0.0, abs=1e-9)

    def test_aircraft_off_path_has_positive_delay(self):
        """Aircraft off-axis from TX-RX baseline produces positive delay."""
        delay = bistatic_delay_us(33.0, -84.0, 34.0, -84.0, 33.5, -83.5)
        assert delay > 0.0

    def test_delay_unit_is_microseconds(self):
        """Aircraft ~150 km from both TX and RX at same point: delay ≈ 300/C_KM_US µs."""
        # TX and RX co-located at (0,0); aircraft at (0, 1.35) on equator.
        # 1.35° × 111.19 km/° ≈ 150.1 km from origin.
        delay = bistatic_delay_us(0.0, 0.0, 0.0, 0.0, 0.0, 1.35)
        expected = 300.0 / C_KM_US
        # d_ta ≈ 150 km, d_ar ≈ 150 km, d_tr = 0 → delay ≈ 300/C_KM_US
        assert delay == pytest.approx(expected, rel=0.05)

    def test_delay_increases_with_distance(self):
        """Moving aircraft further off TX-RX baseline increases delay."""
        # TX=(33,-84), RX=(34,-84); aircraft at increasing off-axis longitude
        delay_near = bistatic_delay_us(33.0, -84.0, 34.0, -84.0, 33.5, -83.5)
        delay_far = bistatic_delay_us(33.0, -84.0, 34.0, -84.0, 33.5, -83.0)
        assert delay_far > delay_near
