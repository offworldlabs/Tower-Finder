"""Tests for streaming endpoints (/ws/aircraft/live and /api/radar/stream SSE)."""

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


class TestLiveWebSocket:
    def test_live_ws_accepts_no_token(self, client, monkeypatch):
        from routes import streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
        with client.websocket_connect("/ws/aircraft/live"):
            pass
        # Client removed from set on close
        # (no assertion needed — just verifies the socket opened/closed cleanly)

    def test_live_ws_sends_initial_snapshot(self, client, monkeypatch):
        from routes import streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")

        snapshot = orjson.dumps({
            "aircraft": [{"hex": "abc123", "lat": 0, "lon": 0}],
            "now": 1.0,
        })
        monkeypatch.setattr(state, "latest_real_aircraft_json_bytes", snapshot)

        with client.websocket_connect("/ws/aircraft/live") as ws:
            msg = ws.receive_text()
            payload = orjson.loads(msg)
            assert payload["aircraft"][0]["hex"] == "abc123"

    def test_live_ws_rejects_invalid_token(self, client, monkeypatch):
        from routes import streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "secret-xyz")
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/aircraft/live?token=wrong") as ws:
                ws.receive_text()

    def test_live_ws_accepts_valid_token(self, client, monkeypatch):
        from routes import streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "secret-xyz")
        with client.websocket_connect("/ws/aircraft/live?token=secret-xyz"):
            pass


class TestAircraftWSInitialSnapshot:
    def test_initial_snapshot_sent_if_aircraft_present(self, client, monkeypatch):
        from routes import streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")

        payload = {"aircraft": [{"hex": "d00d", "lat": 1, "lon": 2}], "now": 1.0}
        # latest_aircraft_json is a dict guard; latest_aircraft_json_bytes is the body
        monkeypatch.setattr(state, "latest_aircraft_json", payload)
        monkeypatch.setattr(state, "latest_aircraft_json_bytes", orjson.dumps(payload))

        with client.websocket_connect("/ws/aircraft") as ws:
            msg = ws.receive_text()
            body = orjson.loads(msg)
            assert body["aircraft"][0]["hex"] == "d00d"

    def test_no_initial_send_when_empty(self, client, monkeypatch):
        from routes import streaming as streaming_mod
        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
        monkeypatch.setattr(state, "latest_aircraft_json", {})
        # If we don't call receive, the socket will just stay open; close it.
        with client.websocket_connect("/ws/aircraft"):
            pass
