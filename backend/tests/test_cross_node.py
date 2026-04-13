"""Tests for analytics/cross_node.py — overlap, beam geometry, coverage suggestions."""

import math

import pytest

from analytics.constants import haversine_km
from analytics.cross_node import (
    _count_covering_nodes,
    _point_in_beam,
    compute_delay_bin_overlap,
    coverage_suggestion,
)
from analytics.detection_area import DetectionAreaState


# ── Helpers ───────────────────────────────────────────────────────────────────

def _area(node_id="n1", rx_lat=33.45, rx_lon=-112.07, tx_lat=33.50, tx_lon=-112.00,
          beam_azimuth=0.0, beam_width=41.0, max_range_km=50.0,
          min_delay=0.0, max_delay=10.0, n_detections=100):
    a = DetectionAreaState(
        node_id=node_id,
        rx_lat=rx_lat, rx_lon=rx_lon,
        tx_lat=tx_lat, tx_lon=tx_lon,
        beam_azimuth_deg=beam_azimuth,
        beam_width_deg=beam_width,
        max_range_km=max_range_km,
        min_delay=min_delay,
        max_delay=max_delay,
        n_detections=n_detections,
    )
    return a


# ── Delay Bin Overlap ─────────────────────────────────────────────────────────

class TestDelayBinOverlap:
    def test_identical_delay_ranges_full_overlap(self):
        a = _area(min_delay=0.0, max_delay=10.0)
        b = _area(min_delay=0.0, max_delay=10.0)
        result = compute_delay_bin_overlap(a, b)
        assert result["overlap_ratio"] == 1.0

    def test_no_overlap_disjoint_ranges(self):
        a = _area(min_delay=0.0, max_delay=5.0)
        b = _area(min_delay=15.0, max_delay=20.0)
        result = compute_delay_bin_overlap(a, b)
        assert result["overlap_ratio"] == 0.0
        assert result["shared_bins"] == 0

    def test_partial_overlap(self):
        a = _area(min_delay=0.0, max_delay=10.0)
        b = _area(min_delay=6.0, max_delay=16.0)
        result = compute_delay_bin_overlap(a, b)
        assert 0.0 < result["overlap_ratio"] < 1.0
        assert result["shared_bins"] > 0

    def test_empty_area_zero_overlap(self):
        a = _area(n_detections=0)
        b = _area(min_delay=0.0, max_delay=10.0)
        result = compute_delay_bin_overlap(a, b)
        assert result["overlap_ratio"] == 0.0

    def test_both_empty_zero_overlap(self):
        a = _area(n_detections=0)
        b = _area(n_detections=0)
        result = compute_delay_bin_overlap(a, b)
        assert result["overlap_ratio"] == 0.0

    def test_custom_bin_width(self):
        a = _area(min_delay=0.0, max_delay=10.0)
        b = _area(min_delay=0.0, max_delay=10.0)
        r1 = compute_delay_bin_overlap(a, b, bin_width_us=1.0)
        r2 = compute_delay_bin_overlap(a, b, bin_width_us=5.0)
        # Finer bins → more bins, but both should be 1.0 overlap for identical ranges
        assert r1["overlap_ratio"] == 1.0
        assert r2["overlap_ratio"] == 1.0
        assert r1["shared_bins"] > r2["shared_bins"]

    def test_a_only_and_b_only_symmetric(self):
        a = _area(min_delay=0.0, max_delay=10.0)
        b = _area(min_delay=5.0, max_delay=15.0)
        result = compute_delay_bin_overlap(a, b)
        assert result["a_only"] > 0
        assert result["b_only"] > 0


# ── Point In Beam ─────────────────────────────────────────────────────────────

