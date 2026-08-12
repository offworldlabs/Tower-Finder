"""Additional tests for services/tasks/aircraft_flush.py.

Focuses on the WebSocket broadcast path and the real-only filtering logic.
"""

import os

import orjson

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services.tasks.aircraft_flush import (  # noqa: E402
    _build_real_only_payload,
    broadcast_aircraft,
    filter_payload_to_nodes,
)


class TestBuildRealOnlyPayload:
    def test_filters_to_real_nodes(self, monkeypatch):
        # Two nodes: one real, one synthetic.
        monkeypatch.setattr(
            state,
            "connected_nodes",
            {
                "real1": {"is_synthetic": False},
                "synth1": {"is_synthetic": True},
            },
        )
        data = {
            "now": 1.0,
            "aircraft": [
                {"hex": "A1", "node_id": "real1"},
                {"hex": "A2", "node_id": "synth1"},
            ],
            "detection_arcs": [
                {"id": "arc1", "node_id": "real1"},
                {"id": "arc2", "node_id": "synth1"},
            ],
        }
        out = orjson.loads(_build_real_only_payload(data))
        assert [a["hex"] for a in out["aircraft"]] == ["A1"]
        assert [a["id"] for a in out["detection_arcs"]] == ["arc1"]
        assert out["messages"] == 1
        assert out["ground_truth"] == {}

    def test_multinode_aircraft_kept_when_any_contributor_real(self, monkeypatch):
        monkeypatch.setattr(
            state,
            "connected_nodes",
            {
                "real1": {"is_synthetic": False},
                "synth1": {"is_synthetic": True},
            },
        )
        data = {
            "now": 1.0,
            "aircraft": [
                {
                    "hex": "MN1",
                    "node_id": "synth1",  # primary synthetic
                    "multinode": True,
                    "contributing_node_ids": ["synth1", "real1"],
                },
                {
                    "hex": "MN2",
                    "node_id": "synth1",
                    "multinode": True,
                    "contributing_node_ids": ["synth1"],
                },
            ],
            "detection_arcs": [],
        }
        out = orjson.loads(_build_real_only_payload(data))
        hexes = [a["hex"] for a in out["aircraft"]]
        assert "MN1" in hexes
        assert "MN2" not in hexes


class TestFilterPayloadToNodes:
    def test_filters_to_given_nodes(self):
        data = {
            "now": 2.0,
            "aircraft": [
                {"hex": "A1", "node_id": "mine"},
                {"hex": "A2", "node_id": "theirs"},
            ],
            "detection_arcs": [
                {"id": "arc1", "node_id": "mine"},
                {"id": "arc2", "node_id": "theirs"},
            ],
        }
        out = orjson.loads(filter_payload_to_nodes(data, {"mine"}))
        assert [a["hex"] for a in out["aircraft"]] == ["A1"]
        assert [a["id"] for a in out["detection_arcs"]] == ["arc1"]
        assert out["messages"] == 1

    def test_empty_node_set_yields_nothing(self):
        data = {"now": 1.0, "aircraft": [{"hex": "A1", "node_id": "mine"}], "detection_arcs": []}
        out = orjson.loads(filter_payload_to_nodes(data, set()))
        assert out["aircraft"] == []
        assert out["messages"] == 0

    def test_multinode_kept_when_contributor_owned(self):
        data = {
            "now": 1.0,
            "aircraft": [
                {
                    "hex": "MN1",
                    "node_id": "theirs",
                    "multinode": True,
                    "contributing_node_ids": ["theirs", "mine"],
                }
            ],
            "detection_arcs": [],
        }
        out = orjson.loads(filter_payload_to_nodes(data, {"mine"}))
        assert [a["hex"] for a in out["aircraft"]] == ["MN1"]


class _FakeWS:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(text)

    async def close(self):
        self.closed = True


