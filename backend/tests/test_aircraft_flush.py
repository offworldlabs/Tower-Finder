"""Tests for aircraft flush — _build_real_only_payload and broadcast_aircraft."""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

import asyncio

import orjson
import pytest

from core import state  # noqa: E402
from services.tasks.aircraft_flush import (  # noqa: E402
    _build_real_only_payload,
    broadcast_aircraft,
)


@pytest.fixture(autouse=True)
def _cleanup():
    old_nodes = dict(state.connected_nodes)
    old_json = state.latest_aircraft_json
    old_bytes = state.latest_aircraft_json_bytes
    yield
    state.connected_nodes.clear()
    state.connected_nodes.update(old_nodes)
    state.latest_aircraft_json = old_json
    state.latest_aircraft_json_bytes = old_bytes


class TestBuildRealOnlyPayload:
    def test_filters_synthetic_nodes(self):
        state.connected_nodes["real-1"] = {"is_synthetic": False}
        state.connected_nodes["synth-1"] = {"is_synthetic": True}

        data = {
            "now": 1000,
            "aircraft": [
                {"node_id": "real-1", "hex": "A"},
                {"node_id": "synth-1", "hex": "B"},
            ],
            "detection_arcs": [
                {"node_id": "real-1"},
                {"node_id": "synth-1"},
            ],
        }
        result = orjson.loads(_build_real_only_payload(data))

        assert len(result["aircraft"]) == 1
        assert result["aircraft"][0]["hex"] == "A"
        assert len(result["detection_arcs"]) == 1

    def test_multinode_includes_if_any_real(self):
        state.connected_nodes["real-1"] = {"is_synthetic": False}
        state.connected_nodes["synth-1"] = {"is_synthetic": True}

        data = {
            "now": 1000,
            "aircraft": [
                {
                    "node_id": "synth-1",
                    "hex": "MN",
                    "multinode": True,
                    "contributing_node_ids": ["synth-1", "real-1"],
                },
            ],
            "detection_arcs": [],
        }
        result = orjson.loads(_build_real_only_payload(data))
        assert len(result["aircraft"]) == 1
        assert result["aircraft"][0]["hex"] == "MN"

    def test_empty_aircraft(self):
        data = {"now": 0, "aircraft": [], "detection_arcs": []}
        result = orjson.loads(_build_real_only_payload(data))
        assert result["aircraft"] == []
        assert result["messages"] == 0


class TestBroadcastAircraft:
    def test_updates_state_bytes(self):
        data = {"now": 123, "aircraft": [], "detection_arcs": [], "ground_truth": {}}
        data_bytes = orjson.dumps(data)

        asyncio.get_event_loop().run_until_complete(
            broadcast_aircraft(data, data_bytes)
        )

        assert state.latest_aircraft_json is data
        assert state.latest_aircraft_json_bytes is data_bytes
