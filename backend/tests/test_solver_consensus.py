"""Tests for the consensus hypothesis stage (solver.py's _consensus_select),
productionized from the bench-only consensus_estimator.py prototype.

Bench measurement (association_bench.py --estimator lm vs consensus-refine,
5 seeds x default + dense scenes): n>=3 ghost solves ~3x down (13-16 vs
~46), position error slightly better, zero real tracks lost, LM-equivalent
per-solve cost.  This module pins the production wiring: consensus runs
once at n>=3, ahead of the LM altitude sweep, and either filters the input
(active), records what it would have done without touching the input
(shadow), or is a no-op (off, the default) — the LM solve, its trimming,
and its gate stack are unchanged either way; see _consensus_select's
docstring for the fallback rules.
"""

import time

from core import state
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0

_MISSING = object()


def _stub_result(node_ids, rms_delay, per_node=None, lat=LAT, lon=LON, **overrides):
    """A solve_multinode-shaped success dict for the given contributing
    nodes.  Same helper as test_solver_trimming.py."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": rms_delay,
        "rms_doppler": 5.0,
        "n_nodes": len(node_ids),
        "n_measurements": len(node_ids),
        "contributing_node_ids": list(node_ids),
    }
    if per_node is not None:
        result["per_node_delay_res_us"] = dict(per_node)
    result.update(overrides)
    return result


def _s_in(node_ids, lat=LAT, lon=LON, alt_km=9.0, **overrides):
    s_in = {
        "initial_guess": {"lat": lat, "lon": lon, "alt_km": alt_km},
        "measurements": [{"node_id": nid, "delay_us": 10.0, "doppler_hz": 1.0, "snr": 15.0} for nid in node_ids],
        "n_nodes": len(node_ids),
        "timestamp_ms": int(time.time() * 1000),
    }
    s_in.update(overrides)
    return s_in


def _stub_solve_fn(table: dict):
    """solve_fn keyed on the frozenset of node_ids in s_in["measurements"].

    Same helper as test_solver_trimming.py — registering only the subset a
    test expects to be solved proves consensus's filter (or lack of it)
    actually reached the LM: an unregistered node set comes back
    success=False, which cannot publish.
    """

    def fn(s_in, node_cfgs):
        nodes = frozenset(m["node_id"] for m in s_in["measurements"])
        base = table.get(nodes, _MISSING)
        if base is _MISSING:
            return {"success": False}
        if base is None:
            return None
        return dict(base)

    return fn


def _stub_select_fn(selection):
    """select_fn stub returning `selection` verbatim.

    `selection` is a selection dict (as select_consensus would return), or
    None (abstain), or an Exception instance/class to raise instead.
    """

    def fn(s_in, node_cfgs):
        if isinstance(selection, Exception):
            raise selection
        if isinstance(selection, type) and issubclass(selection, Exception):
            raise selection("stub select_fn raise")
        return selection

    return fn


class _ConsensusTestBase:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, select_fn, cfgs=None):
        return solver_mod._process_solver_item((dict(s_in), cfgs or {}, time.time()), solve_fn, select_fn)


def _selection(node_ids, input_node_ids, **overrides):
    """A select_consensus-shaped selection dict."""
    sel = {
        "node_ids": list(node_ids),
        "input_node_ids": list(input_node_ids),
        "lat": LAT,
        "lon": LON,
        "alt_km": 9.0,
        "n_candidates": 2 * len(node_ids),
        "n_clusters": 1,
        "n_corroborated_clusters": 1,
        "n_pairs": len(node_ids),
        "mean_residual_us": 0.3,
        "centroid_offset_km": 0.2,
    }
    sel.update(overrides)
    return sel


class TestConsensusFiltersAndPublishes(_ConsensusTestBase):
    """Active mode: consensus's selection replaces the LM's input."""

    def test_filters_to_selection_and_publishes(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        full_nodes = ["n1", "n2", "n3", "n4"]
        selected_nodes = ["n1", "n2", "n3"]
        selection = _selection(selected_nodes, full_nodes)
        # Registers ONLY the 3-node frozenset: a publish is only possible
        # if the LM actually ran on the filtered set.
        table = {
            frozenset(selected_nodes): _stub_result(
                selected_nodes,
                rms_delay=0.5,
            )
        }
        s_in = _s_in(
            full_nodes,
            track_ids=["t1", "t2", "t3", "t4"],
            track_ids_by_node={"n1": ["t1"], "n2": ["t2"], "n3": ["t3"], "n4": ["t4"]},
        )

        result = self._run(s_in, _stub_solve_fn(table), _stub_select_fn(selection))

        assert result is not None and result["success"]
        assert result["contributing_node_ids"] == selected_nodes

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["source_track_ids"] == ["t1", "t2", "t3"]
        assert rec["consensus_meta"]["outcome"] == "selected"
        assert rec["consensus_meta"]["dropped_node_ids"] == ["n4"]
        assert rec["consensus_meta"]["selected_node_ids"] == selected_nodes
        assert state.solver_consensus_selected == 1
        assert state.solver_consensus_filtered == 1


class TestConsensusFallbacks(_ConsensusTestBase):
    """Every non-selecting outcome falls back to the unfiltered input."""

    FULL = ["n1", "n2", "n3", "n4"]

    def _table(self):
        return {frozenset(self.FULL): _stub_result(self.FULL, rms_delay=0.5)}

    def test_abstain_falls_back_to_full_set(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        s_in = _s_in(self.FULL, track_ids=["t1", "t2", "t3", "t4"])

        result = self._run(s_in, _stub_solve_fn(self._table()), _stub_select_fn(None))

        assert result is not None and result["success"]
        assert result["contributing_node_ids"] == self.FULL
        assert state.solver_consensus_fallback == 1
        assert state.solver_consensus_selected == 0
        rec = state.mlat_solve_history[-1]
        assert rec["consensus_meta"]["outcome"] == "fallback_abstained"
        assert rec["source_track_ids"] == ["t1", "t2", "t3", "t4"]

    def test_too_few_selected_falls_back(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        selection = _selection(["n1", "n2"], self.FULL)
        s_in = _s_in(self.FULL)

        result = self._run(
            s_in,
            _stub_solve_fn(self._table()),
            _stub_select_fn(selection),
        )

        assert result is not None and result["success"]
        assert result["contributing_node_ids"] == self.FULL
        assert state.solver_consensus_fallback == 1
        rec = state.mlat_solve_history[-1]
        assert rec["consensus_meta"]["outcome"] == "fallback_small"
        # Selection stats still ride along even though it was not acted on.
        assert rec["consensus_meta"]["node_ids"] == ["n1", "n2"]

    def test_raising_select_fn_falls_back(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        s_in = _s_in(self.FULL)

        result = self._run(
            s_in,
            _stub_solve_fn(self._table()),
            _stub_select_fn(RuntimeError("consensus blew up")),
        )

        assert result is not None and result["success"]
        assert result["contributing_node_ids"] == self.FULL
        assert state.solver_consensus_fallback == 1
        rec = state.mlat_solve_history[-1]
        assert rec["consensus_meta"]["outcome"] == "fallback_error"


class TestConsensusDisabledAndN2(_ConsensusTestBase):
    """select_fn is only ever reached at n>=3 with mode != off."""

    @staticmethod
    def _boom_select_fn(*_a, **_kw):
        raise AssertionError("select_fn must not be called")

    def test_mode_off_never_calls_select_fn(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "off")
        full_nodes = ["n1", "n2", "n3", "n4"]
        table = {frozenset(full_nodes): _stub_result(full_nodes, rms_delay=0.5)}

        result = self._run(
            _s_in(full_nodes),
            _stub_solve_fn(table),
            self._boom_select_fn,
        )

        assert result is not None and result["success"]
        assert state.solver_consensus_selected == 0
        assert state.solver_consensus_fallback == 0

    def test_n2_never_calls_select_fn_even_when_active(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        two_nodes = ["n1", "n2"]
        s_in = _s_in(two_nodes, chi2_per_dof=0.5, n_epochs=8)

        def solve_fn(_s_in, _cfgs):
            return _stub_result(two_nodes, rms_delay=0.5, n_nodes=2)

        result = self._run(s_in, solve_fn, self._boom_select_fn)

        assert result is not None and result["success"]
        assert state.solver_consensus_selected == 0
        assert state.solver_consensus_fallback == 0


class TestConsensusShadow(_ConsensusTestBase):
    """Shadow mode: consensus runs and is recorded, but never filters."""

    def test_shadow_mode_records_meta_without_filtering(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "shadow")
        full_nodes = ["n1", "n2", "n3", "n4"]
        selected_nodes = ["n1", "n2", "n3"]
        selection = _selection(selected_nodes, full_nodes)
        # Registers ONLY the full set: a publish proves the LM saw the
        # unfiltered input, not consensus's selection.
        table = {frozenset(full_nodes): _stub_result(full_nodes, rms_delay=0.5)}

        result = self._run(
            _s_in(full_nodes),
            _stub_solve_fn(table),
            _stub_select_fn(selection),
        )

        assert result is not None and result["success"]
        assert result["contributing_node_ids"] == full_nodes
        assert state.solver_consensus_shadow == 1
        assert state.solver_consensus_selected == 0
        assert state.solver_consensus_filtered == 0

        rec = state.mlat_solve_history[-1]
        assert rec["consensus_meta"]["outcome"] == "shadow_selected"
        assert rec["consensus_meta"]["dropped_node_ids"] == ["n4"]


class TestConsensusThenTrim(_ConsensusTestBase):
    """Consensus filters 6 -> 5, then a subsequent rms_delay failure trims
    5 -> 4 — both stages must show up on the one history record."""

    def test_consensus_selection_then_trim_both_recorded(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        # "bad" has to be one of the original 6 measurement node ids for
        # _filter_s_in_to_nodes to actually carry it through the consensus
        # filter step — it is the node consensus keeps (uncorroborated "n5"
        # is what consensus drops) but trimming later sheds for a bad
        # residual.
        full_nodes = ["n1", "n2", "n3", "n4", "n5", "bad"]
        selected_5 = ["n1", "n2", "n3", "n4", "bad"]
        trimmed_4 = ["n1", "n2", "n3", "n4"]
        selection = _selection(selected_5, full_nodes)
        table = {
            frozenset(selected_5): _stub_result(
                selected_5,
                rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.5, "n3": 0.5, "n4": 0.5, "bad": 12.0},
            ),
            frozenset(trimmed_4): _stub_result(
                trimmed_4,
                rms_delay=0.8,
                per_node={"n1": 0.3, "n2": 0.3, "n3": 0.3, "n4": 0.3},
            ),
        }

        result = self._run(
            _s_in(full_nodes),
            _stub_solve_fn(table),
            _stub_select_fn(selection),
        )

        assert result is not None and result["success"]
        assert result["n_nodes"] == 4
        assert set(result["contributing_node_ids"]) == set(trimmed_4)

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["consensus_meta"]["outcome"] == "selected"
        assert rec["consensus_meta"]["selected_node_ids"] == sorted(selected_5)
        assert rec["pre_trim_n_nodes"] == 5
        assert rec["trimmed_node_ids"] == ["bad"]
        assert state.solver_consensus_selected == 1
        assert state.solver_consensus_filtered == 1
        assert state.solver_trimmed == 1


class TestConsensusMetaOnReject(_ConsensusTestBase):
    """consensus_meta survives on a reject, not just on publish."""

    def test_rejected_rms_delay_still_carries_consensus_meta(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        full_nodes = ["n1", "n2", "n3", "n4"]
        selected = ["n1", "n2", "n3"]
        selection = _selection(selected, full_nodes)
        # No per_node_delay_res_us -> trimming never engages (n=3 is below
        # the n>=4 trim floor anyway); the rms gate rejects outright.
        table = {frozenset(selected): _stub_result(selected, rms_delay=8.0)}

        result = self._run(
            _s_in(full_nodes),
            _stub_solve_fn(table),
            _stub_select_fn(selection),
        )

        assert result is not None
        assert state.multinode_tracks == {}
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_rms_delay"
        assert rec["consensus_meta"]["outcome"] == "selected"
        assert rec["consensus_meta"]["selected_node_ids"] == sorted(selected)


class TestFilterHelperProvenance:
    """Direct unit tests of _filter_s_in_to_nodes — no solver dispatch."""

    def test_filters_measurements_and_recomputes_n_nodes(self):
        s_in = _s_in(["n1", "n2", "n3", "n4"])
        filtered = solver_mod._filter_s_in_to_nodes(s_in, ["n1", "n3"])
        assert {m["node_id"] for m in filtered["measurements"]} == {"n1", "n3"}
        assert filtered["n_nodes"] == 2

    def test_rebuilds_track_ids_by_node_and_track_ids(self):
        s_in = _s_in(
            ["n1", "n2", "n3"],
            track_ids_by_node={"n1": ["t1"], "n2": ["t2", "t2b"], "n3": ["t3"]},
            track_ids=["t1", "t2", "t2b", "t3"],
        )
        filtered = solver_mod._filter_s_in_to_nodes(s_in, ["n1", "n2"])
        assert filtered["track_ids_by_node"] == {"n1": ["t1"], "n2": ["t2", "t2b"]}
        assert filtered["track_ids"] == ["t1", "t2", "t2b"]

    def test_no_track_ids_by_node_leaves_track_ids_unchanged(self):
        s_in = _s_in(["n1", "n2", "n3"], track_ids=["t1", "t2", "t3"])
        filtered = solver_mod._filter_s_in_to_nodes(s_in, ["n1", "n2"])
        assert filtered["track_ids"] == ["t1", "t2", "t3"]
        assert "track_ids_by_node" not in filtered

    def test_original_s_in_is_not_mutated(self):
        s_in = _s_in(["n1", "n2", "n3"])
        original_measurements = list(s_in["measurements"])
        solver_mod._filter_s_in_to_nodes(s_in, ["n1"])
        assert s_in["measurements"] == original_measurements
        assert s_in["n_nodes"] == 3


class TestDisplacementAnchor(_ConsensusTestBase):
    """Live-measured (first active-mode window): 131 displacement kills, 80
    of them <3 km from truth and 110 consensus-selected, median centroid
    offset 3.04 km against the 2 km cap — the gate was anchored to the
    contaminated association guess consensus+LM exist to correct, so it was
    killing the corrections rather than the mis-associations.  For a
    consensus-selected solve the gate now measures against the corroborated
    centroid instead; shadow mode and every fallback_* outcome keep the
    guess anchor — consensus either never ran against this input or was
    not acted on, so its centroid is not a vetted reference there.
    """

    FULL = ["n1", "n2", "n3", "n4"]
    # ~5 km north of the guess (LAT, LON) — comfortably past the 2 km cap
    # from the guess, and comfortably inside it from itself.
    CENTROID_LAT = LAT + 5.0 / 111.32
    CENTROID_LON = LON
    # Far from both the guess and the centroid above (~106-111 km either
    # way) — unambiguously rejects regardless of which anchor is in play.
    FAR_LAT, FAR_LON = 36.0, -82.0

    def _table_at(self, node_ids, lat, lon):
        return {
            frozenset(node_ids): _stub_result(
                node_ids,
                rms_delay=0.5,
                lat=lat,
                lon=lon,
            )
        }

    def test_active_selected_anchors_to_centroid_and_publishes(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        selection = _selection(
            self.FULL,
            self.FULL,
            lat=self.CENTROID_LAT,
            lon=self.CENTROID_LON,
        )
        # Solve lands ON the centroid: ~0 km from it (well under the cap),
        # ~5 km from the guess (over the cap) — only publishes if the gate
        # is measuring against the centroid.
        table = self._table_at(self.FULL, self.CENTROID_LAT, self.CENTROID_LON)

        result = self._run(
            _s_in(self.FULL),
            _stub_solve_fn(table),
            _stub_select_fn(selection),
        )

        assert result is not None and result["success"]
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["consensus_meta"]["displacement_anchor"] == "consensus"
        assert rec["displacement_km"] < 2.0

    def test_active_selected_rejects_when_off_both_anchors(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        selection = _selection(
            self.FULL,
            self.FULL,
            lat=self.CENTROID_LAT,
            lon=self.CENTROID_LON,
        )
        table = self._table_at(self.FULL, self.FAR_LAT, self.FAR_LON)

        result = self._run(
            _s_in(self.FULL),
            _stub_solve_fn(table),
            _stub_select_fn(selection),
        )

        assert result is None
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_displacement"
        assert rec["consensus_meta"]["displacement_anchor"] == "consensus"
        assert rec["displacement_km"] > 2.0

    def test_shadow_mode_keeps_guess_anchor(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "shadow")
        selection = _selection(
            self.FULL,
            self.FULL,
            lat=self.CENTROID_LAT,
            lon=self.CENTROID_LON,
        )
        # Shadow never filters — the LM sees (and solves) the full,
        # unfiltered set, landing on the centroid: ~5 km from the guess.
        table = self._table_at(self.FULL, self.CENTROID_LAT, self.CENTROID_LON)

        result = self._run(
            _s_in(self.FULL),
            _stub_solve_fn(table),
            _stub_select_fn(selection),
        )

        assert result is None
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_displacement"
        assert rec["consensus_meta"]["outcome"] == "shadow_selected"
        assert rec["consensus_meta"]["displacement_anchor"] == "guess"
        assert rec["displacement_km"] > 2.0

    def test_fallback_abstained_keeps_guess_anchor(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_CONSENSUS_MODE", "active")
        # select_fn abstains (returns None) -> fallback to the unfiltered
        # input, landing on the centroid: ~5 km from the guess.
        table = self._table_at(self.FULL, self.CENTROID_LAT, self.CENTROID_LON)

        result = self._run(
            _s_in(self.FULL),
            _stub_solve_fn(table),
            _stub_select_fn(None),
        )

        assert result is None
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_displacement"
        assert rec["consensus_meta"]["outcome"] == "fallback_abstained"
        assert rec["consensus_meta"]["displacement_anchor"] == "guess"
        assert rec["displacement_km"] > 2.0
