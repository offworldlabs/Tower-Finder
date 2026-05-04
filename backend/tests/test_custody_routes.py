"""Tests for chain-of-custody API routes."""

import os
from unittest.mock import patch

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
    state.node_identities.pop("test-cust-1", None)
    state.chain_entries.pop("test-cust-1", None)
    state.iq_commitments.pop("test-cust-1", None)
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


# ── Chain Entry Verification ─────────────────────────────────────────────────


class TestCustodyChainEntryVerification:
    def test_chain_entry_with_registered_key_exception_path(self, client):
        """Test that exceptions from HashChainEntry.from_dict are caught and reason updated."""
        # Register a node
        client.post("/api/custody/register", json={
            "node_id": "test-cust-1",
            "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
        }, headers=_HEADERS)

        # Patch HashChainEntry.from_dict to raise ValueError
        with patch("routes.custody.HashChainEntry.from_dict", side_effect=ValueError("bad entry")):
            r = client.post("/api/custody/chain-entry", json={
                "node_id": "test-cust-1",
                "entry_hash": "abc",
                "prev_hash": "",
                "hour_utc": "2025-01-01T00:00",
            }, headers=_HEADERS)

        assert r.status_code == 200
        body = r.json()
        assert body["verified"] is False
        assert "bad entry" in body["reason"]

    def test_chain_node_with_identity_returns_identity_dict(self, client):
        """Test that GET /api/custody/chain/{node_id} returns identity dict when registered."""
        # Register a node
        client.post("/api/custody/register", json={
            "node_id": "test-cust-1",
            "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
        }, headers=_HEADERS)

        # Pre-populate chain_entries with minimal valid entry
        state.chain_entries["test-cust-1"] = [{
            "node_id": "test-cust-1",
            "entry_hash": "abc",
            "prev_hash": "",
            "hour_utc": "2025-01-01T00:00",
            "detections_hash": "def",
            "n_detections": 0,
            "node_config_hash": "ghi",
            "firmware_version": "1.0",
            "timestamp_utc": "2025-01-01T00:00:00Z",
            "signature": "",
            "signing_mode": "software",
        }]

        # GET chain
        r = client.get("/api/custody/chain/test-cust-1")
        assert r.status_code == 200
        body = r.json()
        assert "identity" in body
        assert body["identity"] is not None


# ── Verify Chain ─────────────────────────────────────────────────────────────


class TestCustodyVerifyChain:
    def test_verify_chain_no_public_key_returns_400(self, client):
        """Test that verify chain returns 400 when no public key is registered."""
        state.node_identities.pop("test-cust-1", None)
        # Pre-populate chain_entries without registering identity
        state.chain_entries["test-cust-1"] = [{
            "node_id": "test-cust-1",
            "entry_hash": "abc",
            "prev_hash": "",
            "hour_utc": "2025-01-01T00:00",
            "detections_hash": "def",
            "n_detections": 0,
            "node_config_hash": "ghi",
            "firmware_version": "1.0",
            "timestamp_utc": "2025-01-01T00:00:00Z",
            "signature": "",
            "signing_mode": "software",
        }]

        r = client.get("/api/custody/verify/test-cust-1")
        assert r.status_code == 400
        body = r.json()
        assert "No public key registered" in body["detail"]

    def test_verify_chain_exception_returns_500(self, client):
        """Test that verify chain returns 500 when HashChainVerifier raises an exception."""
        # Register a node
        client.post("/api/custody/register", json={
            "node_id": "test-cust-1",
            "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
        }, headers=_HEADERS)

        # Pre-populate chain_entries
        state.chain_entries["test-cust-1"] = [{
            "node_id": "test-cust-1",
            "entry_hash": "abc",
            "prev_hash": "",
            "hour_utc": "2025-01-01T00:00",
            "detections_hash": "def",
            "n_detections": 0,
            "node_config_hash": "ghi",
            "firmware_version": "1.0",
            "timestamp_utc": "2025-01-01T00:00:00Z",
            "signature": "",
            "signing_mode": "software",
        }]

        # Patch HashChainVerifier to raise Exception
        with patch("routes.custody.HashChainVerifier", side_effect=Exception("corrupt chain")):
            r = client.get("/api/custody/verify/test-cust-1")

        assert r.status_code == 500
        body = r.json()
        assert "Chain verification failed" in body["detail"]
