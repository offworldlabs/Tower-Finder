"""Tests for frame_processor module.

Covers: process_one_frame, build_combined_aircraft_json, helper functions,
archive buffering, get_or_create_node_pipeline.
"""

import time

import pytest

from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services.frame_processor import (
    append_track_history,
    build_combined_aircraft_json,
    flush_all_archive_buffers,
    get_node_configs,
    get_or_create_node_pipeline,
    multinode_to_aircraft,
    normalize_hex_key,
    position_distance_km,
    process_one_frame,
    resolve_ground_truth_hex,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_frame(ts: int = None, n: int = 3) -> dict:
    if ts is None:
        ts = int(time.time() * 1000)
    return {
        "timestamp": ts,
        "delay": [50.0 + i * 2.0 for i in range(n)],
        "doppler": [10.0 + i * 5.0 for i in range(n)],
        "snr": [20.0 + i for i in range(n)],
    }


@pytest.fixture(autouse=True)
def _cleanup():
    """Clean up state after each test."""
    yield
    # Remove test nodes and pipelines
    for key in list(state.connected_nodes.keys()):
        if key.startswith("test-"):
            del state.connected_nodes[key]
    for key in list(state.node_pipelines.keys()):
        if key.startswith("test-"):
            del state.node_pipelines[key]
    for key in list(state.track_histories.keys()):
        if key.startswith("test"):
            del state.track_histories[key]
    for key in list(state.ground_truth_trails.keys()):
        if key.startswith("test"):
            del state.ground_truth_trails[key]


# ── Unit tests for helper functions ──────────────────────────────────────────

class TestNormalizeHexKey:
    def test_basic(self):
        assert normalize_hex_key("ABC123") == "abc123"

    def test_whitespace(self):
        assert normalize_hex_key("  abc  ") == "abc"

    def test_none(self):
        assert normalize_hex_key(None) == ""

    def test_empty(self):
        assert normalize_hex_key("") == ""


class TestPositionDistanceKm:
    def test_same_point(self):
        d = position_distance_km(33.9, -84.6, 33.9, -84.6)
        assert d == 0.0

    def test_known_distance(self):
        # ~1 degree latitude ≈ 111 km
        d = position_distance_km(33.0, -84.0, 34.0, -84.0)
        assert abs(d - 111.0) < 1.0

    def test_small_distance(self):
        d = position_distance_km(33.9, -84.6, 33.901, -84.601)
        assert 0.0 < d < 1.0  # should be ~150 meters


class TestAppendTrackHistory:
    def test_appends_position(self):
        append_track_history("testac1", 33.9, -84.6, 35000, time.time())
        assert "testac1" in state.track_histories
        assert len(state.track_histories["testac1"]) == 1

    def test_skips_duplicate_positions(self):
        ts = time.time()
        append_track_history("testac2", 33.9, -84.6, 35000, ts)
        append_track_history("testac2", 33.9, -84.6, 35000, ts + 1)  # same position
        assert len(state.track_histories["testac2"]) == 1

    def test_different_positions_appended(self):
        ts = time.time()
        append_track_history("testac3", 33.9, -84.6, 35000, ts)
        append_track_history("testac3", 34.0, -84.5, 35000, ts + 1)  # different
        assert len(state.track_histories["testac3"]) == 2

    def test_respects_maxlen(self):
        ts = time.time()
        for i in range(100):
            append_track_history("testac4", 33.0 + i * 0.1, -84.0, 35000, ts + i)
        assert len(state.track_histories["testac4"]) <= state.TRACK_HISTORY_MAX


class TestResolveGroundTruthHex:
    def test_exact_match(self):
        from collections import deque
        state.ground_truth_trails["testhex1"] = deque([[33.9, -84.6, 35000, time.time()]])
        result = resolve_ground_truth_hex("testhex1", 33.9, -84.6)
        assert result == "testhex1"

    def test_proximity_match(self):
        from collections import deque
        state.ground_truth_trails["testnear"] = deque([[33.9, -84.6, 35000, time.time()]])
        result = resolve_ground_truth_hex("testunknown", 33.901, -84.601)
        assert result == "testnear"

    def test_no_match_too_far(self):
        from collections import deque
        state.ground_truth_trails["testfar"] = deque([[40.0, -74.0, 35000, time.time()]])
        result = resolve_ground_truth_hex("testunknown2", 33.9, -84.6)
        assert result is None


class TestGetNodeConfigs:
    def test_returns_configs(self):
        state.connected_nodes["test-cfg-1"] = {
            "config": {"rx_lat": 33.9, "rx_lon": -84.6},
            "status": "active",
        }
        configs = get_node_configs()
        assert "test-cfg-1" in configs
        assert configs["test-cfg-1"]["rx_lat"] == 33.9

    def test_skips_missing_config(self):
        state.connected_nodes["test-cfg-2"] = {"status": "active"}
        configs = get_node_configs()
        assert "test-cfg-2" not in configs


# ── Pipeline factory ─────────────────────────────────────────────────────────

class TestGetOrCreateNodePipeline:
    def test_creates_pipeline_for_new_node(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        state.connected_nodes["test-new"] = {
            "config": {
                "rx_lat": 34.0, "rx_lon": -84.0, "rx_alt_ft": 900,
                "tx_lat": 33.8, "tx_lon": -83.8, "tx_alt_ft": 1200,
            },
        }
        p = get_or_create_node_pipeline("test-new", default)
        assert p is not default
        assert "test-new" in state.node_pipelines

    def test_returns_cached_pipeline(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        state.connected_nodes["test-cached"] = {
            "config": {
                "rx_lat": 34.0, "rx_lon": -84.0, "rx_alt_ft": 900,
                "tx_lat": 33.8, "tx_lon": -83.8, "tx_alt_ft": 1200,
            },
        }
        p1 = get_or_create_node_pipeline("test-cached", default)
        p2 = get_or_create_node_pipeline("test-cached", default)
        assert p1 is p2

    def test_falls_back_to_default(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        p = get_or_create_node_pipeline("test-noconfig", default)
        assert p is default


# ── Frame processing ─────────────────────────────────────────────────────────

class TestProcessOneFrame:
    def test_process_valid_frame(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        frame = _make_frame()
        # Should not raise
        process_one_frame("test-proc", frame, default)

    def test_sets_aircraft_dirty_with_adsb(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        state.aircraft_dirty = False
        frame = _make_frame()
        frame["adsb"] = [
            {"hex": "testadsb1", "lat": 33.9, "lon": -84.6, "alt_baro": 35000, "gs": 250, "track": 90},
        ]
        process_one_frame("test-adsb", frame, default)
        assert state.aircraft_dirty is True
        # Cleanup
        state.adsb_aircraft.pop("testadsb1", None)

    def test_invalid_adsb_entries_skipped(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        frame = _make_frame()
        frame["adsb"] = [
            {"hex": "testbad", "lat": float("nan"), "lon": -84.6},
        ]
        process_one_frame("test-nan", frame, default)
        assert "testbad" not in state.adsb_aircraft


# ── Multinode result conversion ──────────────────────────────────────────────

class TestMultinodeToAircraft:
    def test_basic_conversion(self):
        r = {
            "lat": 33.9, "lon": -84.6, "alt_m": 3048.0,
            "vel_east": 100.0, "vel_north": 0.0,
            "n_nodes": 3, "n_measurements": 15,
            "rms_delay": 0.5, "rms_doppler": 1.2,
        }
        ac = multinode_to_aircraft("mn-key-1", r)
        assert ac["type"] == "multinode_solve"
        assert ac["multinode"] is True
        assert ac["n_nodes"] == 3
        assert ac["lat"] == 33.9
        assert ac["lon"] == -84.6
        assert ac["alt_baro"] == 10000  # 3048m / 0.3048

    def test_supersonic_flagged(self):
        r = {
            "lat": 33.9, "lon": -84.6, "alt_m": 10000.0,
            "vel_east": 400.0, "vel_north": 0.0,  # > 343 m/s
            "n_nodes": 2, "n_measurements": 10,
            "rms_delay": 0.3, "rms_doppler": 0.8,
        }
        ac = multinode_to_aircraft("mn-key-2", r)
        assert ac["is_anomalous"] is True
        assert "supersonic" in ac["anomaly_types"]
        # Cleanup
        state.anomaly_hexes.discard(ac["hex"])

    def test_subsonic_not_flagged(self):
        r = {
            "lat": 33.9, "lon": -84.6, "alt_m": 3000.0,
            "vel_east": 100.0, "vel_north": 100.0,
            "n_nodes": 2, "n_measurements": 8,
            "rms_delay": 0.2, "rms_doppler": 0.5,
        }
        ac = multinode_to_aircraft("mn-key-3", r)
        assert ac["is_anomalous"] is False
        assert ac["anomaly_types"] == []


# ── Build combined aircraft JSON ─────────────────────────────────────────────

class TestBuildCombinedAircraftJson:
    def test_returns_valid_structure(self):
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        result = build_combined_aircraft_json(default)
        assert "now" in result
        assert "aircraft" in result
        assert isinstance(result["aircraft"], list)
        assert "messages" in result

    def test_adsb_only_excluded_from_map(self):
        """ADS-B-only aircraft (no radar detection) are intentionally excluded."""
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        state.adsb_aircraft["testabc"] = {
            "hex": "testabc",
            "lat": 33.9, "lon": -84.6,
            "alt_baro": 35000, "gs": 250, "track": 90,
            "flight": "TEST123",
            "last_seen_ms": int(time.time() * 1000),
        }
        result = build_combined_aircraft_json(default)
        hexes = [a["hex"] for a in result["aircraft"]]
        # ADS-B-only aircraft must NOT appear — they need ≥1 radar detection
        assert "testabc" not in hexes
        # Cleanup
        state.adsb_aircraft.pop("testabc", None)


# ── Archive buffering ────────────────────────────────────────────────────────

class TestArchiveBuffering:
    def test_flush_empty_is_noop(self):
        # Should not raise
        flush_all_archive_buffers()
