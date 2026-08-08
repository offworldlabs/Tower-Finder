"""Tests for GET /api/test/solver-stats — the Solver Report panel's data source.

Funnel/reject/error stats come from state.mlat_solve_history (windowed);
ghost detection and consensus/counters read current live state directly.
See routes.test._solver_window_stats for the ghost definition.
"""

import time
from collections import deque

from fastapi.testclient import TestClient

from core import state
from routes.test import _ERR_GT_GATE_KM, _GHOST_GATE_KM, _solver_window_stats


def _client():
    from main import app

    return TestClient(app)


def _rec(outcome, n_nodes=2, gt_error_km=None, age_s=0.0):
    return {
        "ts_ms": int((time.time() - age_s) * 1000),
        "outcome": outcome,
        "n_nodes": n_nodes,
        "gt_error_km": gt_error_km,
    }


class TestFunnelAndRejects:
    def setup_method(self):
        state._reset_for_tests()

    def test_funnel_splits_n2_vs_n3plus(self):
        state.mlat_solve_history.append(_rec("published", n_nodes=2))
        state.mlat_solve_history.append(_rec("published", n_nodes=2))
        state.mlat_solve_history.append(_rec("published", n_nodes=3))
        state.mlat_solve_history.append(_rec("published", n_nodes=4))
        out = _solver_window_stats(10.0)
        assert out["published"] == {"total": 4, "n2": 2, "n3plus": 2}
        assert out["attempts"] == 4

    def test_rejected_prefix_is_stripped(self):
        state.mlat_solve_history.append(_rec("rejected_beam"))
        state.mlat_solve_history.append(_rec("rejected_beam"))
        state.mlat_solve_history.append(_rec("rejected_displacement"))
        out = _solver_window_stats(10.0)
        assert out["rejects"] == {
            "total": 3,
            "by_reason": {"beam": 2, "displacement": 1},
        }

    def test_reject_reason_without_prefix_kept_as_is(self):
        # n2_unconfirmed / n2_outbid / unconverged never carried the
        # rejected_ prefix (services/tasks/solver.py); they must not be
        # mangled by the strip.
        state.mlat_solve_history.append(_rec("n2_unconfirmed"))
        state.mlat_solve_history.append(_rec("unconverged"))
        out = _solver_window_stats(10.0)
        assert out["rejects"]["by_reason"] == {"n2_unconfirmed": 1, "unconverged": 1}
        assert out["rejects"]["total"] == 2

    def test_window_excludes_old_records(self):
        state.mlat_solve_history.append(_rec("published", age_s=20 * 60))
        state.mlat_solve_history.append(_rec("published", age_s=1))
        out = _solver_window_stats(10.0)
        assert out["attempts"] == 1


class TestPositionErrorGate:
    def setup_method(self):
        state._reset_for_tests()

    def test_error_gate_inclusion_and_exclusion(self):
        # In-gate: 0.4 km. Out-of-gate: 40.0 km (way past _ERR_GT_GATE_KM).
        assert _ERR_GT_GATE_KM == 15.0
        state.mlat_solve_history.append(_rec("published", n_nodes=2, gt_error_km=0.4))
        state.mlat_solve_history.append(_rec("published", n_nodes=2, gt_error_km=40.0))
        state.mlat_solve_history.append(_rec("published", n_nodes=3, gt_error_km=None))
        out = _solver_window_stats(10.0)
        assert out["position_error_km"]["n"] == 1
        assert out["position_error_km"]["median"] == 0.4
        assert out["position_error_km"]["p90"] == 0.4

    def test_exact_median_and_p90(self):
        errs = [0.1, 0.2, 0.5, 1.0, 2.0]
        for e in errs:
            state.mlat_solve_history.append(_rec("published", n_nodes=2, gt_error_km=e))
        out = _solver_window_stats(10.0)
        # sorted = [0.1, 0.2, 0.5, 1.0, 2.0]; n=5
        # median idiom (matches validate_ground_truth's p50): sorted[n//2] == sorted[2] == 0.5
        # p90 per spec: sorted[int(0.9*(n-1))] == sorted[int(3.6)] == sorted[3] == 1.0
        assert out["position_error_km"]["median"] == 0.5
        assert out["position_error_km"]["p90"] == 1.0
        assert out["position_error_km"]["n"] == 5

    def test_empty_position_error_is_none(self):
        out = _solver_window_stats(10.0)
        assert out["position_error_km"] == {"median": None, "p90": None, "n": 0}


