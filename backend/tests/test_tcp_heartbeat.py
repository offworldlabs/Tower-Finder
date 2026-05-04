"""Tests for the TCP HEARTBEAT handler in services/tcp_handler.py.

Covers _handle_heartbeat and the HEARTBEAT dispatch path in handle_tcp_client:
- last_heartbeat / status updates
- config-drift detection (CONFIG_REQUEST sent vs. not sent)
- unknown-node silent ignore
- analytics recording
"""

import asyncio
import json
import time

import pytest

from core import state
from services.tcp_handler import _handle_heartbeat, handle_tcp_client
from tests.tcp_helpers import FakeReader, FakeWriter

# ── Constants ─────────────────────────────────────────────────────────────────

NODE_ID = "hb-test-node"
CONFIG_HASH = "deadbeef"
ALT_CONFIG_HASH = "cafef00d"
HEARTBEAT_TS = "2026-05-04T12:00:00+00:00"

_NODE_CONFIG = {
    "node_id": NODE_ID,
    "rx_lat": 33.94,
    "rx_lon": -84.65,
    "rx_alt_ft": 950,
    "tx_lat": 33.76,
    "tx_lon": -84.33,
    "tx_alt_ft": 1600,
    "frequency_mhz": 195,
    "beam_width_deg": 41,
    "max_range_km": 50,
}

# ── Message helpers ───────────────────────────────────────────────────────────

def _msg(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8") + b"\n"


def _hello(node_id: str = NODE_ID) -> bytes:
    return _msg({"type": "HELLO", "node_id": node_id, "version": "1.0", "is_synthetic": False})


def _config(node_id: str = NODE_ID, config_hash: str = CONFIG_HASH) -> bytes:
    return _msg({
        "type": "CONFIG",
        "node_id": node_id,
        "config_hash": config_hash,
        "is_synthetic": False,
        "config": _NODE_CONFIG,
        "capabilities": {"adsb_report": True},
    })


def _heartbeat(
    node_id: str = NODE_ID,
    config_hash: str = CONFIG_HASH,
    status: str = "active",
    timestamp: str = HEARTBEAT_TS,
) -> bytes:
    return _msg({
        "type": "HEARTBEAT",
        "node_id": node_id,
        "config_hash": config_hash,
        "status": status,
        "timestamp": timestamp,
    })


# ── Test class ────────────────────────────────────────────────────────────────

class TestTCPHeartbeat:

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        """Remove test node from shared state before and after each test."""
        state.connected_nodes.pop(NODE_ID, None)
        if hasattr(state.node_analytics, "metrics"):
            state.node_analytics.metrics.pop(NODE_ID, None)
        yield
        state.connected_nodes.pop(NODE_ID, None)
        if hasattr(state.node_analytics, "metrics"):
            state.node_analytics.metrics.pop(NODE_ID, None)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _run_hello_config_heartbeat(
        self,
        hb_config_hash: str = CONFIG_HASH,
        hb_status: str = "active",
        hb_timestamp: str = HEARTBEAT_TS,
        node_config_hash: str = CONFIG_HASH,
    ):
        reader = FakeReader([
            _hello(),
            _config(config_hash=node_config_hash),
            _heartbeat(config_hash=hb_config_hash, status=hb_status, timestamp=hb_timestamp),
            b"",
        ])
        writer = FakeWriter()
        asyncio.run(handle_tcp_client(reader, writer))
        return writer

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_heartbeat_updates_last_heartbeat(self):
        """After HELLO+CONFIG+HEARTBEAT, last_heartbeat is set to the heartbeat's timestamp."""
        self._run_hello_config_heartbeat()
        node = state.connected_nodes.get(NODE_ID, {})
        assert node.get("last_heartbeat") == HEARTBEAT_TS

    def test_heartbeat_updates_node_status(self):
        """A HEARTBEAT with status='active' updates the node's status field directly."""
        # Pre-populate node in state.connected_nodes with status="disconnected"
        with state.connected_nodes_lock:
            state.connected_nodes[NODE_ID] = {
                "status": "disconnected",
                "config_hash": CONFIG_HASH,
            }

        # Create a heartbeat message
        msg = {
            "type": "HEARTBEAT",
            "node_id": NODE_ID,
            "status": "active",
            "config_hash": CONFIG_HASH,
            "timestamp": HEARTBEAT_TS,
        }

        # Create a fake writer (not needed for heartbeat logic but required by signature)
        writer = FakeWriter()

        # Call _handle_heartbeat directly
        asyncio.run(_handle_heartbeat(msg, NODE_ID, writer))

        # Assert that status was set to "active"
        node = state.connected_nodes.get(NODE_ID, {})
        assert node.get("status") == "active"
        assert node.get("last_heartbeat") == HEARTBEAT_TS

    def test_heartbeat_no_config_drift_sends_no_config_request(self):
        """Matching config_hash → no CONFIG_REQUEST in writer output."""
        writer = self._run_hello_config_heartbeat(
            node_config_hash=CONFIG_HASH,
            hb_config_hash=CONFIG_HASH,
        )
        config_requests = [m for m in writer.messages() if m.get("type") == "CONFIG_REQUEST"]
        assert config_requests == []

    def test_heartbeat_config_drift_sends_config_request(self):
        """Mismatched config_hash → writer receives CONFIG_REQUEST with correct node_id."""
        writer = self._run_hello_config_heartbeat(
            node_config_hash=CONFIG_HASH,
            hb_config_hash=ALT_CONFIG_HASH,
        )
        config_requests = [m for m in writer.messages() if m.get("type") == "CONFIG_REQUEST"]
        assert len(config_requests) == 1
        assert config_requests[0]["node_id"] == NODE_ID

    def test_heartbeat_config_drift_preserves_node_registration(self):
        """Config drift is advisory — node stays in connected_nodes after drift."""
        self._run_hello_config_heartbeat(
            node_config_hash=CONFIG_HASH,
            hb_config_hash=ALT_CONFIG_HASH,
        )
        assert NODE_ID in state.connected_nodes

    def test_heartbeat_for_unknown_node_is_silently_ignored(self):
        """A HEARTBEAT for a node that was never CONFIGed is silently ignored — no crash, no CONFIG_REQUEST."""
        unknown_id = "never-registered-node"
        state.connected_nodes.pop(unknown_id, None)

        reader = FakeReader([
            _heartbeat(node_id=unknown_id),
            b"",
        ])
        writer = FakeWriter()
        # Must not raise
        asyncio.run(handle_tcp_client(reader, writer))

        config_requests = [m for m in writer.messages() if m.get("type") == "CONFIG_REQUEST"]
        assert config_requests == []
        assert unknown_id not in state.connected_nodes

    def test_heartbeat_records_in_analytics(self):
        """After HELLO+CONFIG+HEARTBEAT, the node's analytics metrics reflect the heartbeat."""
        before = time.time()
        self._run_hello_config_heartbeat()
        after = time.time()

        metrics = state.node_analytics.metrics.get(NODE_ID)
        assert metrics is not None, "NodeMetrics entry missing after HELLO+CONFIG+HEARTBEAT"
        # record_heartbeat() sets last_heartbeat = time.time()
        assert before <= metrics.last_heartbeat <= after + 1