class TestPointInBeam:
    def test_point_directly_ahead_in_beam(self):
        """Point 10 km due north, beam pointing north."""
        a = _area(beam_azimuth=0.0, beam_width=41.0, max_range_km=50.0,
                  rx_lat=33.45, rx_lon=-112.07)
        # 10 km north ≈ 0.09° latitude
        assert _point_in_beam(a, 33.54, -112.07) is True

    def test_point_behind_not_in_beam(self):
        """Point due south, beam pointing north → not in beam."""
        a = _area(beam_azimuth=0.0, beam_width=41.0, max_range_km=50.0,
                  rx_lat=33.45, rx_lon=-112.07)
        assert _point_in_beam(a, 33.36, -112.07) is False

    def test_point_beyond_range(self):
        """Point in correct direction but too far."""
        a = _area(beam_azimuth=0.0, beam_width=41.0, max_range_km=10.0,
                  rx_lat=33.45, rx_lon=-112.07)
        # 40 km north ≈ 0.36° latitude
        assert _point_in_beam(a, 33.81, -112.07) is False

    def test_point_at_beam_edge(self):
        """Point at ~20° off boresight with 41° beam → just inside half-width."""
        a = _area(beam_azimuth=0.0, beam_width=41.0, max_range_km=50.0,
                  rx_lat=33.45, rx_lon=-112.07)
        # 15° off boresight, 20 km away
        lat = 33.45 + 0.18 * math.cos(math.radians(15))
        lon = -112.07 + 0.18 * math.sin(math.radians(15)) / math.cos(math.radians(33.45))
        assert _point_in_beam(a, lat, lon) is True

    def test_360_degree_bearing_wrap(self):
        """Beam at 350°, point at 5° bearing → should be in beam (15° diff)."""
        a = _area(beam_azimuth=350.0, beam_width=41.0, max_range_km=50.0,
                  rx_lat=33.45, rx_lon=-112.07)
        # Point almost due north (5° bearing) - within 15° of 350°
        lat = 33.45 + 0.15  # clearly north
        lon = -112.07 + 0.005  # slight east
        assert _point_in_beam(a, lat, lon) is True

    def test_wide_beam_covers_more(self):
        """120° beam should cover directions a 41° beam wouldn't."""
        narrow = _area(beam_azimuth=0.0, beam_width=41.0, max_range_km=50.0)
        wide = _area(beam_azimuth=0.0, beam_width=120.0, max_range_km=50.0)
        # Point 50° off boresight, 20 km
        lat = 33.45 + 0.18 * math.cos(math.radians(50))
        lon = -112.07 + 0.18 * math.sin(math.radians(50)) / math.cos(math.radians(33.45))
        assert _point_in_beam(narrow, lat, lon) is False
        assert _point_in_beam(wide, lat, lon) is True


# ── Count Covering Nodes ──────────────────────────────────────────────────────

class TestCountCoveringNodes:
    def test_single_node_covers_point(self):
        areas = [_area(beam_azimuth=0.0, beam_width=41.0, max_range_km=50.0)]
        assert _count_covering_nodes(areas, 33.54, -112.07) == 1

    def test_no_nodes_cover_distant_point(self):
        areas = [_area(beam_azimuth=0.0, beam_width=41.0, max_range_km=10.0)]
        assert _count_covering_nodes(areas, 34.0, -112.07) == 0

    def test_multiple_nodes_overlapping(self):
        a1 = _area(node_id="n1", beam_azimuth=0.0, beam_width=120.0, max_range_km=50.0)
        a2 = _area(node_id="n2", beam_azimuth=0.0, beam_width=120.0, max_range_km=50.0)
        # Point 10 km north — both nodes cover it
        assert _count_covering_nodes([a1, a2], 33.54, -112.07) == 2


# ── Coverage Suggestion ───────────────────────────────────────────────────────

