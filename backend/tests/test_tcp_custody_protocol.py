"""Tests for the chain-of-custody TCP message handlers.

Covers: REGISTER_KEY, CHAIN_ENTRY, IQ_COMMITMENT — the three sub-handlers
in services/tcp_handler.py that are dispatched from handle_tcp_client.
"""

import asyncio

import pytest

from config.constants import CHAIN_ENTRIES_MAX_PER_NODE, IQ_COMMITMENTS_MAX_PER_NODE
from core import state
from services.tcp_handler import (
    _handle_chain_entry,
    _handle_iq_commitment,
    _handle_register_key,
)
from tests.tcp_helpers import FakeWriter as MockWriter

# ── Constants ─────────────────────────────────────────────────────────────────

_NODE_ID = "cust-test-node"

_REGISTER_KEY_MSG = {
    "type": "REGISTER_KEY",
    "node_id": _NODE_ID,
    "public_key_pem": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
    "fingerprint": "abc123",
    "serial_number": "SN001",
    "signing_mode": "software",
}

_CHAIN_ENTRY_MSG = {
    "type": "CHAIN_ENTRY",
    "entry": {
        "node_id": _NODE_ID,
        "hour_utc": "2026-01-01T00:00:00Z",
        "entry_hash": "aabb1122",
        "prev_hash": "0000",
        "content_hash": "ccdd",
        "data": "test",
    },
}

_IQ_COMMITMENT_MSG = {
    "type": "IQ_COMMITMENT",
    "capture": {
        "node_id": _NODE_ID,
        "capture_id": "cap001",
        "iq_hash": "aabbccdd1234",
        "sha256": "deadbeef",
    },
}


# ── Shared state-cleanup fixture ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _cleanup_custody_state():
    """Clear chain-of-custody state for the test node before and after every test."""
    state.node_identities.pop(_NODE_ID, None)
    state.chain_entries.pop(_NODE_ID, None)
    state.iq_commitments.pop(_NODE_ID, None)
    yield
    state.node_identities.pop(_NODE_ID, None)
    state.chain_entries.pop(_NODE_ID, None)
    state.iq_commitments.pop(_NODE_ID, None)


# ── TestRegisterKey ───────────────────────────────────────────────────────────

class TestRegisterKey:
    def test_register_key_stores_identity(self):
        """REGISTER_KEY stores a NodeIdentity with correct pem and fingerprint."""
        writer = MockWriter()
        asyncio.run(_handle_register_key(_REGISTER_KEY_MSG, None, writer))

        assert _NODE_ID in state.node_identities
        identity = state.node_identities[_NODE_ID]
        assert identity.public_key_pem == _REGISTER_KEY_MSG["public_key_pem"]
        assert identity.public_key_fingerprint == "abc123"

    def test_register_key_sends_key_ack(self):
        """REGISTER_KEY sends KEY_ACK with status='registered', node_id, fingerprint."""
        writer = MockWriter()
        asyncio.run(_handle_register_key(_REGISTER_KEY_MSG, None, writer))

        msgs = writer.messages()
        acks = [m for m in msgs if m.get("type") == "KEY_ACK"]
        assert len(acks) == 1
        ack = acks[0]
        assert ack["status"] == "registered"
        assert ack["node_id"] == _NODE_ID
        assert ack["fingerprint"] == "abc123"

    def test_register_key_with_custom_signing_mode(self):
        """signing_mode='hardware' is stored in the NodeIdentity."""
        msg = {**_REGISTER_KEY_MSG, "signing_mode": "hardware"}
        writer = MockWriter()
        asyncio.run(_handle_register_key(msg, None, writer))

        identity = state.node_identities[_NODE_ID]
        assert identity.signing_mode == "hardware"


# ── TestChainEntry ────────────────────────────────────────────────────────────

