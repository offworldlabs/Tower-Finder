"""Tests for velocity direction error measurement and CV-fit adoption.

Context: solved positions often land near the true aircraft but the velocity
vector points the wrong way, and nothing measured that — the fleet metric
compared speed magnitude only.  Velocity is Doppler-determined (one
projection per node), so n=2 is underdetermined and n=3 exactly determined:
single-epoch noise maps straight into the vector.  This module covers:

- _nearest_gt's gt_speed_ms/gt_heading_deg stamp (trail-derived, falling
  back to ground_truth_meta) and the heading_err_deg/vel_err_ms fields
  _record_solve_history derives from it;
- adopting fit_constant_velocity's velocity (already run for the n=2 chi2
  gate) into published solves when it clears its own quality gate, with the
  fit result cached on the input so the gate and adoption share one pool
  call;
- the fleet-wide heading/vector error stats in _velocity_accuracy.
"""

import math
import time
from collections import deque

import pytest

from core import state
from services.geo import offset_latlon_m
from services.tasks import analytics_refresh
from services.tasks import solver as solver_mod

LAT0, LON0 = 35.0, -82.0

# n=2 input whose track pairing has already passed the CV fit — same shape
# as test_mlat_history.py's fixture.  No "cv_epochs": adoption is a no-op
# without it, which is exactly what most of this file's tests that are NOT
# about adoption want.
_CONFIRMED_N2 = {"n_nodes": 2, "chi2_per_dof": 0.5, "n_epochs": 8}


def _vel_from_heading(speed_ms: float, heading_deg: float) -> tuple[float, float]:
    return (
        speed_ms * math.sin(math.radians(heading_deg)),
        speed_ms * math.cos(math.radians(heading_deg)),
    )


def _seed_gt_meta_only(hex_code: str, speed_ms: float, heading_deg: float,
                        ts: float | None = None) -> None:
    """A single-point trail (no trail-derivable velocity) + meta velocity —
    isolates the meta fallback path from the two-point trail derivation."""
    ts = ts if ts is not None else time.time()
    state.ground_truth_trails[hex_code] = deque([[LAT0, LON0, 9000.0, ts]])
    state.ground_truth_meta[hex_code] = {"speed_ms": speed_ms, "heading": heading_deg}


def _solve_fn(node_ids, lat=LAT0, lon=LON0, vel_east=0.0, vel_north=0.0,
              **overrides):
    """A solve_fn returning the same success dict regardless of altitude
    layer — _solve_best_altitude calls it once per layer for n>=3."""
    base = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": vel_east,
        "vel_north": vel_north,
        "vel_up": 0.0,
        "rms_delay": 1.0,
        "rms_doppler": 5.0,
        "n_nodes": len(node_ids),
        "n_measurements": len(node_ids),
        "contributing_node_ids": list(node_ids),
    }
    base.update(overrides)

    def fn(s_in, cfgs):
        return dict(base)

    return fn


def _s_in(node_ids, lat=LAT0, lon=LON0, alt_km=9.0, **overrides):
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


class _SolverTestBase:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item(
            (dict(s_in), cfgs or {}, time.time()), solve_fn
        )


# ── A1: _nearest_gt speed/heading stamp ──────────────────────────────────────


class TestNearestGtVelocity(_SolverTestBase):
    def test_speed_and_heading_derived_from_straight_trail(self):
        now = time.time()
        lat1, lon1 = offset_latlon_m(LAT0, LON0, east_m=1000.0, north_m=0.0)
        state.ground_truth_trails["abc123"] = deque([
            [LAT0, LON0, 9000.0, now - 10.0],
            [lat1, lon1, 9000.0, now],
        ])
        gt = solver_mod._nearest_gt(LAT0, LON0, now)
        assert gt["gt_hex"] == "abc123"
        assert gt["gt_speed_ms"] == pytest.approx(100.0, abs=5)
        assert gt["gt_heading_deg"] == pytest.approx(90.0, abs=2)

    def test_single_point_trail_falls_back_to_meta(self):
        now = time.time()
        state.ground_truth_trails["solo"] = deque([[LAT0, LON0, 9000.0, now]])
        state.ground_truth_meta["solo"] = {"speed_ms": 42.0, "heading": 123.0}
        gt = solver_mod._nearest_gt(LAT0, LON0, now)
        assert gt["gt_hex"] == "solo"
        assert gt["gt_speed_ms"] == 42.0
        assert gt["gt_heading_deg"] == 123.0

    def test_no_gt_returns_all_four_keys_none(self):
        gt = solver_mod._nearest_gt(LAT0, LON0, time.time())
        assert gt == {
            "gt_hex": None, "gt_error_km": None, "gt_lat": None, "gt_lon": None,
            "gt_speed_ms": None, "gt_heading_deg": None,
        }


