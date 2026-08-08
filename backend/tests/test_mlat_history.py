"""Tests for the per-solve MLAT history buffer and GET /api/test/mlat-history.

Every solver outcome — published or gate-rejected — lands in
state.mlat_solve_history so an individual map marker's solves can be looked
up by its mn<sha256[:10]> hex and checked after the fact.
"""

import time
from collections import deque

import pytest
from fastapi.testclient import TestClient

from core import state
from services.id_utils import multinode_hex_from_key
from services.tasks import solver as solver_mod

# n=2 input whose track pairing already passed the CV fit (see
# test_solver_worker.py for why a bare {"n_nodes": 2} is withheld).
_CONFIRMED_N2 = {"n_nodes": 2, "chi2_per_dof": 0.5, "n_epochs": 8}

LAT, LON = 35.0, -82.0


def _solve_fn(lat=LAT, lon=LON, **overrides):
    """A solve_fn returning a successful result at (lat, lon), now."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 10.0,
        "vel_north": 0.0,
        "rms_delay": 1.0,
        "rms_doppler": 5.0,
        "contributing_node_ids": ["n1", "n2"],
    }
    result.update(overrides)

    def fn(s_in, cfgs):
        return dict(result)

    return fn


def _put_gt(hex_code="abc123", lat=LAT + 0.001, lon=LON, age_s=0.0):
    state.ground_truth_trails[hex_code] = deque(
        [[lat, lon, 9000.0, time.time() - age_s]]
    )


class TestRecording:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item(
            (dict(s_in), cfgs or {}, time.time()), solve_fn
        )

    def _only_record(self):
        assert len(state.mlat_solve_history) == 1
        return state.mlat_solve_history[0]

    def test_published_solve_records_positions_and_gt(self):
        _put_gt()
        self._run(_CONFIRMED_N2, _solve_fn())
        rec = self._only_record()
        assert rec["outcome"] == "published"
        assert rec["solve_key"] is not None
        assert rec["solver_hex"] == multinode_hex_from_key(rec["solve_key"])
        # Raw position is the solver output; published lat/lon is the
        # (here identical — first solve) smoothed position.
        assert rec["raw_lat"] == pytest.approx(LAT)
        assert rec["lat"] == pytest.approx(LAT)
        # GT stamp frozen at solve time: 0.001° of latitude ≈ 0.111 km.
        assert rec["gt_hex"] == "abc123"
        assert rec["gt_error_km"] == pytest.approx(0.111, abs=0.01)

    def test_published_solver_hex_matches_feed_hex(self):
        """The recorded hex is the same one the feed shows on the map."""
        self._run(_CONFIRMED_N2, _solve_fn())
        rec = self._only_record()
        key = next(iter(state.multinode_tracks))
        assert rec["solver_hex"] == multinode_hex_from_key(key)

    def test_stale_gt_is_not_stamped(self):
        _put_gt(age_s=600.0)
        self._run(_CONFIRMED_N2, _solve_fn())
        assert self._only_record()["gt_hex"] is None

    def test_rms_delay_reject_is_recorded(self):
        self._run(_CONFIRMED_N2, _solve_fn(rms_delay=10.0))
        rec = self._only_record()
        assert rec["outcome"] == "rejected_rms_delay"
        assert rec["solver_hex"] is None
        assert rec["raw_lat"] == pytest.approx(LAT)

    def test_rms_doppler_reject_is_recorded(self):
        self._run(_CONFIRMED_N2, _solve_fn(rms_doppler=500.0))
        assert self._only_record()["outcome"] == "rejected_rms_doppler"

    def test_beam_reject_is_recorded(self):
        # Real geometry that fails the RANGE rule (applied at every n, so
        # it works for this n=2 _CONFIRMED_N2 input): rx placed ~556 km
        # from the solve position, well past a 50 km max_range_km — for
        # both contributing node ids the default _solve_fn() publishes
        # under (see contributing_node_ids in _solve_fn above).
        cfgs = {
            "n1": {"rx_lat": LAT + 5.0, "rx_lon": LON, "max_range_km": 50},
            "n2": {"rx_lat": LAT + 5.0, "rx_lon": LON, "max_range_km": 50},
        }
        self._run(_CONFIRMED_N2, _solve_fn(), cfgs=cfgs)
        rec = self._only_record()
        assert rec["outcome"] == "rejected_beam"
        assert rec["beam_failures"][0]["rule"] == "range"

    def test_displacement_reject_records_distance(self):
        s_in = dict(_CONFIRMED_N2)
        s_in["initial_guess"] = {"lat": LAT + 1.0, "lon": LON, "alt_km": 9.0}
        self._run(s_in, _solve_fn())
        rec = self._only_record()
        assert rec["outcome"] == "rejected_displacement"
        # 1° of latitude ≈ 111 km, way past the 2 km gate.
        assert rec["displacement_km"] == pytest.approx(111.2, abs=1.0)
        assert rec["guess_lat"] == pytest.approx(LAT + 1.0)

    def test_n2_unconfirmed_is_recorded(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_resolve_n2_chi2", lambda *a: None)
        self._run({"n_nodes": 2}, _solve_fn())
        rec = self._only_record()
        assert rec["outcome"] == "n2_unconfirmed"
        assert rec["chi2_per_dof"] is None

    def test_unconverged_is_recorded(self):
        self._run(_CONFIRMED_N2, _solve_fn(success=False))
        assert self._only_record()["outcome"] == "unconverged"

    def test_age_prune_drops_old_records(self):
        state.mlat_solve_history.append(
            {"ts_ms": int(time.time() * 1000) - 40 * 60 * 1000, "outcome": "published"}
        )
        self._run(_CONFIRMED_N2, _solve_fn())
        outcomes = [r["outcome"] for r in state.mlat_solve_history]
        assert len(state.mlat_solve_history) == 1
        assert outcomes == ["published"]

    def test_reset_for_tests_clears_history(self):
        self._run(_CONFIRMED_N2, _solve_fn())
        assert state.mlat_solve_history
        state._reset_for_tests()
        assert not state.mlat_solve_history


class TestEndpoint:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _client(self):
        from main import app

        return TestClient(app)

    def _publish(self, lat=LAT, lon=LON):
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()), _solve_fn(lat, lon)
        )

    def test_requires_hex_or_all(self):
        assert self._client().get("/api/test/mlat-history").status_code == 400

    def test_lookup_by_hex(self):
        self._publish()
        rec = state.mlat_solve_history[0]
        data = self._client().get(
            f"/api/test/mlat-history?hex={rec['solver_hex']}"
        ).json()
        assert data["n_solves"] == 1
        assert data["solves"][0]["solve_key"] == rec["solve_key"]

    def test_unknown_hex_returns_empty(self):
        self._publish()
        data = self._client().get("/api/test/mlat-history?hex=mn0000000000").json()
        assert data["n_solves"] == 0
        assert data["rejects_nearby"]["n"] == 0

    def test_all_returns_window(self):
        self._publish()
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(rms_delay=10.0),
        )
        data = self._client().get("/api/test/mlat-history?all=1").json()
        assert data["n_records"] == 2
        assert {r["outcome"] for r in data["records"]} == {
            "published", "rejected_rms_delay",
        }

    def test_rejects_nearby_within_10km(self):
        self._publish()
        # A reject 0.01° (~1.1 km) away: inside the 10 km radius.
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(LAT + 0.01, LON, rms_delay=10.0),
        )
        # A reject ~1° (~111 km) away: outside.
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(LAT + 1.0, LON, rms_delay=10.0),
        )
        hex_ = next(
            r["solver_hex"] for r in state.mlat_solve_history
            if r["outcome"] == "published"
        )
        data = self._client().get(f"/api/test/mlat-history?hex={hex_}").json()
        assert data["rejects_nearby"]["n"] == 1
        assert data["rejects_nearby"]["by_outcome"] == {"rejected_rms_delay": 1}


class TestTrailSnapshotRace:
    """_nearest_gt must survive concurrent trail appends (2026-08-08 outage).

    Both solver workers died with "deque mutated during iteration" — the
    Python-level min()/_trail_velocity loops over a live trail deque raced
    the sim-ingest append.  The fix snapshots each trail with tuple(), a
    single C call the appender cannot interleave.  This stress test raced
    reliably within a few hundred iterations before the fix.
    """

    def test_nearest_gt_survives_concurrent_appends(self):
        import threading

        now = time.time()
        trail = deque(
            [[LAT + i * 1e-4, LON, 9000.0, now - 60 + i] for i in range(80)],
            maxlen=4000,
        )
        state.ground_truth_trails["race"] = trail
        stop = threading.Event()

        def _appender():
            i = 0
            while not stop.is_set():
                trail.append([LAT + i * 1e-5, LON, 9000.0, now + i * 1e-3])
                i += 1

        t = threading.Thread(target=_appender, daemon=True)
        t.start()
        try:
            for _ in range(2000):
                solver_mod._nearest_gt(LAT, LON, now)
        finally:
            stop.set()
            t.join(timeout=5.0)
