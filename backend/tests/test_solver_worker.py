"""Unit tests for the multinode solver worker helper.

Covers the bookkeeping that happens around a single solver call:
- successful solve updates metrics and stores the track
- exceptions are caught and counted
- high latency triggers an alert (via services.alerting.send_alert)
- None / unsuccessful results do not leak into multinode_tracks
"""

import os
import time

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services.tasks import solver as solver_mod  # noqa: E402


def _reset_state():
    state.task_error_counts.clear()
    state.solver_failures = 0
    state.solver_successes = 0
    state.solver_total_solved = 0
    state.solver_total_latency_s = 0.0
    state.solver_last_latency_s = 0.0
    state.multinode_tracks.clear()
    state.task_last_success.clear()


class _StubAnalytics:
    def __init__(self):
        self.calibration_calls: list = []

    def record_calibration_point(self, node_id, lat, lon):
        self.calibration_calls.append((node_id, lat, lon))


class TestProcessSolverItem:
    def test_success_updates_state(self, monkeypatch):
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "timestamp_ms": 1000,
                "contributing_node_ids": ["n1", "n2"],
            }

        item = ({"n_nodes": 2}, {}, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is not None
        assert state.solver_successes == 1
        assert state.solver_total_solved == 1
        assert state.solver_last_latency_s >= 0
        assert "solver" in state.task_last_success
        assert any(k.startswith("mn-1000-") for k in state.multinode_tracks)
        assert len(stub.calibration_calls) == 2

    def test_exception_increments_failures(self, monkeypatch):
        _reset_state()

        def solve_fn(s_in, cfgs):
            raise ValueError("boom")

        item = ({"n_nodes": 3}, {}, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is None
        assert state.solver_failures == 1
        assert state.task_error_counts["solver"] == 1
        assert state.solver_successes == 0
        assert not state.multinode_tracks

    def test_unsuccessful_result_not_stored(self, monkeypatch):
        _reset_state()

        def solve_fn(s_in, cfgs):
            return {"success": False}

        item = ({"n_nodes": 2}, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert state.solver_successes == 0
        assert not state.multinode_tracks
        assert state.solver_failures == 0  # not counted as failure

    def test_high_latency_triggers_alert(self, monkeypatch):
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        alerts: list = []

        def _record_alert(alert_type, message, meta=None):
            alerts.append((alert_type, meta))

        # services.alerting is imported lazily inside _process_solver_item
        import services.alerting as alerting_mod
        monkeypatch.setattr(alerting_mod, "send_alert", _record_alert)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 0.0,
                "lon": 0.0,
                "timestamp_ms": 2000,
                "contributing_node_ids": [],
            }

        # enqueued 60 seconds in the past → latency > 30 triggers alert
        item = ({"n_nodes": 4}, {}, time.time() - 60.0)
        solver_mod._process_solver_item(item, solve_fn)

        assert any(a[0] == "solver_latency_high" for a in alerts)
        assert state.solver_last_latency_s > 30.0

    def test_missing_enqueued_at_skips_latency(self, monkeypatch):
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 1.0,
                "lon": 2.0,
                "timestamp_ms": 3000,
                "contributing_node_ids": ["n1"],
            }

        # 2-tuple item (legacy shape without enqueued_at)
        item = ({"n_nodes": 2}, {})
        solver_mod._process_solver_item(item, solve_fn)

        assert state.solver_successes == 1
        assert state.solver_total_solved == 1  # counted even without latency info
