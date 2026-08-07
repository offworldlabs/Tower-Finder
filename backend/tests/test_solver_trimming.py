"""Tests for node-trimming recovery at the rms_delay gate (n>=4) and for
beam-rejection instrumentation.

Context (measured live, 35-min window): the rms_delay gate rejected 197 of
202 n>=6 solves, and half of those rejects were within 3 km of truth at
solve time.  Cause: one contaminated measurement (a single-node track from a
different aircraft, bundled in by association) inflates rms_delay while
Huber keeps the fitted position good.  solver.py's _trim_and_resolve drops
the worst-residual node(s) and re-solves, down to a floor of 3 nodes,
instead of discarding the whole solve.

Separately, the beam gate used to return on the first node that failed;
it now evaluates every contributing node and records per-node geometry
diagnostics in the solve history so a rejection can be understood after the
fact.
"""

import time

from core import state
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0

_MISSING = object()


def _stub_result(node_ids, rms_delay, per_node=None, lat=LAT, lon=LON,
                  **overrides):
    """A solve_multinode-shaped success dict for the given contributing
    nodes."""
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
        "measurements": [
            {"node_id": nid, "delay_us": 10.0, "doppler_hz": 1.0, "snr": 15.0}
            for nid in node_ids
        ],
        "n_nodes": len(node_ids),
        "timestamp_ms": int(time.time() * 1000),
    }
    s_in.update(overrides)
    return s_in