# ── A2: heading_err_deg / vel_err_ms derivation ──────────────────────────────


class TestDerivedVelocityError(_SolverTestBase):
    def _record(self, vel_east, vel_north):
        result = {
            "lat": LAT0, "lon": LON0,
            "vel_east": vel_east, "vel_north": vel_north,
            "timestamp_ms": int(time.time() * 1000),
        }
        solver_mod._record_solve_history(
            "published", {}, result, raw_lat=LAT0, raw_lon=LON0,
        )
        return state.mlat_solve_history[-1]

    def test_heading_err_wraps_across_0_360(self):
        _seed_gt_meta_only("t1", speed_ms=50.0, heading_deg=350.0)
        ve, vn = _vel_from_heading(50.0, 10.0)
        rec = self._record(ve, vn)
        assert rec["heading_err_deg"] == pytest.approx(20.0, abs=0.2)

    def test_heading_err_none_when_truth_near_hover(self):
        _seed_gt_meta_only("t1", speed_ms=10.0, heading_deg=90.0)
        ve, vn = _vel_from_heading(10.0, 90.0)
        rec = self._record(ve, vn)
        assert rec["heading_err_deg"] is None

    def test_vel_err_ms_on_perpendicular_case(self):
        _seed_gt_meta_only("t1", speed_ms=50.0, heading_deg=0.0)
        ve, vn = _vel_from_heading(50.0, 90.0)  # perpendicular to truth
        rec = self._record(ve, vn)
        assert rec["vel_err_ms"] == pytest.approx(70.7, abs=0.5)
        # Still moving fast enough for a heading comparison too.
        assert rec["heading_err_deg"] == pytest.approx(90.0, abs=0.5)


# ── B: CV-fit velocity adoption ──────────────────────────────────────────────


def _fake_pool_call(fit_result, calls):
    def fn(target_fn, *args):
        calls.append((target_fn, args))
        return dict(fit_result) if fit_result is not None else None
    return fn


class TestVelocityAdoptionN3(_SolverTestBase):
    def test_good_fit_is_adopted_and_recorded(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            solver_mod, "_pool_call",
            _fake_pool_call(
                {"success": True, "n_epochs": 8, "chi2_per_dof": 0.5,
                 "vel_east": 123.4, "vel_north": -55.5, "vel_up": 3.0},
                calls,
            ),
        )
        node_ids = ["n1", "n2", "n3"]
        s_in = _s_in(node_ids, cv_epochs=[{"t_s": float(i)} for i in range(6)])
        result = self._run(
            s_in, _solve_fn(node_ids, vel_east=10.0, vel_north=5.0),
        )

        assert result is not None and result["success"]
        assert result["vel_source"] == "cv_fit"
        assert result["vel_east"] == pytest.approx(123.4)
        assert result["vel_north"] == pytest.approx(-55.5)
        assert result["vel_up"] == pytest.approx(3.0)
        assert result["solver_vel_east"] == pytest.approx(10.0)
        assert result["solver_vel_north"] == pytest.approx(5.0)
        assert len(calls) == 1

        (entry,) = state.multinode_tracks.values()
        assert entry["vel_source"] == "cv_fit"
        assert entry["vel_east"] == pytest.approx(123.4)

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["vel_source"] == "cv_fit"
        assert rec["vel_east"] == pytest.approx(123.4, abs=0.1)
        assert rec["vel_north"] == pytest.approx(-55.5, abs=0.1)
        assert rec["solver_vel_east"] == pytest.approx(10.0, abs=0.1)
        assert rec["solver_vel_north"] == pytest.approx(5.0, abs=0.1)


