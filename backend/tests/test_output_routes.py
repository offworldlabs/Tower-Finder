"""Tests for public output API routes — solver aircraft, ground truth."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_state():
    """Clean up injected state after each test."""
    yield
    state.connected_nodes.pop("real-node-1", None)
    state.connected_nodes.pop("synth-node-1", None)
    state.ground_truth_trails.clear()
    state.ground_truth_meta.clear()
    state.external_adsb_cache.clear()


# ── Solver aircraft ──────────────────────────────────────────────────────────

class TestSolverAircraft:
    def test_solver_aircraft_empty(self, client):
        r = client.get("/api/v1/solver/aircraft")
        assert r.status_code == 200
        body = r.json()
        assert "aircraft" in body
        assert "count" in body
        assert "timestamp" in body

    def test_solver_aircraft_with_data(self, client):
        state.latest_aircraft_json = {
            "aircraft": [
                {"hex": "ABC123", "lat": 33.45, "lon": -112.07, "node_id": "n1", "multinode": False},
                {"hex": "DEF456", "lat": 34.0, "lon": -111.0, "node_id": "n2", "multinode": False},
            ]
        }
        try:
            r = client.get("/api/v1/solver/aircraft")
            assert r.status_code == 200
            body = r.json()
            assert body["count"] == 2
            assert body["aircraft"][0]["hex"] == "ABC123"
        finally:
            state.latest_aircraft_json = {}

    def test_solver_aircraft_real_only(self, client):
        """real_only=true filters to aircraft from non-synthetic nodes."""
        state.connected_nodes["real-node-1"] = {"is_synthetic": False, "status": "active"}
        state.connected_nodes["synth-node-1"] = {"is_synthetic": True, "status": "active"}
        state.latest_aircraft_json = {
            "aircraft": [
                {"hex": "REAL01", "lat": 33.45, "lon": -112.07, "node_id": "real-node-1", "multinode": False},
                {"hex": "SYNTH01", "lat": 34.0, "lon": -111.0, "node_id": "synth-node-1", "multinode": False},
            ]
        }
        try:
            r = client.get("/api/v1/solver/aircraft?real_only=true")
            assert r.status_code == 200
            body = r.json()
            assert body["real_only"] is True
            assert body["count"] == 1
            assert body["aircraft"][0]["hex"] == "REAL01"
        finally:
            state.latest_aircraft_json = {}

    def test_solver_aircraft_multinode_real(self, client):
        """Multinode aircraft with at least one real contributing node passes real_only filter."""
        state.connected_nodes["real-node-1"] = {"is_synthetic": False, "status": "active"}
        state.latest_aircraft_json = {
            "aircraft": [
                {
                    "hex": "MULTI01",
                    "lat": 33.45,
                    "lon": -112.07,
                    "node_id": "synth-node-1",
                    "multinode": True,
                    "contributing_node_ids": ["synth-node-1", "real-node-1"],
                },
            ]
        }
        try:
            r = client.get("/api/v1/solver/aircraft?real_only=true")
            assert r.status_code == 200
            assert r.json()["count"] == 1
        finally:
            state.latest_aircraft_json = {}


# ── Format aircraft ─────────────────────────────────────────────────────────

class TestFormatAircraft:
    def test_format_includes_expected_keys(self):
        from routes.output import _format_aircraft

        ac = {
            "hex": "ABC",
            "lat": 33.0,
            "lon": -112.0,
            "alt_baro": 10000,
            "gs": 250,
            "track": 180,
            "position_source": "solver_adsb_seed",
            "multinode": True,
            "n_nodes": 3,
            "contributing_node_ids": ["a", "b", "c"],
            "extra_field": "should_not_appear",
        }
        result = _format_aircraft(ac)
        assert result["hex"] == "ABC"
        assert result["multinode"] is True
        assert result["n_nodes"] == 3
        assert "extra_field" not in result

    def test_format_defaults(self):
        from routes.output import _format_aircraft

        result = _format_aircraft({})
        assert result["hex"] is None
        assert result["multinode"] is False
        assert result["n_nodes"] == 1
        assert result["contributing_node_ids"] == []


# ── Real node IDs ────────────────────────────────────────────────────────────

class TestRealNodeIds:
    def test_real_node_ids(self):
        from routes.output import _real_node_ids

        state.connected_nodes["real-node-1"] = {"is_synthetic": False}
        state.connected_nodes["synth-node-1"] = {"is_synthetic": True}
        try:
            ids = _real_node_ids()
            assert "real-node-1" in ids
            assert "synth-node-1" not in ids
        finally:
            state.connected_nodes.pop("real-node-1", None)
            state.connected_nodes.pop("synth-node-1", None)


# ── Ground truth ─────────────────────────────────────────────────────────────

class TestGroundTruth:
    def test_ground_truth_empty(self, client):
        r = client.get("/api/v1/ground-truth/aircraft")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["source"] == "simulation"

    def test_ground_truth_with_trails(self, client):
        from collections import deque

        state.ground_truth_trails["GT01"] = deque(
            [{"lat": 33.45, "lon": -112.07, "ts": 1.0}],
            maxlen=100,
        )
        state.ground_truth_meta["GT01"] = {
            "object_type": "aircraft",
            "is_anomalous": False,
            "speed_ms": 250.0,
            "heading": 90,
        }
        r = client.get("/api/v1/ground-truth/aircraft")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        ac = body["aircraft"][0]
        assert ac["hex"] == "GT01"
        assert ac["object_type"] == "aircraft"
        assert len(ac["trail"]) == 1

    def test_ground_truth_real_empty(self, client):
        r = client.get("/api/v1/ground-truth/real")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["source"] == "opensky_network"

    def test_ground_truth_real_with_data(self, client):
        state.external_adsb_cache["REAL01"] = {
            "lat": 33.45,
            "lon": -112.07,
            "alt_m": 10000,
            "velocity": 250,
            "heading": 180,
        }
        r = client.get("/api/v1/ground-truth/real")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["aircraft"][0]["hex"] == "REAL01"


# ── API docs ─────────────────────────────────────────────────────────────────

class TestApiDocs:
    def test_docs_returns_html(self, client):
        r = client.get("/api/v1/docs")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
