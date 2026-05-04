"""Integration tests for /api/radar/detections and /api/radar/detections/bulk,
plus unit tests for _check_rate_limit.
"""

import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app

VALID_KEY = "test-key-abc123"
HEADERS_OK = {"X-API-Key": VALID_KEY}


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_radar_state():
    """Remove any nodes / rate buckets touched by a test."""
    from core import state

    yield

    for node_id in list(state.connected_nodes.keys()):
        if node_id.startswith("test-") or node_id.startswith("http-") or node_id.startswith("bulk-"):
            state.connected_nodes.pop(node_id, None)
    state.rate_buckets.clear()


# ── Auth tests ────────────────────────────────────────────────────────────────


class TestRadarDetectionsAuth:
    def test_missing_api_key_returns_401(self, client):
        r = client.post("/api/radar/detections", json={"node_id": "test-node"})
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-node"},
            headers={"X-API-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_correct_api_key_returns_200(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-node"},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200


# ── Ingestion tests ───────────────────────────────────────────────────────────


class TestRadarDetectionsIngestion:
    def test_new_node_registered_in_connected_nodes(self, client):
        from core import state

        node_id = "test-new-node"
        assert node_id not in state.connected_nodes

        r = client.post(
            "/api/radar/detections",
            json={"node_id": node_id},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert node_id in state.connected_nodes

    def test_frame_without_timestamp_not_queued(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-no-ts", "frames": [{"value": 42}]},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert r.json()["frames_queued"] == 0

    def test_frame_with_timestamp_is_queued(self, client):
        r = client.post(
            "/api/radar/detections",
            json={
                "node_id": "test-ts-node",
                "frames": [{"timestamp": 1234567890.0, "value": 1}],
            },
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert r.json()["frames_queued"] == 1

    def test_existing_node_status_updated_not_duplicated(self, client):
        from core import state

        node_id = "test-existing"
        # First registration
        client.post(
            "/api/radar/detections",
            json={"node_id": node_id},
            headers=HEADERS_OK,
        )
        assert node_id in state.connected_nodes

        # Second request — still only one entry, status stays "active"
        r = client.post(
            "/api/radar/detections",
            json={"node_id": node_id},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert state.connected_nodes[node_id]["status"] == "active"
        # Confirm there is still exactly one entry with this id
        assert list(state.connected_nodes).count(node_id) == 1


# ── Bulk endpoint tests ───────────────────────────────────────────────────────


class TestRadarDetectionsBulk:
    def test_bulk_auth_wrong_key_returns_401(self, client):
        r = client.post(
            "/api/radar/detections/bulk",
            json={"nodes": []},
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_bulk_two_nodes_registered_and_frames_queued(self, client):
        payload = {
            "nodes": [
                {
                    "node_id": "bulk-node-a",
                    "frames": [{"timestamp": 1.0, "data": "x"}],
                },
                {
                    "node_id": "bulk-node-b",
                    "frames": [{"timestamp": 2.0, "data": "y"}],
                },
            ]
        }
        r = client.post(
            "/api/radar/detections/bulk",
            json=payload,
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["nodes_registered"] == 2
        assert body["frames_queued"] == 2

    def test_bulk_frame_without_timestamp_skipped(self, client):
        payload = {
            "nodes": [
                {
                    "node_id": "bulk-skip-node",
                    "frames": [{"no_timestamp": True}],
                },
            ]
        }
        r = client.post(
            "/api/radar/detections/bulk",
            json=payload,
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["frames_queued"] == 0
        assert body["nodes_registered"] == 1


# ── _check_rate_limit unit tests ─────────────────────────────────────────────


class TestCheckRateLimit:
    def test_first_call_succeeds(self):
        import routes.radar as radar_mod
        from core import state

        state.rate_buckets.clear()
        # Should not raise
        radar_mod._check_rate_limit("192.0.2.1")

    def test_exceeding_rate_limit_raises_429(self, monkeypatch):
        import routes.radar as radar_mod
        from core import state

        state.rate_buckets.clear()
        monkeypatch.setattr(radar_mod, "_RATE_LIMIT", 2)

        ip = "192.0.2.2"
        radar_mod._check_rate_limit(ip)  # call 1
        radar_mod._check_rate_limit(ip)  # call 2 — hits limit on next
        with pytest.raises(HTTPException) as exc_info:
            radar_mod._check_rate_limit(ip)  # call 3 → 429
        assert exc_info.value.status_code == 429

    def test_expired_timestamps_cleaned_up(self, monkeypatch):
        import routes.radar as radar_mod
        from core import state

        state.rate_buckets.clear()
        ip = "192.0.2.3"

        # Inject an old timestamp well outside the rate window
        old_ts = time.monotonic() - 9999
        state.rate_buckets[ip].append(old_ts)

        # _check_rate_limit should evict the expired entry; after the call
        # the bucket should contain exactly one fresh timestamp (just added).
        radar_mod._check_rate_limit(ip)
        bucket = state.rate_buckets.get(ip, [])
        # Only the fresh timestamp added at the end of _check_rate_limit remains
        assert len(bucket) == 1
        assert bucket[0] > old_ts
