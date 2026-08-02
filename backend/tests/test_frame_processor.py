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
    dedup_aircraft,
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

    def test_failed_flush_retains_frames_in_buffer(self, monkeypatch):
        """If archive_detections raises, frames must stay in the buffer.

        Older code popped the buffer before writing, so any disk error
        silently dropped the data on the floor. The fix copies first and
        only drops the prefix it persisted on success.
        """
        from services import frame_processor as fp

        node_id = "test-buffer-retain"
        with fp._archive_buffer_lock:
            fp._archive_buffer[node_id] = [{"ts": 1}, {"ts": 2}, {"ts": 3}]

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(fp, "archive_detections", _boom)
        try:
            fp._flush_archive_node(node_id)
            with fp._archive_buffer_lock:
                assert len(fp._archive_buffer[node_id]) == 3, \
                    "frames must be retained when the disk write fails"
        finally:
            with fp._archive_buffer_lock:
                fp._archive_buffer.pop(node_id, None)

    def test_successful_flush_drops_persisted_prefix(self, monkeypatch):
        """On success, only the frames we wrote are removed from the buffer.

        Frames that arrived during the write (appended after our snapshot)
        must survive — that's why the new logic slices off a prefix instead
        of re-popping the whole list.
        """
        from services import frame_processor as fp

        node_id = "test-buffer-drop"
        with fp._archive_buffer_lock:
            fp._archive_buffer[node_id] = [{"ts": 1}, {"ts": 2}]

        def _ok(nid, frames, *args, **kwargs):
            # Simulate a frame arriving while the (slow) write is in flight.
            with fp._archive_buffer_lock:
                fp._archive_buffer[nid].append({"ts": 3})

        monkeypatch.setattr(fp, "archive_detections", _ok)
        try:
            fp._flush_archive_node(node_id)
            with fp._archive_buffer_lock:
                remaining = list(fp._archive_buffer.get(node_id, []))
            assert remaining == [{"ts": 3}], \
                f"only the late-arriving frame should remain, got {remaining}"
        finally:
            with fp._archive_buffer_lock:
                fp._archive_buffer.pop(node_id, None)


# ── De-duplication ───────────────────────────────────────────────────────────
# Nothing previously asserted "one aircraft => one entry", which is how a single
# target came to render as 4-6 overlapping icons in production: a multinode
# solve is named mn<sha> and a single-node arc by ICAO, so the builder's
# exact-string `seen_hex` set could never collapse them.

class TestDedupAircraft:
    def _entry(self, hex_code, src, lat, lon, alt=30000, gt=None, node=None):
        e = {
            "hex": hex_code, "position_source": src,
            "lat": lat, "lon": lon, "alt_baro": alt,
        }
        if gt is not None:
            e["ground_truth_hex"] = gt
        if node is not None:
            e["node_id"] = node
        return e

    def test_one_aircraft_one_entry_via_ground_truth(self):
        """The production failure: 1 arc + 5 multinode solves for one target."""
        entries = [
            self._entry("abf380", "single_node_ellipse_arc", 34.895, -81.805,
                        gt="abf380", node="synth-RING-0007"),
            *[
                self._entry(f"mn{i:010x}", "multinode_solve", 34.984 + i * 1e-4,
                            -81.97 + i * 1e-4, gt="abf380")
                for i in range(5)
            ],
        ]
        out = dedup_aircraft(entries)
        assert len(out) == 1, f"expected 1 aircraft, got {len(out)}"

    def test_multinode_wins_over_single_node_arc(self):
        out = dedup_aircraft([
            self._entry("abcdef", "single_node_ellipse_arc", 34.9, -82.0, gt="t1"),
            self._entry("mn0000000001", "multinode_solve", 34.9, -82.0, gt="t1"),
        ])
        assert len(out) == 1
        assert out[0]["position_source"] == "multinode_solve"

    def test_contributing_nodes_merged_onto_survivor(self):
        """Node filtering and the frontend highlight must still find the plane
        under every node that saw it."""
        out = dedup_aircraft([
            self._entry("abcdef", "single_node_ellipse_arc", 34.9, -82.0,
                        gt="t1", node="node-a"),
            {**self._entry("mn0000000001", "multinode_solve", 34.9, -82.0, gt="t1"),
             "contributing_node_ids": ["node-b", "node-c"]},
        ])
        assert len(out) == 1
        assert set(out[0]["contributing_node_ids"]) == {"node-a", "node-b", "node-c"}

    def test_proximity_fallback_without_ground_truth(self):
        """Real hardware has no ground_truth_hex — proximity must carry it."""
        out = dedup_aircraft([
            self._entry("pr0001", "single_node_ellipse_arc", 34.900, -82.000),
            self._entry("mn0000000001", "multinode_solve", 34.902, -82.001),
        ])
        assert len(out) == 1
        assert out[0]["position_source"] == "multinode_solve"

    def test_distinct_aircraft_are_not_merged(self):
        """Over-merging would hide real traffic — worse than a cosmetic double."""
        out = dedup_aircraft([
            self._entry("aaa111", "multinode_solve", 34.90, -82.00),
            self._entry("bbb222", "multinode_solve", 35.30, -82.00),  # ~44 km
        ])
        assert len(out) == 2

    def test_vertically_separated_aircraft_not_merged(self):
        """Same lat/lon, 10000 ft apart — two aircraft, not one."""
        out = dedup_aircraft([
            self._entry("aaa111", "multinode_solve", 34.90, -82.00, alt=20000),
            self._entry("bbb222", "multinode_solve", 34.90, -82.00, alt=30000),
        ])
        assert len(out) == 2

    def test_distinct_ground_truth_never_merged_despite_proximity(self):
        """Identity beats proximity: two known-distinct targets stay distinct."""
        out = dedup_aircraft([
            self._entry("mn0000000001", "multinode_solve", 34.900, -82.000, gt="t1"),
            self._entry("mn0000000002", "multinode_solve", 34.901, -82.000, gt="t2"),
        ])
        assert len(out) == 2

    def test_empty_and_singleton_are_passthrough(self):
        assert dedup_aircraft([]) == []
        one = [self._entry("abcdef", "multinode_solve", 34.9, -82.0)]
        assert dedup_aircraft(one) == one
