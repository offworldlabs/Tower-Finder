"""Tests for analytics API routes — analytics, overlaps, accuracy, anomalies, adsb-report."""

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


# ── Analytics ─────────────────────────────────────────────────────────────────

class TestAnalytics:
    def test_analytics_returns_bytes(self, client):
        r = client.get("/api/radar/analytics")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"

    def test_analytics_real_only(self, client):
        import orjson

        test_data = orjson.dumps({"nodes": {"real-1": {"metrics": {}}}})
        orig = state.latest_analytics_real_bytes
        state.latest_analytics_real_bytes = test_data
        try:
            r = client.get("/api/radar/analytics?real_only=true")
            assert r.status_code == 200
            assert r.json()["nodes"]["real-1"] == {"metrics": {}}
        finally:
            state.latest_analytics_real_bytes = orig

    def test_node_analytics_not_found(self, client):
        r = client.get("/api/radar/analytics/nonexistent-node-xyz")
        assert r.status_code == 404

    def test_node_analytics_found(self, client):
        """Register a node and verify it returns analytics."""
        state.node_analytics.register_node("test-an-1", {"name": "Test"})
        try:
            r = client.get("/api/radar/analytics/test-an-1")
            assert r.status_code == 200
            body = r.json()
            assert body["node_id"] == "test-an-1"
        finally:
            state.node_analytics.metrics.pop("test-an-1", None)
            state.node_analytics.trust_scores.pop("test-an-1", None)


# ── ADS-B Report ─────────────────────────────────────────────────────────────

class TestAdsbReport:
    _HEADERS = {"X-API-Key": "test-key-abc123"}

    def test_missing_fields(self, client):
        r = client.post("/api/radar/analytics/adsb-report", json={"node_id": "x"}, headers=self._HEADERS)
        assert r.status_code == 400
        assert "Missing" in r.json()["detail"]

    def test_valid_report(self, client):
        r = client.post(
            "/api/radar/analytics/adsb-report",
            json={
                "node_id": "test-adsb-1",
                "predicted_delay": 100.0,
                "measured_delay": 102.0,
                "adsb_hex": "ABC123",
            },
            headers=self._HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "recorded"
        assert "trust_score" in body


# ── Overlaps ─────────────────────────────────────────────────────────────────

class TestOverlaps:
    def test_overlaps_returns_bytes(self, client):
        r = client.get("/api/radar/association/overlaps")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"


# ── Accuracy ─────────────────────────────────────────────────────────────────

class TestAccuracy:
    def test_accuracy_returns_bytes(self, client):
        r = client.get("/api/radar/accuracy")
        assert r.status_code == 200

    def test_accuracy_with_data(self, client):
        import orjson

        data = orjson.dumps({"mean_error_km": 2.5, "p95_error_km": 5.0})
        orig = state.latest_accuracy_bytes
        state.latest_accuracy_bytes = data
        try:
            r = client.get("/api/radar/accuracy")
            assert r.status_code == 200
            assert r.json()["mean_error_km"] == 2.5
        finally:
            state.latest_accuracy_bytes = orig


# ── Association status ────────────────────────────────────────────────────────

class TestAssociationStatus:
    def test_status_returns_expected_fields(self, client):
        r = client.get("/api/radar/association/status")
        assert r.status_code == 200
        body = r.json()
        assert "registered_nodes" in body
        assert "overlap_zones" in body
        assert "overlaps" in body


# ── Anomalies ────────────────────────────────────────────────────────────────

class TestAnomalies:
    def test_anomalies_empty(self, client):
        r = client.get("/api/radar/anomalies")
        assert r.status_code == 200
        body = r.json()
        assert "summary" in body
        assert "by_type" in body
        assert "timeline" in body
        assert "geographic_clusters" in body

    def test_anomalies_with_data(self, client):
        import time

        with state.anomaly_lock:
            state.anomaly_log.append({
                "ts": time.time(),
                "hex": "ANOM01",
                "reason": "speed_violation",
                "lat": 33.45,
                "lon": -112.07,
            })
            state.anomaly_hexes.add("ANOM01")
        try:
            r = client.get("/api/radar/anomalies")
            assert r.status_code == 200
            body = r.json()
            assert body["summary"]["active_count"] >= 1
            assert body["summary"]["unique_hexes"] >= 1
        finally:
            with state.anomaly_lock:
                state.anomaly_hexes.discard("ANOM01")
                state.anomaly_log.clear()
