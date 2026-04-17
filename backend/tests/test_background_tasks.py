"""Tests for background tasks — aircraft flush, periodic tasks."""

import os
import time

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402

# ── _build_real_only_payload ─────────────────────────────────────────────────

class TestBuildRealOnlyPayload:
    def test_filters_synthetic_nodes(self):
        from services.tasks.aircraft_flush import _build_real_only_payload

        state.connected_nodes["real-1"] = {"is_synthetic": False, "status": "active"}
        state.connected_nodes["synth-1"] = {"is_synthetic": True, "status": "active"}
        try:
            data = {
                "now": time.time(),
                "aircraft": [
                    {"hex": "R1", "node_id": "real-1", "multinode": False},
                    {"hex": "S1", "node_id": "synth-1", "multinode": False},
                ],
                "detection_arcs": [
                    {"node_id": "real-1", "arc": []},
                    {"node_id": "synth-1", "arc": []},
                ],
            }
            import orjson

            result = orjson.loads(_build_real_only_payload(data))
            assert len(result["aircraft"]) == 1
            assert result["aircraft"][0]["hex"] == "R1"
            assert len(result["detection_arcs"]) == 1
            assert result["detection_arcs"][0]["node_id"] == "real-1"
        finally:
            state.connected_nodes.pop("real-1", None)
            state.connected_nodes.pop("synth-1", None)

    def test_includes_multinode_with_real_contributor(self):
        from services.tasks.aircraft_flush import _build_real_only_payload

        state.connected_nodes["real-1"] = {"is_synthetic": False, "status": "active"}
        state.connected_nodes["synth-1"] = {"is_synthetic": True, "status": "active"}
        try:
            data = {
                "now": time.time(),
                "aircraft": [
                    {
                        "hex": "M1",
                        "node_id": "synth-1",
                        "multinode": True,
                        "contributing_node_ids": ["synth-1", "real-1"],
                    },
                ],
                "detection_arcs": [],
            }
            import orjson

            result = orjson.loads(_build_real_only_payload(data))
            assert len(result["aircraft"]) == 1
            assert result["aircraft"][0]["hex"] == "M1"
        finally:
            state.connected_nodes.pop("real-1", None)
            state.connected_nodes.pop("synth-1", None)

    def test_empty_data(self):
        import orjson

        from services.tasks.aircraft_flush import _build_real_only_payload

        result = orjson.loads(_build_real_only_payload({"now": 0}))
        assert result["aircraft"] == []
        assert result["detection_arcs"] == []
        assert result["messages"] == 0


# ── broadcast_aircraft ───────────────────────────────────────────────────────

class TestBroadcastAircraft:
    @pytest.mark.asyncio
    async def test_updates_state(self):
        from services.tasks.aircraft_flush import broadcast_aircraft

        data = {
            "now": time.time(),
            "aircraft": [{"hex": "BC01", "node_id": "n1"}],
            "detection_arcs": [],
            "ground_truth": {},
        }
        import orjson

        data_bytes = orjson.dumps(data)
        await broadcast_aircraft(data, data_bytes)
        assert state.latest_aircraft_json == data
        assert state.latest_aircraft_json_bytes == data_bytes
        assert state.latest_real_aircraft_json_bytes != b""


# ── Stale task detection helpers ─────────────────────────────────────────────

class TestTaskTimestamps:
    def test_task_last_success_updated(self):
        """Verify that task_last_success is a dict that can track timestamps."""
        state.task_last_success["test_task"] = time.time()
        assert "test_task" in state.task_last_success
        state.task_last_success.pop("test_task", None)

    def test_task_error_counts(self):
        """Verify task_error_counts tracks errors."""
        orig = state.task_error_counts.get("test_task", 0)
        state.task_error_counts["test_task"] = orig + 1
        assert state.task_error_counts["test_task"] == orig + 1
        state.task_error_counts.pop("test_task", None)
