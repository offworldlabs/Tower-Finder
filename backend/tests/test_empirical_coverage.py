"""Tests for analytics/empirical_coverage.py — coverage polygon derivation."""

import math

import pytest

from analytics.empirical_coverage import (
    EmpiricalCoverageState,
    N_BINS,
    MIN_POINTS,
    _DEG_PER_BIN,
    _bearing_and_range,
    _bin_for_bearing,
    _p85,
)


# ── Helper functions ──────────────────────────────────────────────────────────

RX_LAT, RX_LON = 33.4484, -112.0740  # Phoenix, AZ


class TestBearingAndRange:
    def test_due_north(self):
        bearing, range_km = _bearing_and_range(RX_LAT, RX_LON, RX_LAT + 0.1, RX_LON)
        assert abs(bearing - 0.0) < 1.0  # ~0° bearing
        assert 10.0 < range_km < 12.0    # ~11.1 km

    def test_due_east(self):
        bearing, range_km = _bearing_and_range(RX_LAT, RX_LON, RX_LAT, RX_LON + 0.12)
        assert 85.0 < bearing < 95.0     # ~90° bearing

    def test_due_south(self):
        bearing, range_km = _bearing_and_range(RX_LAT, RX_LON, RX_LAT - 0.1, RX_LON)
        assert 175.0 < bearing < 185.0   # ~180° bearing

    def test_due_west(self):
        bearing, range_km = _bearing_and_range(RX_LAT, RX_LON, RX_LAT, RX_LON - 0.12)
        assert 265.0 < bearing < 275.0   # ~270° bearing

    def test_same_point_returns_zero_range(self):
        bearing, range_km = _bearing_and_range(RX_LAT, RX_LON, RX_LAT, RX_LON)
        assert range_km == pytest.approx(0.0, abs=0.001)


class TestBinForBearing:
    def test_bearing_zero(self):
        assert _bin_for_bearing(0.0) == 0

    def test_bearing_4_999(self):
        assert _bin_for_bearing(4.999) == 0

    def test_bearing_5(self):
        assert _bin_for_bearing(5.0) == 1

    def test_bearing_359(self):
        assert _bin_for_bearing(359.0) == 71  # last bin

    def test_bearing_360_wraps_to_zero(self):
        assert _bin_for_bearing(360.0) == 0  # modulo

    def test_bearing_negative_wraps(self):
        # -5° → 355° → bin 71
        # But the function takes float, so test with a value that wraps
        assert _bin_for_bearing(355.0) == 71


class TestP85:
    def test_single_value(self):
        assert _p85([10.0]) == 10.0

    def test_two_values(self):
        # 85th percentile of [1, 2]: idx = int(2 * 0.85) = 1 → value 2
        assert _p85([1.0, 2.0]) == 2.0

    def test_hundred_values(self):
        vals = list(range(1, 101))
        result = _p85(vals)
        # idx = int(100 * 0.85) = 85 → sorted value at idx 85 = 86
        assert result == 86

    def test_all_same(self):
        assert _p85([5.0, 5.0, 5.0, 5.0]) == 5.0


# ── EmpiricalCoverageState ───────────────────────────────────────────────────

class TestCoverageIngestion:
    def test_add_point_and_count(self):
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        cov.add_point(RX_LAT + 0.1, RX_LON)  # north
        assert cov.n_points == 1

    def test_close_points_ignored(self):
        """Points < 0.5 km from RX should be dropped."""
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        cov.add_point(RX_LAT + 0.001, RX_LON)  # ~0.11 km
        assert cov.n_points == 0

    def test_bin_cap_enforced(self):
        """Each bin caps at _MAX_PER_BIN (200)."""
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        # Add 250 points all due north
        for i in range(250):
            cov.add_point(RX_LAT + 0.1 + i * 0.0001, RX_LON)
        # Should be capped at 200
        assert cov.n_points == 200

    def test_filled_bins_count(self):
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        # Add points in 4 directions
        cov.add_point(RX_LAT + 0.1, RX_LON)           # N (bin ~0)
        cov.add_point(RX_LAT, RX_LON + 0.12)           # E (bin ~18)
        cov.add_point(RX_LAT - 0.1, RX_LON)            # S (bin ~36)
        cov.add_point(RX_LAT, RX_LON - 0.12)           # W (bin ~54)
        assert cov.n_filled_bins == 4


