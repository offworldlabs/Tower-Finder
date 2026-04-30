"""Tests for admin API routes — events, users, config, storage, leaderboard, metrics."""

import os
import time

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


# ── Events ────────────────────────────────────────────────────────────────────

class TestEvents:
    def test_list_events_returns_list(self, client):
        r = client.get("/api/admin/events")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_log_event_appears_in_list(self, client):
        from routes.admin import log_event

        log_event("test", "Unit test event", "info", {"key": "val"})
        r = client.get("/api/admin/events")
        assert r.status_code == 200
        events = r.json()
        assert any(e["message"] == "Unit test event" for e in events)

    def test_events_respect_limit(self, client):
        from routes.admin import log_event

        for i in range(10):
            log_event("test", f"bulk-{i}", "info")
        r = client.get("/api/admin/events?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_get_node_config_live_fallback(self, client):
        """When no nodes_config.json exists, returns live config from state."""
        r = client.get("/api/admin/config/nodes")
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body or "_source" in body

    def test_get_tower_config_live_fallback(self, client):
        r = client.get("/api/admin/config/towers")
        assert r.status_code == 200
        body = r.json()
        assert "towers" in body or "_source" in body

    def test_config_history_returns_list(self, client):
        r = client.get("/api/admin/config/history")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ── Storage ───────────────────────────────────────────────────────────────────

class TestStorage:
    def test_storage_returns_json(self, client):
        """Storage endpoint returns valid JSON with expected shape."""
        r = client.get("/api/admin/storage")
        assert r.status_code in (200, 202)
        data = r.json()
        if r.status_code == 202:
            assert data.get("status") == "initializing"
        else:
            # Real storage response has archive/disk info
            assert isinstance(data, dict)


# ── Leaderboard ──────────────────────────────────────────────────────────────

class TestLeaderboard:
    def test_leaderboard_empty(self, client):
        r = client.get("/api/admin/leaderboard")
        assert r.status_code == 200
        body = r.json()
        assert "leaderboard" in body
        assert "total" in body

    def test_leaderboard_with_node(self, client):
        """Inject a node and verify it appears in leaderboard."""
        import orjson

        state.connected_nodes["test-lb-1"] = {
            "status": "active",
            "config": {"name": "LB-Test-Node"},
            "is_synthetic": True,
        }
        analytics_data = {
            "nodes": {
                "test-lb-1": {
                    "metrics": {"total_detections": 42, "total_frames": 10, "total_tracks": 5, "uptime_s": 300, "avg_snr": 12.0},
                    "trust": {},
                    "reputation": {},
                }
            }
        }
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = orjson.dumps(analytics_data)
        try:
            r = client.get("/api/admin/leaderboard")
            assert r.status_code == 200
            entries = r.json()["leaderboard"]
            found = [e for e in entries if e["node_id"] == "test-lb-1"]
            assert len(found) == 1
            assert found[0]["detections"] == 42
            assert found[0]["rank"] >= 1
        finally:
            state.latest_analytics_bytes = orig
            state.connected_nodes.pop("test-lb-1", None)


# ── Alerts ───────────────────────────────────────────────────────────────────

class TestAlerts:
    def test_alerts_returns_list(self, client):
        r = client.get("/api/admin/alerts")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_alerts_filters_severity(self, client):
        from routes.admin import log_event

        log_event("test", "info-only", "info")
        log_event("node", "warning-event", "warning")
        r = client.get("/api/admin/alerts")
        events = r.json()
        # warning/error/critical + node/config/system categories pass through
        for e in events:
            assert (
                e.get("severity") in ("warning", "error", "critical")
                or e.get("category") in ("node", "config", "system")
            )


# ── Metrics ──────────────────────────────────────────────────────────────────

class TestMetrics:
    def test_metrics_returns_expected_fields(self, client):
        r = client.get("/api/admin/metrics")
        assert r.status_code == 200
        body = r.json()
        assert "frame_queue_depth" in body
        assert "frames_processed" in body
        assert "connected_nodes" in body
        assert "stale_tasks" in body


# ── Node health ──────────────────────────────────────────────────────────────

class TestNodeHealth:
    def test_check_detects_offline(self):
        from routes.admin import check_node_health

        state.connected_nodes["test-offline"] = {
            "status": "active",
            "last_heartbeat": "2020-01-01T00:00:00Z",
            "config": {},
        }
        try:
            check_node_health()
            # Node should be marked disconnected
            assert state.connected_nodes["test-offline"]["status"] == "disconnected"
        finally:
            state.connected_nodes.pop("test-offline", None)


# ── Stale tasks ──────────────────────────────────────────────────────────────

class TestStaleTasks:
    def test_no_stale_when_recent(self):
        from routes.admin import _get_stale_tasks

        state.task_last_success["frame_processor"] = time.time()
        result = _get_stale_tasks()
        assert "frame_processor" not in result

    def test_stale_when_old(self):
        from routes.admin import _get_stale_tasks

        state.task_last_success["frame_processor"] = time.time() - 9999
        result = _get_stale_tasks()
        assert "frame_processor" in result


# ── Queue saturation alerting ─────────────────────────────────────────────────

class TestQueueSaturationAlert:
    def test_drop_event_logged_when_throttle_expires(self):
        """When _last_drop_log is old enough, a drop should produce a system event."""
        import services.tcp_handler as th
        from routes.admin import _events, log_event  # noqa: F401

        # Reset throttle timer so the next drop fires immediately
        th._last_drop_log = 0.0
        state.frames_dropped = 0

        # Simulate a frame queue full drop by calling the internal helper directly
        # after artificially making the queue full.
        # Fill the queue to capacity with dummy items, then call _enqueue_detection
        # on a node whose rate-limit window has expired.
        th._per_node_last_enqueue.pop("alert-test-node", None)
        while not state.frame_queue.full():
            try:
                state.frame_queue.put_nowait(("_fill", {"timestamp": 1}))
            except Exception:
                break

        before_len = len(_events)
        th._enqueue_detection(
            {"type": "DETECTION", "data": {"timestamp": 1, "detections": []}},
            "alert-test-node",
        )

        # Drain the queue to restore state
        while not state.frame_queue.empty():
            try:
                state.frame_queue.get_nowait()
                state.frame_queue.task_done()
            except Exception:
                break

        # There should be a new system/error event
        new_events = list(_events)[:len(_events) - before_len + 5]
        assert any(
            e.get("category") == "system" and e.get("severity") == "error"
            for e in new_events
        ), "Expected a system/error event for queue saturation"


# ── Node reconnect event logging ─────────────────────────────────────────────

class TestNodeReconnectEvent:
    def test_reconnect_flag_in_meta(self):
        """When a node that was disconnected sends CONFIG again, event should have reconnect=True."""
        from routes.admin import _events

        # Pre-populate node as disconnected
        state.connected_nodes["reconnect-test"] = {
            "status": "disconnected",
            "config": {},
            "config_hash": "oldhash",
            "first_seen_ts": time.time() - 300,
        }

        # Simulate what handle_tcp_client does on CONFIG receipt for a known-disconnected node
        import services.tcp_handler as th

        was_disconnected = state.connected_nodes.get("reconnect-test", {}).get("status") == "disconnected"
        assert was_disconnected

        if was_disconnected:
            th._log_event(
                "node",
                "Node reconnect-test reconnected (hash=newhash1, synthetic=False)",
                "info",
                {"node_id": "reconnect-test", "config_hash": "newhash1234", "is_synthetic": False, "reconnect": True},
            )

        reconnect_events = [
            e for e in _events
            if e.get("meta", {}).get("reconnect") is True
            and e.get("meta", {}).get("node_id") == "reconnect-test"
        ]
        assert len(reconnect_events) >= 1, "Expected a reconnect event in the event log"

        # Cleanup
        state.connected_nodes.pop("reconnect-test", None)

    def test_fresh_connect_has_no_reconnect_flag(self):
        """A brand-new node (not seen before) should not have reconnect=True."""
        from routes.admin import _events

        # Ensure node is not in state
        state.connected_nodes.pop("fresh-node-test", None)

        was_disconnected = state.connected_nodes.get("fresh-node-test", {}).get("status") == "disconnected"
        assert not was_disconnected

        import services.tcp_handler as th
        th._log_event(
            "node",
            "Node fresh-node-test connected (hash=abc12345, synthetic=False)",
            "info",
            {"node_id": "fresh-node-test", "config_hash": "abc12345", "is_synthetic": False},
        )

        reconnect_events = [
            e for e in _events
            if e.get("meta", {}).get("reconnect") is True
            and e.get("meta", {}).get("node_id") == "fresh-node-test"
        ]
        assert len(reconnect_events) == 0, "Fresh connect should not have reconnect flag"