class TestVelocityAdoptionRejectionFallbacks(_SolverTestBase):
    def _publish_with_fit(self, fit_result, monkeypatch):
        calls = []
        monkeypatch.setattr(
            solver_mod, "_pool_call", _fake_pool_call(fit_result, calls),
        )
        node_ids = ["n1", "n2", "n3"]
        s_in = _s_in(node_ids, cv_epochs=[{"t_s": float(i)} for i in range(6)])
        result = self._run(
            s_in, _solve_fn(node_ids, vel_east=10.0, vel_north=5.0),
        )
        return result

    def test_chi2_above_adoption_threshold_keeps_raw_velocity(self, monkeypatch):
        result = self._publish_with_fit(
            {"success": True, "n_epochs": 8, "chi2_per_dof": 8.0,
             "vel_east": 999.0, "vel_north": 999.0, "vel_up": 0.0},
            monkeypatch,
        )
        assert result["vel_source"] == "solve"
        assert result["vel_east"] == pytest.approx(10.0)
        assert result["vel_north"] == pytest.approx(5.0)

    def test_too_few_epochs_keeps_raw_velocity(self, monkeypatch):
        result = self._publish_with_fit(
            {"success": True, "n_epochs": 3, "chi2_per_dof": 0.5,
             "vel_east": 999.0, "vel_north": 999.0, "vel_up": 0.0},
            monkeypatch,
        )
        assert result["vel_source"] == "solve"
        assert result["vel_east"] == pytest.approx(10.0)

    def test_fit_returns_none_keeps_raw_velocity(self, monkeypatch):
        result = self._publish_with_fit(None, monkeypatch)
        assert result["vel_source"] == "solve"
        assert result["vel_east"] == pytest.approx(10.0)

    def test_fit_raises_keeps_raw_velocity(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("pool broke")
        monkeypatch.setattr(solver_mod, "_pool_call", boom)
        node_ids = ["n1", "n2", "n3"]
        s_in = _s_in(node_ids, cv_epochs=[{"t_s": float(i)} for i in range(6)])
        result = self._run(
            s_in, _solve_fn(node_ids, vel_east=10.0, vel_north=5.0),
        )
        assert result["vel_source"] == "solve"
        assert result["vel_east"] == pytest.approx(10.0)


class TestVelocityAdoptionN2SharesPoolCall(_SolverTestBase):
    def test_gate_and_adoption_share_one_pool_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            solver_mod, "_pool_call",
            _fake_pool_call(
                {"success": True, "n_epochs": 8, "chi2_per_dof": 0.5,
                 "vel_east": 200.0, "vel_north": -80.0, "vel_up": 1.0},
                calls,
            ),
        )
        node_ids = ["n1", "n2"]
        s_in = _s_in(node_ids, cv_epochs=[{"t_s": float(i)} for i in range(6)])
        result = self._run(
            s_in, _solve_fn(node_ids, vel_east=1.0, vel_north=1.0),
        )

        assert result is not None and result["success"]
        assert result["vel_source"] == "cv_fit"
        assert len(calls) == 1


# ── A3: fleet-wide direction error stats ─────────────────────────────────────


class TestVelocityAccuracyDirection(_SolverTestBase):
    def test_heading_and_vector_stats_on_a_perpendicular_track(self):
        state.ground_truth_trails["truth1"] = deque(
            [[LAT0, LON0, 9000.0, time.time()]]
        )
        state.ground_truth_meta["truth1"] = {"speed_ms": 50.0, "heading": 0.0}
        ve, vn = _vel_from_heading(50.0, 90.0)  # 90 deg off, same speed
        state.multinode_tracks["mn-test"] = {
            "lat": LAT0, "lon": LON0, "vel_east": ve, "vel_north": vn,
        }

        out = analytics_refresh._velocity_accuracy()

        assert out["n_matched"] == 1
        assert "ratio_median" in out
        assert out["ratio_median"] == pytest.approx(1.0, abs=0.05)
        assert out["heading_err_median_deg"] == pytest.approx(90.0, abs=1.0)
        assert out["heading_err_p95_deg"] == pytest.approx(90.0, abs=1.0)
        assert out["vector_err_median_ms"] == pytest.approx(70.7, abs=0.5)
        assert out["vector_err_p95_ms"] == pytest.approx(70.7, abs=0.5)
