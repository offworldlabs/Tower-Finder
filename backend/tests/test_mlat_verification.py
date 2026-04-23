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
        # Truth point 65s old — just over the 60s rejection threshold
        state.ground_truth_trails["abc123"] = deque([_trail_point(33.9, -84.6, age_s=65)])
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


class TestRealSolverIntegration:
    """Tests that run the actual solver on synthetic measurements.

    These cover the path:
      synthetic measurements → solve_multinode() → state.multinode_tracks
      → _refresh_mlat_verification() → matched result

    This is distinct from the other test classes, which inject pre-built solve
    results directly and never exercise the solver itself.
    """

    # Five nodes spread around the NYC area (target: 40.73°N, 73.95°W).
    # node_a and node_b reproduce the geometry used in test_solver.py.
    # node_c/d/e add diversity for the 3–5-node parametrized cases.
    _NODE_CONFIGS = {
        "node_a": {
            "rx_lat": 40.7128, "rx_lon": -74.0060, "rx_alt_ft": 100,
            "tx_lat": 40.78, "tx_lon": -73.95, "tx_alt_ft": 500,
            "fc_hz": 100e6,
        },
        "node_b": {
            "rx_lat": 40.75, "rx_lon": -73.90, "rx_alt_ft": 150,
            "tx_lat": 40.70, "tx_lon": -73.85, "tx_alt_ft": 400,
            "fc_hz": 100e6,
        },
        "node_c": {
            "rx_lat": 40.64, "rx_lon": -74.08, "rx_alt_ft": 80,
            "tx_lat": 40.60, "tx_lon": -74.15, "tx_alt_ft": 350,
            "fc_hz": 100e6,
        },
        "node_d": {
            "rx_lat": 40.83, "rx_lon": -73.87, "rx_alt_ft": 200,
            "tx_lat": 40.89, "tx_lon": -73.82, "tx_alt_ft": 450,
            "fc_hz": 100e6,
        },
        "node_e": {
            "rx_lat": 40.73, "rx_lon": -74.18, "rx_alt_ft": 120,
            "tx_lat": 40.68, "tx_lon": -74.25, "tx_alt_ft": 400,
            "fc_hz": 100e6,
        },
    }

    def setup_method(self):
        _clear()

    @staticmethod
    def _make_solver_input(node_configs, target_lat, target_lon, target_alt_km,
                           vel_east=0.0, vel_north=0.0):
        """Build a solver_input dict from a known target position.

        Computes exact delay_us and doppler_hz for each node using the bistatic
        forward models so that the solver should converge back to the truth.
        """
        from retina_geolocator.bistatic_models import bistatic_delay, bistatic_doppler
        from retina_geolocator.multinode_solver import _lla_to_enu_km

        ref_lat, ref_lon = target_lat, target_lon
        target_enu = _lla_to_enu_km(
            target_lat, target_lon, target_alt_km * 1000.0, ref_lat, ref_lon, 0.0
        )
        measurements = []
        for nid, cfg in node_configs.items():
            rx_enu = _lla_to_enu_km(
                cfg["rx_lat"], cfg["rx_lon"], cfg.get("rx_alt_ft", 0) * 0.3048,
                ref_lat, ref_lon, 0.0,
            )
            tx_enu = _lla_to_enu_km(
                cfg["tx_lat"], cfg["tx_lon"], cfg.get("tx_alt_ft", 0) * 0.3048,
                ref_lat, ref_lon, 0.0,
            )
            fc = cfg.get("fc_hz", 100e6)
            delay = bistatic_delay(target_enu, tx_enu, rx_enu)
            doppler = bistatic_doppler(
                target_enu, (vel_east, vel_north, 0.0), tx_enu, rx_enu, fc
            )
            measurements.append(
                {"node_id": nid, "delay_us": delay, "doppler_hz": doppler, "snr": 15.0}
            )
        return {
            "initial_guess": {
                "lat": target_lat + 0.01,
                "lon": target_lon + 0.01,
                "alt_km": target_alt_km,
            },
            "measurements": measurements,
            "n_nodes": len(node_configs),
            "timestamp_ms": int(time.time() * 1000),
        }

    # Position threshold per n_nodes:
    # - n=2: 5 equations (2 delay + 2 doppler + 1 altitude soft constraint) for
    #        6 unknowns — marginally underdetermined for velocity, so position
    #        convergence is ~0.9 km with a noise-free initial guess 1.4 km away.
    # - n≥3: system becomes well-overdetermined; solver converges sub-metre.
    @pytest.mark.parametrize("n_nodes,pos_threshold_km", [
        (2, 1.0),
        (3, 0.5),
        (4, 0.5),
        (5, 0.5),
    ])
    def test_real_solver_converges_n_nodes(self, n_nodes, pos_threshold_km):
        """Solver converges from synthetic measurements for 2–5 nodes.

        Asserts position error < pos_threshold_km and altitude error < 200 m.
        """
        from retina_geolocator.multinode_solver import solve_multinode

        target_lat, target_lon, target_alt_km = 40.73, -73.95, 8.0
        configs = dict(list(self._NODE_CONFIGS.items())[:n_nodes])

        solver_input = self._make_solver_input(configs, target_lat, target_lon, target_alt_km)
        result = solve_multinode(solver_input, configs)

        assert result is not None, f"Solver failed to converge with {n_nodes} nodes"
        assert result["success"] is True

        state.multinode_tracks[_key(result)] = result
        state.ground_truth_trails["abc123"] = deque(
            [_trail_point(target_lat, target_lon, target_alt_km * 1000.0)]
        )
        state.ground_truth_meta["abc123"] = {
            "object_type": "aircraft",
            "is_anomalous": False,
            "speed_ms": 0.0,
        }

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["n_solves"] == 1
        assert data["n_matched"] == 1
        assert data["match_rate_pct"] == 100.0
        assert data["position"]["mean_km"] < pos_threshold_km
        assert data["tracks"][0]["altitude_error_m"] < 200
        assert data["tracks"][0]["truth_hex"] == "abc123"

    def test_real_solver_moving_target(self):
        """Solver recovers position and speed for a moving target using all 5 nodes.

        With 5 nodes the system is well-overdetermined (21 equations, 6 unknowns)
        and velocity should converge to within 20 m/s of the true speed.
        """
        from retina_geolocator.multinode_solver import solve_multinode

        vel_east, vel_north = 150.0, 80.0
        truth_speed = math.sqrt(vel_east**2 + vel_north**2)
        target_lat, target_lon, target_alt_km = 40.73, -73.95, 8.0

        solver_input = self._make_solver_input(
            self._NODE_CONFIGS, target_lat, target_lon, target_alt_km,
            vel_east=vel_east, vel_north=vel_north,
        )
        result = solve_multinode(solver_input, self._NODE_CONFIGS)

        assert result is not None, "Solver failed to converge on moving target"
        assert result["success"] is True

        state.multinode_tracks[_key(result)] = result
        state.ground_truth_trails["abc123"] = deque(
            [_trail_point(target_lat, target_lon, target_alt_km * 1000.0)]
        )
        state.ground_truth_meta["abc123"] = {
            "object_type": "aircraft",
            "is_anomalous": False,
            "speed_ms": truth_speed,
        }

        _refresh_mlat_verification()
        data = orjson.loads(state.latest_mlat_verification_bytes)

        assert data["n_matched"] == 1
        assert data["position"]["mean_km"] < 0.5
        assert data["tracks"][0]["altitude_error_m"] < 200
        assert data["velocity"]["mean_ms"] < 20.0


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
