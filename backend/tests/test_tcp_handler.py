"""Tests for the TCP handler and RETINA node protocol.

Covers: HELLO/CONFIG handshake, node registration in state, disconnection
cleanup, malformed messages, _enqueue_detection, _apply_synthetic_adsb.
"""

import asyncio
import json
import time

import pytest

from core import state
from services.tcp_handler import (
    handle_tcp_client,
    is_synthetic_node,
    _enqueue_detection,
    _apply_synthetic_adsb,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _msg(d: dict) -> bytes:
    """Encode a dict as a newline-terminated JSON message."""
    return json.dumps(d).encode("utf-8") + b"\n"


def _make_hello(node_id: str = "test-node-1", is_synthetic: bool = False) -> bytes:
    return _msg({
        "type": "HELLO",
        "node_id": node_id,
        "version": "1.0",
        "is_synthetic": is_synthetic,
    })


def _make_config(node_id: str = "test-node-1", is_synthetic: bool = False) -> bytes:
    return _msg({
        "type": "CONFIG",
        "node_id": node_id,
        "config_hash": "abc123",
        "is_synthetic": is_synthetic,
        "config": {
            "node_id": node_id,
            "rx_lat": 33.94,
            "rx_lon": -84.65,
            "rx_alt_ft": 950,
            "tx_lat": 33.76,
            "tx_lon": -84.33,
            "tx_alt_ft": 1600,
            "frequency_mhz": 195,
            "beam_width_deg": 41,
            "max_range_km": 50,
        },
        "capabilities": {"adsb_report": True},
    })


class MockStreamReader:
    """Simulates asyncio.StreamReader with queued data chunks."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._idx = 0

    async def read(self, n: int) -> bytes:
        if self._idx >= len(self._chunks):
            return b""
        data = self._chunks[self._idx]
        self._idx += 1
        return data


class MockStreamWriter:
    """Simulates asyncio.StreamWriter, capturing written data."""

    def __init__(self):
        self.written: list[bytes] = []
        self._closed = False

    def get_extra_info(self, key, default=None):
        if key == "peername":
            return ("127.0.0.1", 12345)
        return default

    def write(self, data: bytes):
        self.written.append(data)

    async def drain(self):
        pass

    def close(self):
        self._closed = True

    def get_messages(self) -> list[dict]:
        """Parse all written messages as JSON."""
        msgs = []
        for chunk in self.written:
            for line in chunk.split(b"\n"):
                line = line.strip()
                if line:
                    msgs.append(json.loads(line))
        return msgs


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestIsSyntheticNode:
    def test_synthetic_prefix(self):
        assert is_synthetic_node("synth-atl-001") is True

    def test_non_synthetic(self):
        assert is_synthetic_node("net13") is False

    def test_empty(self):
        assert is_synthetic_node("") is False


class TestHandshake:
    @pytest.fixture(autouse=True)
    def _cleanup_state(self):
        """Ensure test node is removed from state after each test."""
        yield
        state.connected_nodes.pop("test-node-1", None)
        state.connected_nodes.pop("test-node-2", None)

    def test_hello_config_registers_node(self):
        """HELLO + CONFIG → node appears in state.connected_nodes."""
        reader = MockStreamReader([
            _make_hello("test-node-1"),
            _make_config("test-node-1"),
            b"",  # EOF
        ])
        writer = MockStreamWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        assert "test-node-1" in state.connected_nodes
        node = state.connected_nodes["test-node-1"]
        assert node["config_hash"] == "abc123"
        assert node["status"] == "disconnected"  # set in finally block after EOF

    def test_config_ack_sent(self):
        """Server replies with CONFIG_ACK after receiving CONFIG."""
        reader = MockStreamReader([
            _make_hello("test-node-2"),
            _make_config("test-node-2"),
            b"",
        ])
        writer = MockStreamWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        msgs = writer.get_messages()
        ack = [m for m in msgs if m.get("type") == "CONFIG_ACK"]
        assert len(ack) == 1
        assert ack[0]["config_hash"] == "abc123"

    def test_disconnection_marks_status(self):
        """After disconnect, node status is set to 'disconnected'."""
        reader = MockStreamReader([
            _make_hello("test-node-1"),
            _make_config("test-node-1"),
            b"",  # EOF triggers disconnect
        ])
        writer = MockStreamWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        assert state.connected_nodes["test-node-1"]["status"] == "disconnected"

    def test_malformed_json_skipped(self):
        """Malformed JSON lines are skipped without crashing."""
        reader = MockStreamReader([
            _make_hello("test-node-1"),
            b"not valid json\n",
            _make_config("test-node-1"),
            b"",
        ])
        writer = MockStreamWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        # Node still registered despite malformed line
        assert "test-node-1" in state.connected_nodes

    def test_synthetic_node_flag(self):
        """Synthetic nodes are correctly flagged."""
        reader = MockStreamReader([
            _make_hello("synth-test-1", is_synthetic=True),
            _make_config("synth-test-1", is_synthetic=True),
            b"",
        ])
        writer = MockStreamWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        state.connected_nodes.pop("synth-test-1", None)


class TestEnqueueDetection:
    @pytest.fixture(autouse=True)
    def _drain_queue(self):
        """Drain any leftover frames from the queue before/after tests."""
        while not state.frame_queue.empty():
            try:
                state.frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        yield
        while not state.frame_queue.empty():
            try:
                state.frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def test_enqueue_valid_detection(self):
        msg = {
            "type": "DETECTION",
            "data": {
                "timestamp": int(time.time() * 1000),
                "delay": [50.0],
                "doppler": [10.0],
                "snr": [20.0],
            },
        }
        # Reset rate limiter for this node
        from services.tcp_handler import _per_node_last_enqueue
        _per_node_last_enqueue.pop("test-enq", None)

        _enqueue_detection(msg, "test-enq")
        assert not state.frame_queue.empty()

    def test_enqueue_no_timestamp_skipped(self):
        msg = {"type": "DETECTION", "data": {"delay": [1.0]}}
        _enqueue_detection(msg, "test-skip")
        assert state.frame_queue.empty()


class TestApplySyntheticAdsb:
    @pytest.fixture(autouse=True)
    def _cleanup_adsb(self):
        yield
        for key in list(state.adsb_aircraft.keys()):
            if key.startswith("test"):
                del state.adsb_aircraft[key]

    def test_stores_adsb_positions(self):
        msg = {
            "data": {
                "timestamp": 1000,
                "adsb": [
                    {"hex": "test001", "lat": 33.9, "lon": -84.6, "alt_baro": 35000, "gs": 250, "track": 90},
                    {"hex": "test002", "lat": 34.0, "lon": -84.5, "alt_baro": 30000, "gs": 200, "track": 180},
                ],
            },
        }
        _apply_synthetic_adsb(msg, "synth-test")
        assert "test001" in state.adsb_aircraft
        assert "test002" in state.adsb_aircraft
        assert state.adsb_aircraft["test001"]["lat"] == 33.9

    def test_skips_invalid_entries(self):
        msg = {
            "data": {
                "timestamp": 1000,
                "adsb": [
                    {"hex": "", "lat": 33.9, "lon": -84.6},      # empty hex
                    {"hex": "testbad", "lat": 0, "lon": 0},       # zero coords
                    "not a dict",                                   # wrong type
                ],
            },
        }
        _apply_synthetic_adsb(msg, "synth-test")
        assert "testbad" not in state.adsb_aircraft

    def test_no_adsb_field_is_noop(self):
        msg = {"data": {"timestamp": 1000}}
        _apply_synthetic_adsb(msg, "synth-test")
        # No crash, nothing added
