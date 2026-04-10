"""Tests for HTTP API routes — health, detections, config, metrics, WebSocket auth."""

import os
import time

import pytest
from fastapi.testclient import TestClient

# Ensure test env so JWT fallback is allowed
os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from main import app  # noqa: E402
from core import state  # noqa: E402


@pytest.fixture()
def client():
    """Synchronous test client — does not run lifespan (no TCP, no tasks)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── Health ────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ── Detections ────────────────────────────────────────────────────────────────

class TestDetections:
    def test_missing_api_key_returns_401(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test", "timestamp": time.time()},
        )
        assert r.status_code == 401

    def test_valid_detection_accepted(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-http", "timestamp": time.time(), "detections": []},
            headers={"X-API-Key": "test-key-abc123"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "frames_queued" in body

    def test_no_timestamp_frame_skipped(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-http", "frames": [{"no_ts": True}]},
            headers={"X-API-Key": "test-key-abc123"},
        )
        assert r.status_code == 200
        assert r.json()["frames_queued"] == 0

    def test_bulk_endpoint_validates_shape(self, client):
        r = client.post(
            "/api/radar/detections/bulk",
            json={"nodes": "not-a-list"},
            headers={"X-API-Key": "test-key-abc123"},
        )
        assert r.status_code == 422  # Pydantic validation error

    def test_bulk_accepts_valid(self, client):
        r = client.post(
            "/api/radar/detections/bulk",
            json={"nodes": [{"node_id": "bulk-1", "frames": [{"timestamp": 1.0}]}]},
            headers={"X-API-Key": "test-key-abc123"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["frames_queued"] >= 1


# ── Request size limit ────────────────────────────────────────────────────────

class TestRequestSizeLimit:
    def test_oversized_body_rejected(self, client):
        """Requests declaring Content-Length > MAX_REQUEST_BODY_BYTES get 413."""
        r = client.post(
            "/api/radar/detections",
            content=b"x" * 100,  # small body but big content-length header
            headers={
                "Content-Length": str(10 * 1024 * 1024),  # 10 MB
                "Content-Type": "application/json",
                "X-API-Key": "test-key-abc123",
            },
        )
        assert r.status_code == 413


# ── CORS ──────────────────────────────────────────────────────────────────────

class TestCORS:
    def test_cors_allows_configured_origin(self, client):
        r = client.options(
            "/api/health",
            headers={
                "Origin": "https://retina.fm",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "https://retina.fm"

    def test_cors_rejects_unknown_origin(self, client):
        r = client.options(
            "/api/health",
            headers={
                "Origin": "https://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") != "https://evil.com"

    def test_cors_restricts_methods(self, client):
        r = client.options(
            "/api/health",
            headers={
                "Origin": "https://retina.fm",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        allow_methods = r.headers.get("access-control-allow-methods", "")
        assert "DELETE" not in allow_methods


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)


# ── Receiver / aircraft JSON ─────────────────────────────────────────────────

class TestRadarData:
    def test_receiver_json(self, client):
        r = client.get("/api/radar/data/receiver.json")
        assert r.status_code == 200
        data = r.json()
        assert "lat" in data and "lon" in data

    def test_aircraft_json(self, client):
        r = client.get("/api/radar/data/aircraft.json")
        assert r.status_code == 200


# ── WebSocket auth ────────────────────────────────────────────────────────────

class TestWebSocketAuth:
    def test_ws_open_when_no_token_configured(self, client):
        """When WS_AUTH_TOKEN is empty, any client can connect."""
        # WS_AUTH_TOKEN defaults to "" — should accept
        with client.websocket_connect("/ws/aircraft") as ws:
            # Connection succeeded — close immediately
            pass

    def test_ws_rejected_with_bad_token(self, client, monkeypatch):
        """When WS_AUTH_TOKEN is set, invalid tokens get rejected."""
        import routes.streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "secret-token-123")
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/aircraft?token=wrong") as ws:
                ws.receive_text()

    def test_ws_accepted_with_valid_token(self, client, monkeypatch):
        """When WS_AUTH_TOKEN is set, valid tokens get accepted."""
        import routes.streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "secret-token-123")
        with client.websocket_connect("/ws/aircraft?token=secret-token-123") as ws:
            pass