class TestGhosts:
    def setup_method(self):
        state._reset_for_tests()

    def test_adsb_associated_by_key_prefix(self):
        state.multinode_tracks["mn-adsb-abc123"] = {"lat": 35.0, "lon": -82.0}
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["live_tracks"] == 1
        assert out["ghosts"]["adsb_associated"] == 1
        assert out["ghosts"]["ghost_tracks"] == 0

    def test_adsb_associated_by_result_field(self):
        # Same association signal, this time via the result's own adsb_hex
        # field rather than the key prefix (belt-and-suspenders per spec).
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0, "adsb_hex": "abc123"}
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["adsb_associated"] == 1

    def test_close_to_ground_truth_is_matched_not_ghost(self):
        assert _GHOST_GATE_KM == 5.0
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~1 km north of the track.
        state.ground_truth_trails["gt1"] = deque([[35.009, -82.0, 9000.0, time.time()]])
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["gt_matched"] == 1
        assert out["ghosts"]["ghost_tracks"] == 0

    def test_far_from_everything_is_a_ghost(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~50 km away — outside both gates.
        state.ground_truth_trails["gt1"] = deque([[35.45, -82.0, 9000.0, time.time()]])
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["gt_matched"] == 0
        assert out["ghosts"]["ghost_tracks"] == 1
        assert out["ghosts"]["precision_pct"] == 0.0

    def test_close_to_fresh_adsb_is_not_a_ghost(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~1 km away, fresh (just seen).
        state.adsb_aircraft["dead1"] = {
            "lat": 35.009, "lon": -82.0, "last_seen_ms": int(time.time() * 1000),
        }
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["ghost_tracks"] == 0

    def test_stale_adsb_entry_does_not_rescue(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~1 km away but last seen 5 minutes ago — past the 60 s freshness gate.
        state.adsb_aircraft["dead1"] = {
            "lat": 35.009, "lon": -82.0,
            "last_seen_ms": int((time.time() - 300) * 1000),
        }
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["ghost_tracks"] == 1

    def test_precision_pct(self):
        state.multinode_tracks["mn-adsb-a"] = {"lat": 35.0, "lon": -82.0}
        state.multinode_tracks["mn-dark-1"] = {"lat": 10.0, "lon": 10.0}  # ghost, nothing nearby
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["live_tracks"] == 2
        assert out["ghosts"]["ghost_tracks"] == 1
        assert out["ghosts"]["precision_pct"] == 50.0


class TestConsensusAndCounters:
    def setup_method(self):
        state._reset_for_tests()

    def test_consensus_and_counters_reflect_state(self):
        state.solver_successes = 5
        state.solver_failures = 2
        state.n2_unconfirmed = 1
        state.solver_trimmed = 3
        state.solver_stale_drops = 4
        state.solver_queue_drops = 6
        state.solver_consensus_selected = 7
        state.solver_consensus_filtered = 8
        state.solver_consensus_fallback = 9
        state.solver_consensus_shadow = 10
        out = _solver_window_stats(10.0)
        assert out["counters"] == {
            "successes": 5, "failures": 2, "n2_unconfirmed": 1,
            "solver_trimmed": 3, "stale_drops": 4, "queue_drops": 6,
        }
        assert out["consensus"]["selected"] == 7
        assert out["consensus"]["filtered"] == 8
        assert out["consensus"]["fallback"] == 9
        assert out["consensus"]["shadow"] == 10
        assert "mode" in out["consensus"]


class TestEmptyState:
    def setup_method(self):
        state._reset_for_tests()

    def test_empty_state_no_division_errors(self):
        out = _solver_window_stats(10.0)
        assert out["attempts"] == 0
        assert out["published"] == {"total": 0, "n2": 0, "n3plus": 0}
        assert out["rejects"] == {"total": 0, "by_reason": {}}
        assert out["position_error_km"] == {"median": None, "p90": None, "n": 0}
        assert out["ghosts"]["live_tracks"] == 0
        assert out["ghosts"]["precision_pct"] == 0.0


class TestEndpoint:
    def setup_method(self):
        state._reset_for_tests()

    def test_default_window(self):
        resp = _client().get("/api/test/solver-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_minutes"] == 10.0

    def test_minutes_clamp_low(self):
        resp = _client().get("/api/test/solver-stats?minutes=0")
        assert resp.json()["window_minutes"] == 1.0

    def test_minutes_clamp_high(self):
        resp = _client().get("/api/test/solver-stats?minutes=1000")
        assert resp.json()["window_minutes"] == 35.0
