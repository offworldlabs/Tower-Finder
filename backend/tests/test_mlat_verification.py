"""Tests for _refresh_mlat_verification() and GET /api/test/mlat-verification.

Tests:
- Empty state → returns zero-counts JSON without crashing
- Single match: solve result near a ground-truth trail point
- Proximity miss: solve result beyond the match threshold is excluded
- Per-node-count breakdown populated correctly
- Percentile statistics computed correctly
- No double-matching: two solves close to the same truth only produce one match
- ADS-B fallback: uses state.adsb_aircraft when ground_truth_trails is empty
- HTTP endpoint returns 200 with the pre-computed bytes
"""

import math
import time
from collections import deque

import orjson
import pytest
from fastapi.testclient import TestClient

from core import state
from services.tasks.analytics_refresh import _refresh_mlat_verification

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_solve_result(
    lat: float,
    lon: float,
    alt_m: float = 10000.0,
    vel_east: float = 200.0,
    vel_north: float = 50.0,
    n_nodes: int = 2,
    rms_delay: float = 0.5,
    rms_doppler: float = 5.0,
    ts_ms: int | None = None,
) -> dict:
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": alt_m,
        "vel_east": vel_east,
        "vel_north": vel_north,
        "vel_up": 0.0,
        "rms_delay": rms_delay,
        "rms_doppler": rms_doppler,
        "n_nodes": n_nodes,
        "n_measurements": n_nodes * 2,
        "contributing_node_ids": [f"node-{i}" for i in range(n_nodes)],
        "cost": 0.01,
        "timestamp_ms": ts_ms,
    }


def _key(r: dict) -> str:
    return f"mn-{r['timestamp_ms']}-{r['lat']:.3f}"


def _trail_point(lat: float, lon: float, alt_m: float = 10000.0, age_s: float = 5.0) -> list:
    return [round(lat, 6), round(lon, 6), round(alt_m, 0), round(time.time() - age_s, 1)]


def _clear():
    state.multinode_tracks.clear()
    state.ground_truth_trails.clear()
    state.ground_truth_meta.clear()
    state.adsb_aircraft.clear()
    state.latest_mlat_verification_bytes = b"{}"


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestEmptyState:
    def setup_method(self):
        _clear()

    def test_empty_multinode_tracks(self):
        # Add a truth point but no multinode tracks
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6)])
        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        assert data["n_solves"] == 0
        assert data["n_matched"] == 0
        assert data["match_rate_pct"] == 0.0
        assert data["tracks"] == []

    def test_empty_truth_pool(self):
        # Add a multinode track but no truth
        r = _make_solve_result(33.9, -84.6)
        state.multinode_tracks[_key(r)] = r
        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        assert data["n_solves"] == 0  # truth pool empty → early exit
        assert data["n_matched"] == 0

    def test_returns_valid_json_always(self):
        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        assert "n_solves" in data
        assert "position" in data
        assert "velocity" in data
        assert "altitude" in data
        assert "by_node_count" in data
        assert "tracks" in data


class TestSingleMatch:
    def setup_method(self):
        _clear()

    def test_close_solve_matches_truth(self):
        # Truth at (33.9, -84.6), solver at (33.9001, -84.6001) ≈ 14 m error
        truth_lat, truth_lon, truth_alt = 33.9, -84.6, 10000.0
        truth_speed = math.sqrt(200.0**2 + 50.0**2)

        state.ground_truth_trails["abc123"] = deque([_trail_point(truth_lat, truth_lon, truth_alt)])
        state.ground_truth_meta["abc123"] = {
            "object_type": "aircraft",
            "is_anomalous": False,
            "speed_ms": truth_speed,
            "heading": 75.0,
        }

        r = _make_solve_result(33.9001, -84.6001, alt_m=10050.0, vel_east=200.0, vel_north=50.0)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["n_solves"] == 1
        assert data["n_matched"] == 1
        assert data["match_rate_pct"] == 100.0
        assert len(data["tracks"]) == 1

        track = data["tracks"][0]
        assert track["truth_hex"] == "abc123"
        assert track["position_error_km"] < 0.1
        assert track["altitude_error_m"] == pytest.approx(50.0, abs=1.0)
        assert track["object_type"] == "aircraft"
        assert track["is_anomalous"] is False
        assert track["n_nodes"] == 2

    def test_error_metrics_in_summary(self):
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6, 10000.0)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 200.0}

        r = _make_solve_result(33.9001, -84.6001, alt_m=10500.0, vel_east=190.0, vel_north=50.0)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["position"]["mean_km"] > 0
        assert data["position"]["median_km"] > 0
        assert data["altitude"]["mean_m"] > 0


class TestProximityThreshold:
    def setup_method(self):
        _clear()

    def test_solve_within_threshold_matched(self):
        # 3 km away — within 8 km threshold
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

        # ~3 km north of truth
        r = _make_solve_result(33.927, -84.6)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        assert data["n_matched"] == 1

    def test_solve_beyond_threshold_not_matched(self):
        # 20 km away — beyond 8 km threshold
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

        # ~20 km north
        r = _make_solve_result(34.08, -84.6)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        assert data["n_solves"] == 1
        assert data["n_matched"] == 0


