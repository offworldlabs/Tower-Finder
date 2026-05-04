"""Tests for tower-finding, health, and helper functions."""

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


# ── _detect_source ───────────────────────────────────────────────────────────

class TestDetectSource:
    def test_us_mainland(self):
        from routes.towers import _detect_source

        assert _detect_source(34.05, -118.25) == "us"

    def test_australia(self):
        from routes.towers import _detect_source

        assert _detect_source(-33.87, 151.21) == "au"

    def test_canada(self):
        from routes.towers import _detect_source

        assert _detect_source(45.42, -75.69) == "ca"

    def test_hawaii(self):
        from routes.towers import _detect_source

        assert _detect_source(21.31, -157.86) == "us"

    def test_alaska(self):
        from routes.towers import _detect_source

        assert _detect_source(64.2, -152.5) == "us"

    def test_unknown_defaults_to_us(self):
        from routes.towers import _detect_source

        assert _detect_source(48.85, 2.35) == "us"  # Paris → falls through to us


# ── Health endpoint ──────────────────────────────────────────────────────────

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_degraded_stale_task(self, client):
        import time

        state.task_last_success["frame_processor"] = time.time() - 9999
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert "issues" not in body  # details are logged, not exposed publicly
        finally:
            state.task_last_success.pop("frame_processor", None)

    def test_health_degraded_queue_saturated(self, client):
        """Fill the frame queue past 90% to trigger saturation warning."""
        import queue

        orig_queue = state.frame_queue
        # Create a small queue and fill it
        small_q = queue.Queue(maxsize=10)
        for i in range(10):
            small_q.put(i)
        state.frame_queue = small_q
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert "issues" not in body  # details are logged, not exposed publicly
        finally:
            state.frame_queue = orig_queue


# ── Tower search validation ──────────────────────────────────────────────────

class TestTowerSearch:
    def test_missing_lat_lon(self, client):
        r = client.get("/api/towers")
        assert r.status_code == 422  # Missing required query params

    def test_invalid_source(self, client):
        r = client.get("/api/towers?lat=33.45&lon=-112.07&source=invalid")
        assert r.status_code == 400
        assert "Invalid source" in r.json()["detail"]

    def test_lat_out_of_range(self, client):
        r = client.get("/api/towers?lat=100&lon=0")
        assert r.status_code == 422


# ── Config endpoints ─────────────────────────────────────────────────────────

class TestTowerConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
