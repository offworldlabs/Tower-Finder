"""Tests for chain-of-custody API routes."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from main import app  # noqa: E402

_HEADERS = {"X-API-Key": "test-key-abc123"}


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup_state():
    yield
    state.node_identities.pop("test-cust-1", None)
    state.chain_entries.pop("test-cust-1", None)
    state.iq_commitments.pop("test-cust-1", None)


# ── Register ─────────────────────────────────────────────────────────────────


class TestCustodyRegister:
    def test_register_success(self, client):
        r = client.post("/api/custody/register", json={
            "node_id": "test-cust-1",
            "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
        }, headers=_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["node_id"] == "test-cust-1"
        assert body["signing_mode"] == "software"

    def test_register_missing_node_id(self, client):
        r = client.post("/api/custody/register", json={
            "public_key_pem": "key",
        }, headers=_HEADERS)
        assert r.status_code == 422

    def test_register_empty_node_id(self, client):
        r = client.post("/api/custody/register", json={
            "node_id": "",
            "public_key_pem": "key",
        }, headers=_HEADERS)
        assert r.status_code == 422

    def test_register_no_api_key(self, client):
        r = client.post("/api/custody/register", json={
            "node_id": "test-cust-1",
            "public_key_pem": "key",
        })
        assert r.status_code == 401

    def test_register_wrong_api_key(self, client):
        r = client.post("/api/custody/register", json={
            "node_id": "test-cust-1",
            "public_key_pem": "key",
        }, headers={"X-API-Key": "wrong"})
        assert r.status_code == 401


# ── Chain Entry ──────────────────────────────────────────────────────────────


class TestCustodyChainEntry:
    def test_submit_entry(self, client):
        r = client.post("/api/custody/chain-entry", json={
            "node_id": "test-cust-1",
            "entry_hash": "abc",
            "prev_hash": "",
            "hour_utc": "2025-01-01T00:00",
        }, headers=_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "stored"
        assert body["node_id"] == "test-cust-1"

    def test_submit_entry_no_auth(self, client):
        r = client.post("/api/custody/chain-entry", json={
            "node_id": "test-cust-1",
        })
        assert r.status_code == 401

    def test_rolling_cap(self, client):
        from config.constants import CHAIN_ENTRIES_MAX_PER_NODE

        # Pre-fill entries just below cap
        state.chain_entries["test-cust-1"] = [{"i": i} for i in range(CHAIN_ENTRIES_MAX_PER_NODE)]
        # Add one more via API
        r = client.post("/api/custody/chain-entry", json={
            "node_id": "test-cust-1",
            "entry_hash": "overflow",
        }, headers=_HEADERS)
        assert r.status_code == 200
        assert len(state.chain_entries["test-cust-1"]) == CHAIN_ENTRIES_MAX_PER_NODE


# ── IQ Commitment ────────────────────────────────────────────────────────────


class TestCustodyIqCommitment:
    def test_submit_commitment(self, client):
        r = client.post("/api/custody/iq-commitment", json={
            "node_id": "test-cust-1",
            "capture_id": "cap-001",
        }, headers=_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "committed"
        assert body["capture_id"] == "cap-001"

    def test_submit_no_auth(self, client):
        r = client.post("/api/custody/iq-commitment", json={
            "node_id": "test-cust-1",
        })
        assert r.status_code == 401

    def test_rolling_cap(self, client):
        from config.constants import IQ_COMMITMENTS_MAX_PER_NODE

        state.iq_commitments["test-cust-1"] = [{"i": i} for i in range(IQ_COMMITMENTS_MAX_PER_NODE)]
        r = client.post("/api/custody/iq-commitment", json={
            "node_id": "test-cust-1",
            "capture_id": "overflow",
        }, headers=_HEADERS)
        assert r.status_code == 200
        assert len(state.iq_commitments["test-cust-1"]) == IQ_COMMITMENTS_MAX_PER_NODE


# ── Status / Read endpoints ──────────────────────────────────────────────────


class TestCustodyRead:
    def test_status_empty(self, client):
        r = client.get("/api/custody/status")
        assert r.status_code == 200
        body = r.json()
        assert "registered_nodes" in body

    def test_chain_not_found(self, client):
        r = client.get("/api/custody/chain/nonexistent")
        assert r.status_code == 404

    def test_verify_not_found(self, client):
        r = client.get("/api/custody/verify/nonexistent")
        assert r.status_code == 404