def _stub_solve_fn(table: dict):
    """solve_fn keyed on the frozenset of node_ids in s_in["measurements"].

    _solve_best_altitude calls solve_fn once per altitude layer — the stub
    must answer identically regardless of which layer it is asked to try, so
    it is keyed only on which nodes are present.  A registered value of None
    simulates a re-solve that fails to converge; an unregistered node set
    simulates one solve_multinode itself rejects outright.
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


class _TrimmingTestBase:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item(
            (dict(s_in), cfgs or {}, time.time()), solve_fn
        )


class TestTrimRecoversPublish(_TrimmingTestBase):
    """Case 1: a bad node's residual sinks rms_delay; dropping it alone
    recovers a solve that publishes."""

    def test_trim_drops_bad_node_and_publishes(self):
        full_nodes = ["n1", "n2", "n3", "n4", "bad"]
        trim_nodes = ["n1", "n2", "n3", "n4"]
        table = {
            frozenset(full_nodes): _stub_result(
                full_nodes, rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.5, "n3": 0.5, "n4": 0.5,
                          "bad": 12.0},
            ),
            frozenset(trim_nodes): _stub_result(
                trim_nodes, rms_delay=0.8,
                per_node={"n1": 0.3, "n2": 0.3, "n3": 0.3, "n4": 0.3},
            ),
        }
        result = self._run(_s_in(full_nodes), _stub_solve_fn(table))

        assert result is not None and result["success"]
        assert result["n_nodes"] == 4
        (entry,) = state.multinode_tracks.values()
        assert entry["n_nodes"] == 4

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["trimmed_node_ids"] == ["bad"]
        assert rec["pre_trim_rms_delay"] == 8.0
        assert rec["pre_trim_n_nodes"] == 5
        assert rec["trim_rounds"] == 1
        assert state.solver_trimmed == 1


class TestTrimPrunesSourceTracks(_TrimmingTestBase):
    """Case 2: when the input carries per-node track ids, a trim also drops
    the dropped node's tracks from the published provenance; without that
    field the original track_ids pass through untouched."""

    _FULL = ["n1", "n2", "n3", "n4", "bad"]
    _TRIM = ["n1", "n2", "n3", "n4"]
    _PER_NODE = {"n1": 0.5, "n2": 0.5, "n3": 0.5, "n4": 0.5, "bad": 12.0}

    def _table(self):
        return {
            frozenset(self._FULL): _stub_result(
                self._FULL, rms_delay=8.0, per_node=self._PER_NODE,
            ),
            frozenset(self._TRIM): _stub_result(
                self._TRIM, rms_delay=0.8,
                per_node={"n1": 0.3, "n2": 0.3, "n3": 0.3, "n4": 0.3},
            ),
        }

    def test_track_ids_by_node_prunes_the_dropped_node(self):
        s_in = _s_in(
            self._FULL,
            track_ids=["t1", "t2", "t3", "t4", "tbad"],
            track_ids_by_node={
                "n1": ["t1"], "n2": ["t2"], "n3": ["t3"], "n4": ["t4"],
                "bad": ["tbad"],
            },
        )
        result = self._run(s_in, _stub_solve_fn(self._table()))
        assert result is not None and result["success"]
        assert result["source_track_ids"] == ["t1", "t2", "t3", "t4"]

    def test_without_track_ids_by_node_track_ids_are_unchanged(self):
        s_in = _s_in(
            self._FULL,
            track_ids=["t1", "t2", "t3", "t4", "tbad"],
        )
        result = self._run(s_in, _stub_solve_fn(self._table()))
        assert result is not None and result["success"]
        assert result["source_track_ids"] == [
            "t1", "t2", "t3", "t4", "tbad",
        ]


class TestTrimFloor(_TrimmingTestBase):
    """Case 3: dropping every over-threshold candidate would go below the
    3-node floor — keep the 3 lowest-residual nodes instead, and never
    re-solve with fewer than that."""

    def test_floor_keeps_three_lowest_residual_nodes(self):
        nodes4 = ["n1", "n2", "n3", "n4"]
        nodes3 = ["n1", "n2", "n3"]
        called_node_sets: list[frozenset] = []
        table = {
            frozenset(nodes4): _stub_result(
                nodes4, rms_delay=4.0,
                per_node={"n1": 0.5, "n2": 0.5, "n3": 7.0, "n4": 8.0},
            ),
            frozenset(nodes3): _stub_result(
                nodes3, rms_delay=2.0,
                per_node={"n1": 0.3, "n2": 0.3, "n3": 0.3},
            ),
        }

        def solve_fn(s_in, cfgs):
            nodes = frozenset(m["node_id"] for m in s_in["measurements"])
            called_node_sets.append(nodes)
            base = table.get(nodes, _MISSING)
            return dict(base) if base not in (_MISSING, None) else None

        result = self._run(_s_in(nodes4), solve_fn)

        assert result is not None and result["success"]
        assert result["n_nodes"] == 3
        assert all(len(ns) >= 3 for ns in called_node_sets)

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert sorted(rec["trimmed_node_ids"]) == ["n4"]


class TestTrimGivesUp(_TrimmingTestBase):
    """Case 4: rms never clears the gate no matter what is trimmed — reject,
    but the reject record still shows what trimming tried, and nothing
    publishes."""

    def test_persistent_bad_rms_ends_in_reject_with_trim_metadata(self):
        nodes5 = ["n1", "n2", "n3", "n4", "n5"]
        nodes4 = ["n1", "n2", "n3", "n4"]
        nodes3 = ["n1", "n2", "n3"]
        table = {
            frozenset(nodes5): _stub_result(
                nodes5, rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.6, "n3": 0.7, "n4": 0.8,
                          "n5": 20.0},
            ),
            frozenset(nodes4): _stub_result(
                nodes4, rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.6, "n3": 0.7, "n4": 9.0},
            ),
            frozenset(nodes3): _stub_result(
                nodes3, rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.6, "n3": 0.7},
            ),
        }
        result = self._run(_s_in(nodes5), _stub_solve_fn(table))

        # Rejected, not a crash: the last trimmed (3-node) result comes back,
        # just not published.
        assert result is not None and result["success"]
        assert result["rms_delay"] == 8.0
        assert state.multinode_tracks == {}
        assert state.solver_trimmed == 0

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_rms_delay"
        assert rec["pre_trim_n_nodes"] == 5
        assert rec["pre_trim_rms_delay"] == 8.0
        assert sorted(rec["trimmed_node_ids"]) == ["n4", "n5"]


class TestTrimSkippedWithoutResiduals(_TrimmingTestBase):
    """Case 5: an old lib build with no per_node_delay_res_us — trimming
    never runs, and the reject record carries no trim keys at all."""

    def test_missing_residuals_disables_trimming(self):
        nodes5 = ["n1", "n2", "n3", "n4", "n5"]
        result = self._run(
            _s_in(nodes5),
            _stub_solve_fn({frozenset(nodes5): _stub_result(
                nodes5, rms_delay=8.0,
            )}),
        )
        assert result is not None and result.get("rms_delay") == 8.0
        assert state.multinode_tracks == {}

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_rms_delay"
        assert "trimmed_node_ids" not in rec
        assert "pre_trim_rms_delay" not in rec


class TestTrimResolveFailure(_TrimmingTestBase):
    """Case 6: the trimmed re-solve itself fails to converge — trimming is
    abandoned and the ORIGINAL (untrimmed) result is what gets gated, not a
    crash and not a success-less publish."""

    def test_failed_resolve_falls_back_to_pre_trim_result(self):
        full_nodes = ["n1", "n2", "n3", "n4", "bad"]
        trim_nodes = ["n1", "n2", "n3", "n4"]
        table = {
            frozenset(full_nodes): _stub_result(
                full_nodes, rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.5, "n3": 0.5, "n4": 0.5,
                          "bad": 20.0},
            ),
            frozenset(trim_nodes): None,  # re-solve fails to converge
        }
        result = self._run(_s_in(full_nodes), _stub_solve_fn(table))

        assert result is not None and result["success"]
        assert result["n_nodes"] == 5  # pre-trim result, untouched
        assert result["rms_delay"] == 8.0
        assert state.multinode_tracks == {}

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_rms_delay"
        assert "trimmed_node_ids" not in rec


class TestBeamInstrumentation(_TrimmingTestBase):
    """Case 7: the beam gate now evaluates every contributing node and
    records per-node geometry for each failure."""

    def test_out_of_beam_records_per_node_diagnostics(self):
        s_in = {
            "n_nodes": 1,
            "measurements": [
                {"node_id": "n1", "delay_us": 10.0, "doppler_hz": 1.0,
                 "snr": 15.0},
            ],
            "timestamp_ms": int(time.time() * 1000),
        }
        cfgs = {
            "n1": {
                "rx_lat": 35.0, "rx_lon": -82.0,
                "beam_azimuth_deg": 90.0, "beam_width_deg": 10.0,
                "max_range_km": 5.0,
            },
        }

        def solve_fn(_s_in, _cfgs):
            return _stub_result(
                ["n1"], rms_delay=1.0, lat=36.0, lon=-82.0,
            )

        result = self._run(s_in, solve_fn, cfgs=cfgs)

        assert result is None
        assert state.solver_fail_beam == 1
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_beam"
        failures = rec["beam_failures"]
        assert len(failures) == 1
        assert failures[0]["node_id"] == "n1"
        assert isinstance(failures[0]["range_km"], float)
        assert failures[0]["range_km"] > 5.0
        assert isinstance(failures[0]["bearing_off_deg"], float)


class TestTrimEnvKnob(_TrimmingTestBase):
    """Case 8: raising SOLVER_RMS_DELAY_MAX_US (env-overridable at import,
    monkeypatched here) lets a solve pass without ever trimming."""

    def test_raised_threshold_publishes_untrimmed(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_SOLVER_RMS_DELAY_MAX_US", 10.0)
        nodes5 = ["n1", "n2", "n3", "n4", "n5"]
        result = self._run(
            _s_in(nodes5),
            _stub_solve_fn({frozenset(nodes5): _stub_result(
                nodes5, rms_delay=8.0,
                per_node={"n1": 0.5, "n2": 0.5, "n3": 0.5, "n4": 0.5,
                          "n5": 12.0},
            )}),
        )

        assert result is not None and result["success"]
        assert result["n_nodes"] == 5
        (entry,) = state.multinode_tracks.values()
        assert entry["n_nodes"] == 5

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert "trimmed_node_ids" not in rec
        assert state.solver_trimmed == 0
