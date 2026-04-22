"""Additional branch coverage for the /api/health endpoint.

Covers the branches that are not exercised by test_towers_routes.py:
  - solver_queue_drops > 0
  - solver_queue_high
  - solver_latency_high
  - no_active_tracks
  - anomaly_flood
  - solver_accuracy_degraded
  - high_miss_rate
  - node_dropout
"""

import os

import orjson
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


def _assert_degraded(r):
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"


class TestHealthDegradedBranches:
    def test_solver_queue_drops(self, client):
        state.solver_queue_drops = 5
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.solver_queue_drops = 0

    def test_solver_queue_backpressure(self, client, monkeypatch):
        # Solver worker threads drain the real queue, so we stub qsize/maxsize
        # rather than filling the queue (which is racy).
        class _StubQueue:
            def qsize(self):
                return 6
            maxsize = 10
        monkeypatch.setattr(state, "solver_queue", _StubQueue())
        _assert_degraded(client.get("/api/health"))

    def test_solver_latency_high(self, client):
        orig = state.solver_last_latency_s
        state.solver_last_latency_s = 45.0
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.solver_last_latency_s = orig

    def test_no_active_tracks(self, client):
        orig_frames = state.frames_processed
        orig_aircraft = dict(state.adsb_aircraft)
        orig_tracks = dict(state.multinode_tracks)
        state.frames_processed = 1000
        state.adsb_aircraft.clear()
        state.multinode_tracks.clear()
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.frames_processed = orig_frames
            state.adsb_aircraft.update(orig_aircraft)
            state.multinode_tracks.update(orig_tracks)

    def test_anomaly_flood(self, client):
        # >10 aircraft, more than half flagged anomalous
        for i in range(20):
            state.adsb_aircraft[f"ac{i:03d}"] = {"hex": f"ac{i:03d}"}
        state.anomaly_hexes.update(f"ac{i:03d}" for i in range(15))
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.adsb_aircraft.clear()
            state.anomaly_hexes.clear()

    def test_solver_accuracy_degraded(self, client):
        orig = state.latest_accuracy_bytes
        state.latest_accuracy_bytes = orjson.dumps({"n_samples": 50, "mean_km": 25.0})
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.latest_accuracy_bytes = orig

    def test_high_miss_rate(self, client):
        orig = dict(state.latest_missed_detections)
        state.latest_missed_detections.clear()
        state.latest_missed_detections.update({
            "n1": {"in_range": 10, "miss_rate": 0.8},
            "n2": {"in_range": 20, "miss_rate": 0.9},
        })
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.latest_missed_detections.clear()
            state.latest_missed_detections.update(orig)

    def test_node_dropout(self, client):
        orig_peak = state.peak_connected_nodes
        state.peak_connected_nodes = 100
        # No connected nodes → active_nodes=0, far below 80% threshold
        with state.connected_nodes_lock:
            state.connected_nodes.clear()
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.peak_connected_nodes = orig_peak

    def test_invalid_accuracy_bytes_handled(self, client):
        """Malformed latest_accuracy_bytes should not crash health."""
        orig = state.latest_accuracy_bytes
        state.latest_accuracy_bytes = b"{not json"
        try:
            r = client.get("/api/health")
            assert r.status_code == 200  # exception swallowed
        finally:
            state.latest_accuracy_bytes = orig