class TestStaleFiltering:
    def setup_method(self):
        _clear()

    def test_stale_multinode_result_skipped(self):
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

        # timestamp 200s old → beyond 120s threshold
        old_ts_ms = int((time.time() - 200) * 1000)
        r = _make_solve_result(33.9, -84.6, ts_ms=old_ts_ms)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        assert data["n_solves"] == 0

    def test_stale_truth_point_skipped(self):
        # Truth point 120s old
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6, age_s=120)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

        r = _make_solve_result(33.9, -84.6)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)
        # truth pool is empty → early exit
        assert data["n_matched"] == 0


class TestByNodeCount:
    def setup_method(self):
        _clear()

    def test_by_node_count_populated(self):
        # Two truths and two solves: one 2-node, one 3-node
        for i, (lat, truth_lat) in enumerate([(33.9, 33.9002), (34.1, 34.1002)]):
            hex_id = f"hex{i}"
            state.ground_truth_trails[hex_id] = deque([_trail_point(lat, -84.6)])
            state.ground_truth_meta[hex_id] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

        r1 = _make_solve_result(33.9002, -84.6, n_nodes=2, ts_ms=int(time.time() * 1000) - 100)
        r2 = _make_solve_result(34.1002, -84.6, n_nodes=3, ts_ms=int(time.time() * 1000) - 200)
        state.multinode_tracks[_key(r1)] = r1
        state.multinode_tracks[_key(r2)] = r2

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert "2" in data["by_node_count"]
        assert "3" in data["by_node_count"]
        assert data["by_node_count"]["2"]["n"] == 1
        assert data["by_node_count"]["3"]["n"] == 1


class TestNoDoubleMatching:
    def setup_method(self):
        _clear()

    def test_two_solves_near_same_truth_only_one_matches(self):
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

        # Two solves both close to the same truth
        r1 = _make_solve_result(33.9001, -84.6001, ts_ms=int(time.time() * 1000) - 100)
        r2 = _make_solve_result(33.9002, -84.6002, ts_ms=int(time.time() * 1000) - 200)
        state.multinode_tracks[_key(r1)] = r1
        state.multinode_tracks[_key(r2)] = r2

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["n_solves"] == 2
        assert data["n_matched"] == 1


class TestAdsbFallback:
    def setup_method(self):
        _clear()

    def test_adsb_aircraft_used_when_no_ground_truth_trail(self):
        # No ground_truth_trails — only adsb_aircraft
        state.adsb_aircraft["aabbcc"] = {
            "hex": "aabbcc",
            "lat": 33.9,
            "lon": -84.6,
            "alt_baro": 32808,  # 10000 m
            "gs": 388.0,  # ~200 m/s
            "track": 76.0,
            "last_seen_ms": int(time.time() * 1000) - 5000,
        }

        r = _make_solve_result(33.9001, -84.6001)
        state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["n_matched"] == 1
        assert data["tracks"][0]["truth_hex"] == "aabbcc"


class TestPercentiles:
    def setup_method(self):
        _clear()

    def test_percentile_with_multiple_matches(self):
        # Three solves at 1, 2, 3 km errors respectively
        errors_km = [0.01, 0.02, 0.10]  # small, small, larger
        base_ts = int(time.time() * 1000)

        for i, err in enumerate(errors_km):
            hex_id = f"hex{i}"
            truth_lat = 33.9 + i * 0.5
            state.ground_truth_trails[hex_id] = deque([_trail_point(truth_lat, -84.6)])
            state.ground_truth_meta[hex_id] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}

            # err km north in latitude degrees ≈ err / 111.32
            solve_lat = truth_lat + err / 111.32
            r = _make_solve_result(solve_lat, -84.6, ts_ms=base_ts - i * 100)
            state.multinode_tracks[_key(r)] = r

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["n_matched"] == 3
        assert data["position"]["mean_km"] > 0
        assert data["position"]["p95_km"] >= data["position"]["median_km"]
        assert data["position"]["max_km"] >= data["position"]["p95_km"]


class TestHttpEndpoint:
    def setup_method(self):
        _clear()

    def test_endpoint_returns_200(self):
        from main import app

        client = TestClient(app)
        resp = client.get("/api/test/mlat-verification")
        assert resp.status_code == 200
        # Before first refresh the bytes are b'{}'; after refresh they have the full structure.
        assert isinstance(resp.json(), dict)

    def test_endpoint_reflects_refresh(self):
        from main import app

        client = TestClient(app)

        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6)])
        state.ground_truth_meta["abc123"] = {"object_type": "aircraft", "is_anomalous": False, "speed_ms": 0.0}
        r = _make_solve_result(33.9001, -84.6001)
        state.multinode_tracks[_key(r)] = r
        _refresh_mlat_verification()

        resp = client.get("/api/test/mlat-verification")
        data = resp.json()
        assert data["n_matched"] == 1
        assert data["tracks"][0]["truth_hex"] == "abc123"
