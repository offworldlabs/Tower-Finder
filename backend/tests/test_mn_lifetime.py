"""Tests for one-shot ghost suppression in the multinode solver track store.

Two problems in how state.multinode_tracks renders as mn-* aircraft:

- a track solved exactly once dead-reckons for the full 60 s entry lifetime
  (30 s DR cap) even though nothing has confirmed it is a real aircraft and
  not a mirror-point/wrong-frame ghost.  solver.py now stamps a solve_count
  onto every published entry, and aircraft_feed.py withholds rendering until
  a 2-node track has MN_N2_MIN_SOLVES solves, and caps an n>=3 one-shot's
  display window to MN_ONESHOT_TTL_S seconds.

- a later solve of the same aircraft that misses the 6 km track-key match
  (_multinode_track_key) mints a new entry instead of updating the old one,
  so the old ghost keeps rendering beside the new, correct entry.  solver.py
  now supersedes any earlier entry that shares a source single-node track id
  with the new solve, under a different key.
"""

import time
import types

from core import state
from services import aircraft_feed as aircraft_feed_mod
from services.id_utils import multinode_hex_from_key
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0


def _solve_fn(lat=LAT, lon=LON, **overrides):
    """A solve_fn returning a successful n>=2 result at (lat, lon), now."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": 1.0,
        "rms_doppler": 5.0,
        "n_nodes": 3,
        "n_measurements": 3,
        "contributing_node_ids": ["n1", "n2", "n3"],
    }
    result.update(overrides)

    def fn(s_in, cfgs):
        return dict(result)

    return fn


def _mn_entry(n_nodes: int, age_s: float, solve_count: int,
              lat: float = LAT, lon: float = LON) -> dict:
    """A state.multinode_tracks-shaped entry for feed-gate tests.

    Built directly (not via solver.py) so the feed gates are exercised in
    isolation from the solver's key/supersession machinery.
    """
    return {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 7000.0,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": 0.1,
        "rms_doppler": 1.0,
        "n_nodes": n_nodes,
        "n_measurements": n_nodes,
        "contributing_node_ids": [f"n{i}" for i in range(n_nodes)],
        "timestamp_ms": int((time.time() - age_s) * 1000),
        "solve_count": solve_count,
    }


class TestSolveCount:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item(
            (dict(s_in), cfgs or {}, time.time()), solve_fn
        )

    def test_first_publish_solve_count_is_1(self):
        self._run({"n_nodes": 3}, _solve_fn())
        (entry,) = state.multinode_tracks.values()
        assert entry["solve_count"] == 1

    def test_second_publish_same_position_solve_count_is_2(self):
        # Same position -> same key via the 6 km dark-track match, so the
        # second solve updates the existing entry instead of minting a new
        # one.
        self._run({"n_nodes": 3}, _solve_fn())
        self._run({"n_nodes": 3}, _solve_fn())
        assert len(state.multinode_tracks) == 1
        (entry,) = state.multinode_tracks.values()
        assert entry["solve_count"] == 2


class TestSupersession:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item(
            (dict(s_in), cfgs or {}, time.time()), solve_fn
        )

    def test_shared_track_id_supersedes_old_entry(self):
        self._run(
            {"n_nodes": 3, "track_ids": ["t1", "t2"]},
            _solve_fn(35.0, -82.0),
        )
        old_key = next(iter(state.multinode_tracks))
        # Pre-seed the anomaly hex the old entry would carry if it had ever
        # been through the feed, to prove supersession discards it.
        with state.anomaly_lock:
            state.anomaly_hexes.add(multinode_hex_from_key(old_key))

        # >6 km away -> the dark-track key matcher mints a new key, but this
        # solve shares track t2 with the old entry.
        self._run(
            {"n_nodes": 3, "track_ids": ["t2", "t3"]},
            _solve_fn(35.5, -82.0),
        )

        assert len(state.multinode_tracks) == 1
        (entry,) = state.multinode_tracks.values()
        # Old entry's solve_count (1) carries forward so the re-solved
        # aircraft is not hidden by the n=2 gate.
        assert entry["solve_count"] == 2
        assert multinode_hex_from_key(old_key) not in state.anomaly_hexes
        assert old_key not in state.multinode_tracks
        assert state.mn_superseded == 1

    def test_disjoint_track_ids_do_not_supersede(self):
        self._run(
            {"n_nodes": 3, "track_ids": ["t1"]},
            _solve_fn(35.0, -82.0),
        )
        self._run(
            {"n_nodes": 3, "track_ids": ["t9"]},
            _solve_fn(45.0, -90.0),
        )
        assert len(state.multinode_tracks) == 2
        assert all(
            e["solve_count"] == 1 for e in state.multinode_tracks.values()
        )
        assert state.mn_superseded == 0


class TestHistoryRecord:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def test_published_record_carries_solve_count_and_source_track_ids(self):
        solver_mod._process_solver_item(
            (
                {"n_nodes": 3, "track_ids": ["t2", "t1"]},
                {},
                time.time(),
            ),
            _solve_fn(),
        )
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["solve_count"] == 1
        assert rec["source_track_ids"] == ["t1", "t2"]


class TestFeedGates:
    def setup_method(self):
        state._reset_for_tests()

    def _build(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        return build_combined_aircraft_json(pipeline)

    def _mn_aircraft(self):
        return [a for a in self._build()["aircraft"] if a.get("multinode")]

    def test_n2_one_shot_is_not_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=2, age_s=1.0, solve_count=1)
        assert self._mn_aircraft() == []
        # Gated, not expired: stays in the store for the next solve to
        # confirm or supersede.
        assert "k" in state.multinode_tracks

    def test_n2_confirmed_is_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=2, age_s=1.0, solve_count=2)
        assert len(self._mn_aircraft()) == 1

    def test_n3_one_shot_within_ttl_is_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=3, age_s=3.0, solve_count=1)
        assert len(self._mn_aircraft()) == 1

    def test_n3_one_shot_past_ttl_is_not_rendered_but_not_expired(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=3, age_s=6.0, solve_count=1)
        assert self._mn_aircraft() == []
        assert "k" in state.multinode_tracks

    def test_n3_confirmed_old_entry_is_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=3, age_s=40.0, solve_count=2)
        assert len(self._mn_aircraft()) == 1

    def test_n2_gate_disabled_via_min_solves_1(self, monkeypatch):
        monkeypatch.setattr(aircraft_feed_mod, "MN_N2_MIN_SOLVES", 1)
        state.multinode_tracks["k"] = _mn_entry(n_nodes=2, age_s=1.0, solve_count=1)
        assert len(self._mn_aircraft()) == 1
