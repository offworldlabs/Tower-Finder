"""
Unit tests for metro filtering and orchestrator helpers.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from simulation.orchestrator import _KNOWN_METROS, _parse_metro_areas


class TestKnownMetros:
    def test_has_atl_and_gvl(self):
        assert "atl" in _KNOWN_METROS
        assert "gvl" in _KNOWN_METROS

    def test_metro_has_required_keys(self):
        for code, metro in _KNOWN_METROS.items():
            assert "name" in metro, f"{code} missing name"
            assert "lat" in metro, f"{code} missing lat"
            assert "lon" in metro, f"{code} missing lon"
            assert "radius_nm" in metro, f"{code} missing radius_nm"
            assert -90 <= metro["lat"] <= 90
            assert -180 <= metro["lon"] <= 180
            assert metro["radius_nm"] > 0


class TestParseMetroAreas:
    def test_single_metro(self):
        result = _parse_metro_areas("atl")
        assert len(result) == 1
        assert result[0]["name"] == "Atlanta"

    def test_multiple_metros(self):
        result = _parse_metro_areas("atl,gvl")
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"Atlanta", "Greenville"}

    def test_whitespace_handling(self):
        result = _parse_metro_areas(" atl , gvl ")
        assert len(result) == 2

    def test_unknown_metro_skipped(self):
        result = _parse_metro_areas("atl,xyz,gvl")
        assert len(result) == 2

    def test_empty_string(self):
        result = _parse_metro_areas("")
        assert len(result) == 0

    def test_case_insensitive(self):
        result = _parse_metro_areas("ATL,GVL")
        assert len(result) == 2


class TestArcBuilderWithDelay:
    """Test that _build_single_node_arc accepts float delay_us directly."""

    def test_accepts_float_delay(self):
        from services.frame_processor import _build_single_node_arc

        node_cfg = {
            "rx_lat": 33.939182,
            "rx_lon": -84.651910,
            "tx_lat": 33.756670,
            "tx_lon": -84.331844,
            "beam_width_deg": 90,
            "max_range_km": 100,
        }
        arc = _build_single_node_arc(50.0, node_cfg)  # 50 µs delay as float
        assert arc is not None
        assert len(arc) >= 2

    def test_accepts_track_object(self):
        from services.frame_processor import _build_single_node_arc

        class FakeTrack:
            latest_delay_us = 50.0

        node_cfg = {
            "rx_lat": 33.939182,
            "rx_lon": -84.651910,
            "tx_lat": 33.756670,
            "tx_lon": -84.331844,
            "beam_width_deg": 90,
            "max_range_km": 100,
        }
        arc = _build_single_node_arc(FakeTrack(), node_cfg)
        assert arc is not None
        assert len(arc) >= 2

    def test_zero_delay_returns_none(self):
        from services.frame_processor import _build_single_node_arc

        node_cfg = {
            "rx_lat": 33.0, "rx_lon": -84.0,
            "tx_lat": 33.5, "tx_lon": -84.5,
            "beam_width_deg": 90, "max_range_km": 100,
        }
        assert _build_single_node_arc(0.0, node_cfg) is None
        assert _build_single_node_arc(-5.0, node_cfg) is None
