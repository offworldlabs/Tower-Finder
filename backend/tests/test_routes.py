"""Tests for HTTP API routes — health, detections, config, metrics, WebSocket auth."""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

# Ensure test env so JWT fallback is allowed
os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from main import app  # noqa: E402


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
        with client.websocket_connect("/ws/aircraft"):
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
        with client.websocket_connect("/ws/aircraft?token=secret-token-123"):
            pass


# ── Config (detailed) ────────────────────────────────────────────────────────

class TestConfigDetailed:
    def test_config_has_ranking(self, client):
        r = client.get("/api/config")
        cfg = r.json()
        assert "ranking" in cfg
        assert "band_priority" in cfg["ranking"]
        assert "distance_classes" in cfg["ranking"]
        assert "sort_order" in cfg["ranking"]

    def test_config_has_receiver(self, client):
        r = client.get("/api/config")
        assert "receiver" in r.json()

    def test_config_has_broadcast_bands(self, client):
        r = client.get("/api/config")
        assert "broadcast_bands" in r.json()


# ── Config PUT + reload ──────────────────────────────────────────────────────

class TestConfigReload:
    def test_put_and_reload(self, client):
        r = client.get("/api/config")
        cfg = r.json()

        new_cfg = json.loads(json.dumps(cfg))
        new_cfg["ranking"]["band_priority"]["FM"] = 0
        new_cfg["ranking"]["band_priority"]["VHF"] = 2

        r2 = client.put("/api/config", json=new_cfg)
        assert r2.status_code == 200
        assert r2.json().get("status") == "updated"

        reloaded = client.get("/api/config").json()
        assert reloaded["ranking"]["band_priority"]["FM"] == 0
        assert reloaded["ranking"]["band_priority"]["VHF"] == 2

        # Restore original
        r3 = client.put("/api/config", json=cfg)
        assert r3.status_code == 200
        restored = client.get("/api/config").json()
        assert restored["ranking"]["band_priority"]["VHF"] == cfg["ranking"]["band_priority"]["VHF"]


# ── Elevation ─────────────────────────────────────────────────────────────────

class TestElevation:
    @pytest.mark.external
    def test_sydney_elevation(self, client):
        r = client.get("/api/elevation", params={"lat": -33.8688, "lon": 151.2093})
        assert r.status_code == 200
        body = r.json()
        assert "elevation_m" in body
        assert isinstance(body["elevation_m"], (int, float))
        assert 0 <= body["elevation_m"] <= 200

    @pytest.mark.external
    def test_denver_elevation(self, client):
        r = client.get("/api/elevation", params={"lat": 39.7392, "lon": -104.9903})
        assert r.status_code == 200
        assert r.json()["elevation_m"] > 1500

    def test_invalid_lat_returns_422(self, client):
        r = client.get("/api/elevation", params={"lat": 999, "lon": 0})
        assert r.status_code == 422


# ── Towers parameter validation ──────────────────────────────────────────────

class TestTowersValidation:
    def test_missing_lat_lon_returns_422(self, client):
        r = client.get("/api/towers")
        assert r.status_code == 422

    def test_lat_out_of_range_returns_422(self, client):
        r = client.get("/api/towers", params={"lat": 999, "lon": 0})
        assert r.status_code == 422

    def test_invalid_source_returns_400(self, client):
        r = client.get("/api/towers", params={"lat": 0, "lon": 0, "source": "xx"})
        assert r.status_code == 400


# ── Tower usage statistics ───────────────────────────────────────────────────

class TestTowerStats:
    @pytest.fixture(autouse=True)
    def cleanup_stats(self):
        yield
        stats_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tower_stats.json")
        if os.path.exists(stats_path):
            os.remove(stats_path)

    _HEADERS = {"X-API-Key": "test-key-abc123"}

    def test_post_selection(self, client):
        r = client.post("/api/stats/tower-selection", json={
            "node_id": "test-node-1",
            "tower_callsign": "ABC7",
            "tower_frequency_mhz": 177.5,
            "tower_lat": -33.8,
            "tower_lon": 151.2,
            "node_lat": -33.9,
            "node_lon": 151.1,
            "source": "au",
        }, headers=self._HEADERS)
        assert r.status_code == 200
        assert r.json()["status"] == "recorded"

    def test_missing_fields_returns_400(self, client):
        r = client.post("/api/stats/tower-selection", json={"node_id": "x"}, headers=self._HEADERS)
        assert r.status_code == 400

    def test_get_summary(self, client):
        # Record two selections first
        for nid in ("test-s-1", "test-s-2"):
            client.post("/api/stats/tower-selection", json={
                "node_id": nid,
                "tower_callsign": "ABC7",
                "tower_frequency_mhz": 177.5,
                "tower_lat": -33.8,
                "tower_lon": 151.2,
                "node_lat": -34.0,
                "node_lon": 151.0,
                "source": "au",
            }, headers=self._HEADERS)
        r = client.get("/api/stats/summary")
        assert r.status_code == 200
        s = r.json()
        assert "total_selections" in s
        assert "unique_towers" in s
        assert isinstance(s.get("tower_usage"), list)
        assert s["total_selections"] >= 2


# ── Archive API ──────────────────────────────────────────────────────────────

class TestArchiveAPI:
    def test_archive_list(self, client):
        r = client.get("/api/data/archive")
        assert r.status_code == 200
        body = r.json()
        assert "files" in body
        assert "count" in body

    def test_missing_archive_file_returns_404(self, client):
        r = client.get("/api/data/archive/nonexistent/path.json")
        assert r.status_code == 404