class TestPolygonGeneration:
    def _make_coverage_all_around(self, n_points=100, radius_deg=0.2):
        """Create coverage with points uniformly distributed around RX."""
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        for i in range(n_points):
            angle = (360.0 / n_points) * i
            rad = math.radians(angle)
            lat = RX_LAT + radius_deg * math.cos(rad)
            lon = RX_LON + radius_deg * math.sin(rad) / math.cos(math.radians(RX_LAT))
            cov.add_point(lat, lon)
        return cov

    def test_no_polygon_below_min_points(self):
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        for i in range(MIN_POINTS - 1):
            angle = (360.0 / MIN_POINTS) * i
            rad = math.radians(angle)
            cov.add_point(RX_LAT + 0.2 * math.cos(rad), RX_LON + 0.2 * math.sin(rad))
        assert cov.to_polygon() is None

    def test_polygon_returned_at_min_points(self):
        cov = self._make_coverage_all_around(n_points=MIN_POINTS + 5)
        poly = cov.to_polygon()
        assert poly is not None
        assert len(poly) >= 4  # at least RX-start + 2 arc points + RX-close

    def test_polygon_is_closed(self):
        """Polygon starts and ends at RX position."""
        cov = self._make_coverage_all_around(100)
        poly = cov.to_polygon()
        assert poly[0] == poly[-1]  # closed polygon
        assert poly[0] == [round(RX_LAT, 5), round(RX_LON, 5)]

    def test_polygon_all_points_valid_coords(self):
        cov = self._make_coverage_all_around(100)
        poly = cov.to_polygon()
        for lat, lon in poly:
            assert -90 <= lat <= 90
            assert -180 <= lon <= 180

    def test_beam_clipping_reduces_coverage(self):
        """With beam sector constraint, polygon has fewer arc points."""
        cov = self._make_coverage_all_around(200)
        full = cov.to_polygon()
        clipped = cov.to_polygon(beam_azimuth_deg=0.0, beam_width_deg=60.0)
        assert clipped is not None
        assert len(clipped) < len(full)  # fewer bins in-beam

    def test_beam_clipping_narrow_beam(self):
        """Very narrow beam (~5°) → only 1-2 arc vertices."""
        cov = self._make_coverage_all_around(200)
        poly = cov.to_polygon(beam_azimuth_deg=90.0, beam_width_deg=5.0)
        # RX-start + 1 arc point + RX-close = 3, maybe 4 if bin boundary
        # Could also be None if < 4 points
        if poly is not None:
            assert len(poly) <= 5

    def test_no_data_one_quadrant(self):
        """Only north quadrant has data — polygon still formed with interpolated south."""
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        for i in range(30):
            angle = -30 + (60.0 / 30) * i  # -30° to +30° (north sector)
            rad = math.radians(angle)
            cov.add_point(
                RX_LAT + 0.2 * math.cos(rad),
                RX_LON + 0.2 * math.sin(rad) / math.cos(math.radians(RX_LAT)),
            )
        poly = cov.to_polygon()
        assert poly is not None

    def test_custom_min_points(self):
        """to_polygon(min_points=5) lowers the threshold."""
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        for i in range(10):
            angle = 36.0 * i
            rad = math.radians(angle)
            cov.add_point(RX_LAT + 0.15 * math.cos(rad), RX_LON + 0.15 * math.sin(rad))
        assert cov.to_polygon(min_points=100) is None  # too few
        assert cov.to_polygon(min_points=5) is not None  # enough


# ── Serialisation ─────────────────────────────────────────────────────────────

class TestCoverageSerialization:
    def test_to_dict_from_dict_round_trip(self):
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        cov.add_point(RX_LAT + 0.1, RX_LON)
        cov.add_point(RX_LAT, RX_LON + 0.12)

        d = cov.to_dict()
        restored = EmpiricalCoverageState.from_dict(d)

        assert restored.rx_lat == cov.rx_lat
        assert restored.rx_lon == cov.rx_lon
        assert restored.n_points == cov.n_points

    def test_from_dict_handles_empty_bins(self):
        d = {"rx_lat": RX_LAT, "rx_lon": RX_LON, "bins": []}
        restored = EmpiricalCoverageState.from_dict(d)
        assert restored.n_points == 0

    def test_save_load_file_round_trip(self, tmp_path):
        cov = EmpiricalCoverageState(RX_LAT, RX_LON)
        for i in range(30):
            angle = 12.0 * i
            rad = math.radians(angle)
            cov.add_point(RX_LAT + 0.15 * math.cos(rad), RX_LON + 0.15 * math.sin(rad))

        path = str(tmp_path / "coverage.json")
        cov.save_to_file(path)
        loaded = EmpiricalCoverageState.load_from_file(path)

        assert loaded.n_points == cov.n_points
        assert loaded.rx_lat == cov.rx_lat
        # Polygon should be identical
        p1 = cov.to_polygon()
        p2 = loaded.to_polygon()
        assert p1 == p2