class TestBroadcastAircraft:
    async def test_updates_state_and_sends_to_clients(self, monkeypatch):
        monkeypatch.setattr(state, "connected_nodes", {"n1": {"is_synthetic": False}})
        state.ws_clients.clear()
        state.ws_live_clients.clear()

        real_ws = _FakeWS()
        live_ws = _FakeWS()
        state.ws_clients.add(real_ws)
        state.ws_live_clients.add(live_ws)

        data = {
            "now": 1.0,
            "aircraft": [{"hex": "X1", "node_id": "n1"}],
            "detection_arcs": [],
            "ground_truth": {},
        }
        payload_bytes = orjson.dumps(data)

        try:
            await broadcast_aircraft(data, payload_bytes)
        finally:
            state.ws_clients.discard(real_ws)
            state.ws_live_clients.discard(live_ws)

        assert state.latest_aircraft_json == data
        assert state.latest_aircraft_json_bytes == payload_bytes
        assert state.latest_real_aircraft_json_bytes != b""
        assert len(real_ws.sent) == 1
        assert len(live_ws.sent) == 1

    async def test_failing_client_is_removed(self, monkeypatch):
        monkeypatch.setattr(state, "connected_nodes", {"n1": {"is_synthetic": False}})
        state.ws_clients.clear()
        state.ws_live_clients.clear()

        broken = _FakeWS(fail=True)
        state.ws_live_clients.add(broken)
        try:
            await broadcast_aircraft(
                {"now": 1.0, "aircraft": [], "detection_arcs": [], "ground_truth": {}},
                b"{}",
            )
        finally:
            # Broken socket should have been removed from the set
            assert broken not in state.ws_live_clients
            assert broken.closed

    async def test_owner_client_gets_only_their_nodes(self, monkeypatch):
        monkeypatch.setattr(state, "connected_nodes", {})
        state.ws_clients.clear()
        state.ws_live_clients.clear()
        state.ws_owner_clients.clear()

        owner_ws = _FakeWS()
        state.ws_owner_clients[owner_ws] = {"mine"}
        data = {
            "now": 1.0,
            "aircraft": [
                {"hex": "MINE", "node_id": "mine"},
                {"hex": "THEIRS", "node_id": "theirs"},
            ],
            "detection_arcs": [],
            "ground_truth": {},
        }
        try:
            await broadcast_aircraft(data, orjson.dumps(data))
        finally:
            state.ws_owner_clients.pop(owner_ws, None)

        assert len(owner_ws.sent) == 1
        payload = orjson.loads(owner_ws.sent[0])
        assert [a["hex"] for a in payload["aircraft"]] == ["MINE"]

    async def test_failing_owner_client_is_removed(self, monkeypatch):
        monkeypatch.setattr(state, "connected_nodes", {})
        state.ws_clients.clear()
        state.ws_live_clients.clear()
        state.ws_owner_clients.clear()

        broken = _FakeWS(fail=True)
        state.ws_owner_clients[broken] = {"mine"}
        try:
            await broadcast_aircraft(
                {"now": 1.0, "aircraft": [], "detection_arcs": [], "ground_truth": {}},
                b"{}",
            )
        finally:
            assert broken not in state.ws_owner_clients
            assert broken.closed

    async def test_ground_truth_slimmed_to_last_position(self, monkeypatch):
        monkeypatch.setattr(state, "connected_nodes", {})
        state.ws_clients.clear()
        state.ws_live_clients.clear()

        sink = _FakeWS()
        state.ws_clients.add(sink)
        data = {
            "now": 1.0,
            "aircraft": [],
            "detection_arcs": [],
            "ground_truth": {
                "abc": [[0, 0], [1, 1], [2, 2]],  # 3 positions
            },
        }
        try:
            await broadcast_aircraft(data, orjson.dumps(data))
        finally:
            state.ws_clients.discard(sink)

        assert len(sink.sent) == 1
        payload = orjson.loads(sink.sent[0])
        # Ground truth slimmed to last position only
        assert payload["ground_truth"] == {"abc": [[2, 2]]}