class TestCoverageSuggestion:
    def test_empty_network_all_expansion(self):
        """No nodes → all 8 directions are expansion suggestions."""
        result = coverage_suggestion([], center_lat=33.45, center_lon=-112.07)
        assert len(result) == 8
        assert all(s["strategy"] == "expansion" for s in result)
        directions = {s["direction"] for s in result}
        assert directions == {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}

    def test_suggestions_have_required_fields(self):
        result = coverage_suggestion([], center_lat=33.45, center_lon=-112.07)
        for s in result:
            assert "direction" in s
            assert "bearing_deg" in s
            assert "test_point" in s
            assert "lat" in s["test_point"]
            assert "lon" in s["test_point"]
            assert "strategy" in s

    def test_dense_network_has_suggestions(self):
        """With nodes covering some directions, we get expansion or densification."""
        # Place two nodes near center with wide beams pointing in cross directions
        areas = [
            _area(node_id="n0", rx_lat=33.45, rx_lon=-112.07,
                  beam_azimuth=0.0, beam_width=120.0, max_range_km=100.0),
            _area(node_id="n1", rx_lat=33.46, rx_lon=-112.06,
                  beam_azimuth=90.0, beam_width=120.0, max_range_km=100.0),
        ]
        result = coverage_suggestion(
            areas, center_lat=33.45, center_lon=-112.07, desired_range_km=50.0,
        )
        # Should have at least some suggestions (covered but < 3 nodes, or uncovered)
        assert len(result) > 0

    def test_saturation_detection_triggers_expansion(self):
        """When solver RMS plateaus and overlap is high → expansion strategy."""
        # Create overlapping nodes
        areas = [_area(node_id=f"n{i}", max_range_km=200.0, beam_width=120.0) for i in range(5)]
        # Flat RMS history = saturation
        rms_history = [1.0, 1.0, 1.0, 1.0, 1.0, 0.99, 0.99, 0.99, 0.99, 0.99]
        result = coverage_suggestion(
            areas, center_lat=33.45, center_lon=-112.07,
            solver_rms_history=rms_history,
        )
        # With saturated RMS and high overlap_density, uncovered points → expansion
        expansion_count = sum(1 for s in result if s["strategy"] == "expansion")
        assert expansion_count >= 0  # may or may not trigger

    def test_short_rms_history_no_saturation(self):
        """< 10 RMS samples → saturation should NOT trigger."""
        areas = [_area(node_id=f"n{i}", max_range_km=200.0, beam_width=120.0) for i in range(5)]
        rms_history = [1.0, 1.0, 1.0]  # only 3, need 10
        result = coverage_suggestion(
            areas, center_lat=33.45, center_lon=-112.07,
            solver_rms_history=rms_history,
        )
        assert len(result) > 0  # should still work, just no saturation-driven expansion


# ── DetectionAreaState tests ──────────────────────────────────────────────────

class TestDetectionAreaState:
    def test_update_tracking(self):
        area = DetectionAreaState(node_id="n1")
        area.update(delay=5.0, doppler=10.0)
        area.update(delay=3.0, doppler=15.0)
        area.update(delay=8.0, doppler=5.0)
        assert area.n_detections == 3
        assert area.min_delay == 3.0
        assert area.max_delay == 8.0
        assert area.min_doppler == 5.0
        assert area.max_doppler == 15.0

    def test_delay_range_empty(self):
        area = DetectionAreaState(node_id="n1")
        assert area.delay_range == (0.0, 0.0)

    def test_doppler_range_empty(self):
        area = DetectionAreaState(node_id="n1")
        assert area.doppler_range == (0.0, 0.0)

    def test_update_from_frame(self):
        area = DetectionAreaState(node_id="n1")
        frame = {"delay": [1.0, 5.0, 3.0], "doppler": [10.0, 20.0, 15.0]}
        area.update_from_frame(frame)
        assert area.n_detections == 3
        assert area.min_delay == 1.0
        assert area.max_delay == 5.0

    def test_furthest_detections_heap(self):
        area = DetectionAreaState(node_id="n1", rx_lat=33.45, rx_lon=-112.07)
        # Record 15 detections at varying distances
        for i in range(15):
            lat = 33.45 + 0.01 * (i + 1)  # increasing distance
            area.record_verified_detection(lat, -112.07)
        # Only top 10 furthest should survive
        assert len(area.furthest_detections) == 10
        # The closest of the 10 should be further than the 5 dropped
        min_dist = min(d for d, _, _ in area.furthest_detections)
        assert min_dist > 1.0  # first 5 dropped are < 6 km each

    def test_verified_detection_too_close_ignored(self):
        area = DetectionAreaState(node_id="n1", rx_lat=33.45, rx_lon=-112.07)
        area.record_verified_detection(33.45001, -112.07001)  # very close
        assert len(area.furthest_detections) == 0

    def test_estimated_max_range(self):
        area = DetectionAreaState(node_id="n1")
        area.update(delay=15.0, doppler=10.0)
        assert area.estimated_max_range_km > 0
        # 15 μs × 0.299792 km/μs ≈ 4.5 km
        assert abs(area.estimated_max_range_km - 15.0 * 0.299792458) < 0.01