class TestChainEntry:
    def test_chain_entry_appended_to_state(self):
        """CHAIN_ENTRY is stored in state.chain_entries with _received_at set."""
        writer = MockWriter()
        asyncio.run(_handle_chain_entry(_CHAIN_ENTRY_MSG, _NODE_ID, writer))

        assert _NODE_ID in state.chain_entries
        entries = state.chain_entries[_NODE_ID]
        assert len(entries) == 1
        assert "_received_at" in entries[0]

    def test_chain_entry_sends_ack(self):
        """CHAIN_ENTRY sends CHAIN_ENTRY_ACK with entry_hash and verified fields."""
        writer = MockWriter()
        asyncio.run(_handle_chain_entry(_CHAIN_ENTRY_MSG, _NODE_ID, writer))

        msgs = writer.messages()
        acks = [m for m in msgs if m.get("type") == "CHAIN_ENTRY_ACK"]
        assert len(acks) == 1
        ack = acks[0]
        assert "entry_hash" in ack
        assert "verified" in ack
        assert ack["node_id"] == _NODE_ID

    def test_chain_entry_unregistered_node_not_verified(self):
        """Without a prior REGISTER_KEY, _verified=False and ACK says verified=false."""
        # Ensure no identity is registered
        state.node_identities.pop(_NODE_ID, None)

        writer = MockWriter()
        asyncio.run(_handle_chain_entry(_CHAIN_ENTRY_MSG, _NODE_ID, writer))

        # Stored entry has _verified=False
        entry = state.chain_entries[_NODE_ID][0]
        assert entry["_verified"] is False

        # ACK also carries verified=false
        msgs = writer.messages()
        ack = next(m for m in msgs if m.get("type") == "CHAIN_ENTRY_ACK")
        assert ack["verified"] is False

    def test_chain_entry_cap_enforced(self):
        """Sending more than CHAIN_ENTRIES_MAX_PER_NODE entries trims the list."""
        writer = MockWriter()

        # Pre-fill state to the cap
        state.chain_entries[_NODE_ID] = [
            {"_synthetic": True, "i": i} for i in range(CHAIN_ENTRIES_MAX_PER_NODE)
        ]

        # Add one more — should trigger a trim
        asyncio.run(_handle_chain_entry(_CHAIN_ENTRY_MSG, _NODE_ID, writer))

        entries = state.chain_entries[_NODE_ID]
        assert len(entries) == CHAIN_ENTRIES_MAX_PER_NODE
        # The newly appended entry should be the last one
        assert "_received_at" in entries[-1]
        # The first synthetic entry should have been trimmed off
        assert entries[0].get("_synthetic") is True
        assert entries[0].get("i") == 1


# ── TestIQCommitment ──────────────────────────────────────────────────────────

class TestIQCommitment:
    def test_iq_commitment_appended_to_state(self):
        """IQ_COMMITMENT is stored in state.iq_commitments with _received_at and capture_id."""
        writer = MockWriter()
        asyncio.run(_handle_iq_commitment(_IQ_COMMITMENT_MSG, _NODE_ID, writer))

        assert _NODE_ID in state.iq_commitments
        commitments = state.iq_commitments[_NODE_ID]
        assert len(commitments) == 1
        assert "_received_at" in commitments[0]
        assert commitments[0]["capture_id"] == "cap001"

    def test_iq_commitment_sends_ack(self):
        """IQ_COMMITMENT sends IQ_COMMITMENT_ACK with capture_id and status='committed'."""
        writer = MockWriter()
        asyncio.run(_handle_iq_commitment(_IQ_COMMITMENT_MSG, _NODE_ID, writer))

        msgs = writer.messages()
        acks = [m for m in msgs if m.get("type") == "IQ_COMMITMENT_ACK"]
        assert len(acks) == 1
        ack = acks[0]
        assert ack["capture_id"] == "cap001"
        assert ack["status"] == "committed"
        assert ack["node_id"] == _NODE_ID

    def test_iq_commitment_cap_enforced(self):
        """Sending more than IQ_COMMITMENTS_MAX_PER_NODE entries trims the list."""
        writer = MockWriter()

        # Pre-fill state to the cap
        state.iq_commitments[_NODE_ID] = [
            {"_synthetic": True, "i": i} for i in range(IQ_COMMITMENTS_MAX_PER_NODE)
        ]

        # Add one more — should trigger a trim
        asyncio.run(_handle_iq_commitment(_IQ_COMMITMENT_MSG, _NODE_ID, writer))

        commitments = state.iq_commitments[_NODE_ID]
        assert len(commitments) == IQ_COMMITMENTS_MAX_PER_NODE
        # The newly appended commitment should be the last one
        assert "_received_at" in commitments[-1]
        # The first synthetic entry should have been trimmed off
        assert commitments[0].get("_synthetic") is True
        assert commitments[0].get("i") == 1
