"""Unit tests for the multinode solver worker helper.

Covers the bookkeeping that happens around a single solver call:
- successful solve updates metrics and stores the track
- exceptions are caught and counted
- high latency triggers an alert (via services.alerting.send_alert)
- None / unsuccessful results do not leak into multinode_tracks
"""

import os
import time

import pytest

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


class TestRmsDelayFilter:
    def test_high_rms_delay_rejected(self, monkeypatch):
        """Results with rms_delay > _SOLVER_RMS_DELAY_MAX_US must not enter multinode_tracks."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "alt_m": 8000.0,
                "rms_delay": 230.0,  # ~70 km lateral error residual
                "timestamp_ms": 5000,
                "contributing_node_ids": ["n1", "n2"],
                "n_nodes": 2,
            }

        item = ({"n_nodes": 2}, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert not state.multinode_tracks, "false solve must not be stored"
        assert state.solver_successes == 0
        assert state.solver_failures == 1

    def test_low_rms_delay_accepted(self, monkeypatch):
        """Results with rms_delay within threshold are stored normally."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "alt_m": 8000.0,
                "rms_delay": 1.2,  # good solve
                "timestamp_ms": 6000,
                "contributing_node_ids": ["n1", "n2"],
                "n_nodes": 2,
            }

        item = ({"n_nodes": 2}, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert any(k.startswith("mn-6000-") for k in state.multinode_tracks)
        assert state.solver_successes == 1


class TestSolveBestAltitude:
    """The altitude-sweep helper is used only for n_nodes >= 3."""

    def test_n3_picks_minimum_rms_altitude(self, monkeypatch):
        """For n_nodes=3, _process_solver_item tries all altitude layers and picks best."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        calls: list[float] = []

        def solve_fn(s_in, cfgs):
            alt = s_in["initial_guess"]["alt_km"]
            calls.append(alt)
            # Simulate: 9 km layer gives best rms; others give poor rms
            rms = 0.1 if abs(alt - 9.0) < 0.1 else 4.0
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "alt_m": alt * 1000,
                "rms_delay": rms,
                "timestamp_ms": 7000,
                "contributing_node_ids": ["n1", "n2", "n3"],
                "n_nodes": 3,
            }

        s_in = {
            "n_nodes": 3,
            "initial_guess": {"lat": 37.5, "lon": -122.1, "alt_km": 3.0},
            "measurements": [],
        }
        item = (s_in, {}, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        # All 4 altitude layers tried
        assert set(calls) == {3.0, 6.0, 9.0, 12.0}
        # Best result (rms=0.1 at 9 km) selected
        assert result is not None
        assert result["alt_m"] == pytest.approx(9000.0)
        assert state.solver_successes == 1

    def test_n2_uses_association_altitude_directly(self, monkeypatch):
        """For n_nodes=2, no altitude sweep; association altitude used as-is."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        calls: list[float] = []

        def solve_fn(s_in, cfgs):
            calls.append(s_in.get("initial_guess", {}).get("alt_km"))
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "rms_delay": 0.5,
                "timestamp_ms": 8000,
                "contributing_node_ids": ["n1", "n2"],
                "n_nodes": 2,
            }

        s_in = {
            "n_nodes": 2,
            "initial_guess": {"lat": 37.5, "lon": -122.1, "alt_km": 6.0},
            "measurements": [],
        }
        item = (s_in, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        # Exactly one call with the original association altitude
        assert calls == [6.0]

