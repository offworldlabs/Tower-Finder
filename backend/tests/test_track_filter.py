"""Unit tests for services.track_filter — the per-track-key Kalman filter
that replaced _ewma_smooth_track as the default multinode display smoother.

Covers:
- TRACK_SMOOTHER mode dispatch (off / ewma / kf / unknown-falls-back-to-kf)
- KF lifecycle basics: first-solve passthrough, duplicate/out-of-order
  passthrough, gap re-init, innovation-gate re-init, drop_key/reset
- Covariance weighting (tight cov trusts the measurement more than loose)
  and ADS-B velocity preference over the solved CV fit
- RMSE reduction on a seeded synthetic constant-velocity track
- Exact agreement with a Stone-Soup reference Kalman filter (skipped if
  stonesoup is not installed — nothing else in this file depends on it)
- _enu_offset_m as the exact inverse of services.geo.offset_latlon_m
"""

import datetime
import math
import os
import time

import numpy as np
import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services import track_filter  # noqa: E402
from services.geo import offset_latlon_m  # noqa: E402


def make_result(lat, lon, ts_ms, vel_east=None, vel_north=None, cov_en_km2=None):
    """Build a solver-result-shaped dict — the same shape solver.py hands to
    the smoother.  vel_east/vel_north/cov_en_km2 are omitted entirely unless
    given, matching the solver's own conditional fields (vel_east/vel_north
    are only set on an adopted CV fit; cov_en_km2 is only set when the solver
    could compute one)."""
    result = {"success": True, "lat": lat, "lon": lon, "timestamp_ms": ts_ms}
    if vel_east is not None:
        result["vel_east"] = vel_east
    if vel_north is not None:
        result["vel_north"] = vel_north
    if cov_en_km2 is not None:
        result["cov_en_km2"] = cov_en_km2
    return result


class TestModes:
    """TRACK_SMOOTHER selects the smoothing strategy: off / ewma / kf, and
    any unrecognised value falls back to kf."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_off_is_raw_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "off")
        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "off-key", None)
        assert out is result   # same object — no copy, no new keys
        assert "smoother" not in out
        assert "kf_pos_sigma_m" not in out

    def test_ewma_delegates_to_supplied_fn(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "ewma")
        calls = []

        def fake_ewma(result, track_key, adsb_hex):
            calls.append((track_key, adsb_hex))
            out = dict(result)
            out["smoother"] = "ewma-fake"
            return out

        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "ewma-key", "abc123", ewma_fn=fake_ewma)

        assert calls == [("ewma-key", "abc123")]
        assert out["smoother"] == "ewma-fake"

    def test_ewma_without_fn_is_raw_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "ewma")
        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "ewma-noop", None, ewma_fn=None)
        assert out is result

    def test_unknown_value_falls_back_to_kf(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "some-nonsense-value")
        key = "unknown-mode-key"
        r1 = make_result(35.0, -82.0, 1_000)
        out1 = track_filter.smooth_solve(r1, key, None)
        assert "smoother" not in out1   # first-ever solve on the kf path: raw

        r2 = make_result(35.001, -82.0, 21_000)
        out2 = track_filter.smooth_solve(r2, key, None)
        assert out2["smoother"] == "kf"   # proves the kf branch, not off/ewma, ran


class TestKFBasics:
    """Lifecycle behaviour of the KF path: init, passthrough conditions,
    gap re-init, innovation-gate re-init, and the drop_key/reset hooks."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_first_solve_is_raw_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "basics-1", None)
        assert out is result
        assert "smoother" not in out

    def test_duplicate_and_out_of_order_are_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-2"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)

        dup = make_result(35.001, -82.0, 1_000)   # dt == 0
        out_dup = track_filter.smooth_solve(dup, key, None)
        assert out_dup is dup

        earlier = make_result(35.001, -82.0, 500)   # dt < 0
        out_earlier = track_filter.smooth_solve(earlier, key, None)
        assert out_earlier is earlier

    def test_large_gap_reinits_then_next_solve_behaves_like_second_ever(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-3"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)

        # 200 s gap > _KF_MAX_GAP_S (160 s): re-init, raw return.
        gap_result = make_result(35.001, -82.0, 1_000 + 200_000)
        out_gap = track_filter.smooth_solve(gap_result, key, None)
        assert out_gap is gap_result

        # The NEXT solve, 20 s later, now has exactly one prior — it should
        # behave like the second-ever solve for a fresh key: smoothed output.
        next_result = make_result(35.0011, -82.0, gap_result["timestamp_ms"] + 20_000)
        out_next = track_filter.smooth_solve(next_result, key, None)
        assert out_next["smoother"] == "kf"

    def test_innovation_gate_reanchors_on_large_jump(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-4"
        lat0, lon0 = 35.0, -82.0
        ts = 1_000

        track_filter.smooth_solve(make_result(lat0, lon0, ts), key, None)
        for i in range(2):
            ts += 20_000
            lat_i, lon_i = offset_latlon_m(lat0, lon0, east_m=5.0 * i, north_m=5.0 * i)
            track_filter.smooth_solve(make_result(lat_i, lon_i, ts), key, None)

        # A ~50 km jump east — far outside anything the filter's own
        # predicted uncertainty could explain as the same track.
        ts += 20_000
        jump_lat, jump_lon = offset_latlon_m(lat0, lon0, east_m=50_000.0, north_m=0.0)
        jump_result = make_result(jump_lat, jump_lon, ts)
        out_jump = track_filter.smooth_solve(jump_result, key, None)
        assert out_jump is jump_result   # gate breach: raw return

        # The FOLLOWING solve, near the jump, must smooth against the NEW
        # anchor — close to the jump position, nowhere near the old track.
        ts += 20_000
        near_lat, near_lon = offset_latlon_m(jump_lat, jump_lon, east_m=20.0, north_m=0.0)
        out_near = track_filter.smooth_solve(make_result(near_lat, near_lon, ts), key, None)
        assert out_near["smoother"] == "kf"
        assert abs(out_near["lon"] - jump_lon) < 0.01
        assert abs(out_near["lon"] - lon0) > 0.1

    def test_drop_key_and_reset_clear_state(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-5"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        assert key in track_filter._KF_TRACKS

        track_filter.drop_key(key)
        assert key not in track_filter._KF_TRACKS

        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        assert key in track_filter._KF_TRACKS

        track_filter.reset()
        assert track_filter._KF_TRACKS == {}

    def test_smoothed_output_shape(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-6"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        out = track_filter.smooth_solve(make_result(35.001, -82.0, 21_000), key, None)

        assert out["smoother"] == "kf"
        assert math.isfinite(out["kf_pos_sigma_m"])
        assert out["kf_pos_sigma_m"] > 0
        assert out["lat"] == round(out["lat"], 6)
        assert out["lon"] == round(out["lon"], 6)


class TestKFWeighting:
    """The KF weights each solve by its per-solve covariance and prefers
    ADS-B velocity over the solved CV fit — the two behaviours a plain mean
    (the old EWMA) cannot give you."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_tight_covariance_trusts_measurement_more_than_loose(self, monkeypatch):
        """cov_en_km2 is inflated by _KF_R_INFLATE (4x sigma) and then
        clamped to [_KF_MIN_POS_SIGMA_M, _KF_MAX_POS_SIGMA_M] = [500, 8000] m
        before use (see TestRInflation for that mechanism in isolation) —
        neither input here survives unclamped, but the two stay strictly
        ordered on opposite sides of the band, which is all this test needs:

          tight: raw sigma  100 m -> inflated  400 m -> floored to   500 m
          loose: raw sigma 2000 m -> inflated 8000 m -> capped   at 8000 m

        so the qualitative claim (tighter reported cov -> posterior trusts
        the measurement more) still holds, and by a wider margin than before
        the floor/inflation existed (500 vs 8000 is a bigger contrast than
        the raw 100 vs 2000)."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        lat0, lon0 = 35.0, -82.0
        loose_cov = [[4.0, 0.0], [0.0, 4.0]]     # raw ~2 km sigma -> capped at 8000 m post-inflation
        tight_cov = [[0.01, 0.0], [0.0, 0.01]]   # raw ~100 m sigma -> floored at 500 m post-inflation

        def run(key, cov_third):
            ts = 1_000
            # Two identical, stationary, loosely-covariant solves build up
            # identical prior state/covariance for both keys.
            track_filter.smooth_solve(make_result(lat0, lon0, ts, cov_en_km2=loose_cov), key, None)
            ts += 20_000
            track_filter.smooth_solve(make_result(lat0, lon0, ts, cov_en_km2=loose_cov), key, None)
            ts += 20_000
            # Third measurement, offset well away from the dead-reckoned
            # (stationary) prediction — only its covariance differs per key.
            meas_lat, meas_lon = offset_latlon_m(lat0, lon0, east_m=600.0, north_m=0.0)
            return track_filter.smooth_solve(
                make_result(meas_lat, meas_lon, ts, cov_en_km2=cov_third), key, None
            )

        out_tight = run("weight-tight", tight_cov)
        out_loose = run("weight-loose", loose_cov)

        meas_east = 600.0
        e_tight, _ = track_filter._enu_offset_m(lat0, lon0, out_tight["lat"], out_tight["lon"])
        e_loose, _ = track_filter._enu_offset_m(lat0, lon0, out_loose["lat"], out_loose["lon"])

        # Tight cov: posterior lands closer to the raw measurement.
        # Loose cov: posterior stays closer to the (stationary) prediction.
        assert abs(e_tight - meas_east) < abs(e_loose - meas_east)

    def test_adsb_velocity_preferred_over_solved(self, monkeypatch):
        """Mirrors TestDarkSolveSmoothing.test_adsb_velocity_is_preferred_over_solved
        in test_solver_worker.py, but exercised directly through
        track_filter.smooth_solve rather than the full solver pipeline."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        state.adsb_aircraft["abc123"] = {
            # 100 m/s = 194.384 kt, due north.
            "gs": 194.384, "track": 0.0,
            "lat": 35.0, "lon": -82.0,
            "last_seen_ms": int(time.time() * 1000),
        }
        lat0, lon0 = 35.0, -82.0
        d20s_deg = 2000.0 / 111_320.0   # ~100 m/s * 20 s north, in degrees lat
        lat2 = lat0 + d20s_deg
        key = "weight-adsb"

        # Solved velocity claims 200 m/s due EAST — junk that would drag the
        # estimate east if the filter used it instead of the ADS-B velocity.
        r1 = make_result(lat0, lon0, 1_000, vel_east=200.0)
        track_filter.smooth_solve(r1, key, "abc123")
        r2 = make_result(lat2, lon0, 21_000, vel_east=200.0)
        out2 = track_filter.smooth_solve(r2, key, "abc123")

        assert out2["smoother"] == "kf"
        assert out2["lat"] == pytest.approx(lat2, abs=2e-4)
        assert out2["lon"] == pytest.approx(lon0, abs=1e-3)


class TestRInflation:
    """_measurement_R inflates a cov_en_km2-derived R by _KF_R_INFLATE (a
    sigma multiplier — the covariance matrix scales by _KF_R_INFLATE**2)
    before the min/max clamp; the fallback default R is NOT inflated — it is
    already an empirical end-to-end figure, not a formal measurement
    covariance from the LM fit's Jacobian, so inflating it again would
    double-correct for the same gap.  See the module docstring and the
    _KF_R_INFLATE comment for the staging measurement (2026-08-09: 1/19
    multi-solve records actually smoothed) that made this necessary — an
    un-inflated Jacobian sigma made ordinary innovations look like identity
    breaks to the gate, so the filter was re-anchoring almost every solve."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_cov_derived_r_is_inflated_then_clamped(self):
        # A nominal sigma (1000 m) chosen so that inflated-and-clamped is
        # just "inflated" — 1000 * _KF_R_INFLATE (4.0) = 4000 m, inside
        # [_KF_MIN_POS_SIGMA_M, _KF_MAX_POS_SIGMA_M] = [500, 8000] — so the
        # expected result is a clean, directly-checkable multiplication with
        # the clamp not muddying what's being verified.
        sigma_m = 1000.0
        cov_en_km2 = [[(sigma_m / 1000.0) ** 2, 0.0], [0.0, (sigma_m / 1000.0) ** 2]]
        result = make_result(35.0, -82.0, 1_000, cov_en_km2=cov_en_km2)

        r = track_filter._measurement_R(result)
        actual_sigma = math.sqrt(0.5 * (r[0, 0] + r[1, 1]))

        expected_sigma = sigma_m * track_filter._KF_R_INFLATE
        expected_sigma = min(max(expected_sigma, track_filter._KF_MIN_POS_SIGMA_M),
                              track_filter._KF_MAX_POS_SIGMA_M)
        assert expected_sigma == pytest.approx(4000.0)   # sanity: this test is inside the clamp band
        assert actual_sigma == pytest.approx(expected_sigma)

    def test_fallback_r_is_not_inflated(self):
        # No cov_en_km2 at all -> the isotropic default, untouched by
        # _KF_R_INFLATE (it's already an end-to-end figure, see the module's
        # comment on _KF_DEFAULT_POS_SIGMA_M).
        result = make_result(35.0, -82.0, 1_000)

        r = track_filter._measurement_R(result)
        actual_sigma = math.sqrt(0.5 * (r[0, 0] + r[1, 1]))

        # If this were inflated like the cov-derived path, actual_sigma would
        # be _KF_DEFAULT_POS_SIGMA_M * _KF_R_INFLATE instead — assert the
        # un-inflated value specifically, not just "some value in the band",
        # so an accidental inflate-everything regression would be caught.
        assert actual_sigma == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M)
        # And confirm the assumption the module's comments make: the default
        # already sits inside the clamp band, so this test isn't accidentally
        # passing only because the clamp happens to produce the same number.
        assert track_filter._KF_MIN_POS_SIGMA_M < track_filter._KF_DEFAULT_POS_SIGMA_M < track_filter._KF_MAX_POS_SIGMA_M


class TestKFReducesError:
    """A seeded synthetic constant-velocity track: the filtered RMSE against
    ground truth must beat the raw (unsmoothed) RMSE by at least 20%."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_filtered_rmse_beats_raw_by_20_percent(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        rng = np.random.RandomState(20260809)

        lat0, lon0 = 35.0, -82.0
        v_ms = 120.0
        heading_rad = math.radians(35.0)
        ve_true = v_ms * math.sin(heading_rad)
        vn_true = v_ms * math.cos(heading_rad)
        dt_s = 10.0
        n_steps = 20
        sigma_m = 800.0   # actual injected measurement noise
        # cov_en_km2 reported here is the REALISTIC pre-inflation figure:
        # sigma_m / _KF_R_INFLATE, so that once track_filter applies its
        # inflation the filter's R lands on sigma_m — the true injected
        # noise — rather than overshooting it by another _KF_R_INFLATE.
        # Feeding sigma_m directly as cov_en_km2 (pre-inflation) would leave
        # the filter's R ~16x too large relative to the real noise: an
        # under-confident filter starves its own Kalman gain, which slows
        # velocity convergence on this 120 m/s target enough to eat most of
        # the margin this test checks for.  That miscalibration is exactly
        # what _KF_R_INFLATE exists to correct on real solves (TestRInflation
        # tests the correction itself); a synthetic RMSE check should feed a
        # believable cov_en_km2, not the deliberately-mismatched one this
        # whole feature is a response to.
        reported_sigma_m = sigma_m / track_filter._KF_R_INFLATE
        cov_en_km2 = [[(reported_sigma_m / 1000.0) ** 2, 0.0], [0.0, (reported_sigma_m / 1000.0) ** 2]]

        key = "rmse-key"
        raw_err_sq = []
        filt_err_sq = []
        for i in range(n_steps):
            true_e = ve_true * dt_s * i
            true_n = vn_true * dt_s * i
            noise_e, noise_n = rng.normal(scale=sigma_m, size=2)
            meas_e, meas_n = true_e + noise_e, true_n + noise_n

            lat, lon = offset_latlon_m(lat0, lon0, east_m=meas_e, north_m=meas_n)
            result = make_result(lat, lon, 1_000 + int(i * dt_s * 1000), cov_en_km2=cov_en_km2)
            out = track_filter.smooth_solve(result, key, None)

            raw_err_sq.append((meas_e - true_e) ** 2 + (meas_n - true_n) ** 2)
            if "smoother" in out:
                filt_e, filt_n = track_filter._enu_offset_m(lat0, lon0, out["lat"], out["lon"])
                filt_err_sq.append((filt_e - true_e) ** 2 + (filt_n - true_n) ** 2)
            else:
                filt_err_sq.append(raw_err_sq[-1])   # first solve: no smoothing happened yet

        raw_rmse = math.sqrt(sum(raw_err_sq) / len(raw_err_sq))
        filt_rmse = math.sqrt(sum(filt_err_sq) / len(filt_err_sq))

        assert filt_rmse < raw_rmse * 0.8, f"filtered RMSE {filt_rmse:.1f} vs raw {raw_rmse:.1f}"


class TestStoneSoupOracle:
    """track_filter's internal KF math against a Stone-Soup reference filter.

    Skipped entirely if stonesoup is not installed — nothing else in this
    file imports it or depends on it being present.

    Position-only updates: the measurement sequence carries no vel_east/
    vel_north and no ADS-B entry, so smooth_solve's velocity-update branch
    never fires and the two filters' predict+update chains stay directly
    comparable at every step.

    No cov_en_km2 either — deliberately.  cov_en_km2-derived R gets
    inflated by _KF_R_INFLATE inside track_filter (see _measurement_R), a
    step Stone-Soup's LinearGaussian model here knows nothing about, so
    feeding a covariance would desync the two filters' R immediately and
    break the exact-equality assertions below.  The fallback default R
    (_KF_DEFAULT_POS_SIGMA_M, un-inflated by design) is exactly what both
    sides use when cov_en_km2 is absent, so that is what this test compares.

    The importorskip lives in setup_method, not the class body: a class body
    runs at module-import time (collection), so a bare
    `pytest.importorskip("stonesoup")` statement there would risk skipping
    the WHOLE FILE's collection if stonesoup is absent, not just this class.
    Gating in setup_method means the skip only takes effect when pytest
    actually goes to run a test in this class, leaving every other class in
    the file untouched.
    """

    def setup_method(self):
        pytest.importorskip("stonesoup")
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_matches_stonesoup_kalman_at_every_step(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")

        from stonesoup.models.measurement.linear import LinearGaussian
        from stonesoup.models.transition.linear import (
            CombinedLinearGaussianTransitionModel,
            ConstantVelocity,
        )
        from stonesoup.predictor.kalman import KalmanPredictor
        from stonesoup.types.detection import Detection
        from stonesoup.types.hypothesis import SingleHypothesis
        from stonesoup.types.state import GaussianState
        from stonesoup.updater.kalman import KalmanUpdater

        ref_lat, ref_lon = 35.0, -82.0
        dt_s = 10.0
        n_steps = 15
        ve_true, vn_true = 80.0, -20.0
        # The fallback R — no cov_en_km2 in this sequence, so this is what
        # track_filter actually uses internally (see class docstring).
        r_sigma_m = track_filter._KF_DEFAULT_POS_SIGMA_M
        r_m2 = np.array([[r_sigma_m ** 2, 0.0], [0.0, r_sigma_m ** 2]])

        # A fixed, deterministic wiggle rather than random noise: this test
        # checks the KF arithmetic against an external oracle, not gate
        # robustness, so removing randomness removes any chance a seed trips
        # the innovation gate and desyncs the two filters being compared.
        # Both terms vanish at i=0 — the first measurement lands exactly on
        # the anchor (e=n=0), which is what makes the two filters' ENU
        # frames line up exactly (see the assertion just below the loop).
        def meas_en(i):
            return (
                ve_true * dt_s * i + 40.0 * math.sin(i * 0.7),
                vn_true * dt_s * i + 25.0 * math.sin(i * 0.9),
            )

        key = "oracle-key"
        base_ts_ms = 1_000_000
        entry_after = []
        for i in range(n_steps):
            e_i, n_i = meas_en(i)
            lat, lon = offset_latlon_m(ref_lat, ref_lon, east_m=e_i, north_m=n_i)
            result = make_result(lat, lon, base_ts_ms + int(i * dt_s * 1000))   # no cov_en_km2 -> fallback R
            track_filter.smooth_solve(result, key, None)
            entry = track_filter._KF_TRACKS[key]
            entry_after.append((np.array(entry.x, dtype=float), np.array(entry.P, dtype=float)))

        # First measurement is exactly at the anchor, so track_filter's ENU
        # frame is now EXACTLY (ref_lat, ref_lon) — no flat-earth round-trip
        # slop between the two coordinate systems compared below.
        assert track_filter._KF_TRACKS[key].ref_lat == ref_lat
        assert track_filter._KF_TRACKS[key].ref_lon == ref_lon

        q = track_filter._KF_SIGMA_A_MS2
        cv = ConstantVelocity(q)
        transition_model = CombinedLinearGaussianTransitionModel([cv, cv])
        measurement_model = LinearGaussian(ndim_state=4, mapping=(0, 2), noise_covar=r_m2)
        predictor = KalmanPredictor(transition_model)
        updater = KalmanUpdater(measurement_model)

        t0 = datetime.datetime(2026, 1, 1)
        # Prior at i=0 equals track_filter's own post-init state exactly —
        # same init rule (x=[0,0,0,0], P_pos=R, P_vel=_KF_INIT_VEL_SIGMA^2),
        # and there is no vel measurement in this sequence for either filter
        # to have applied since.
        x0, p0 = entry_after[0]
        prior = GaussianState(state_vector=x0, covar=p0, timestamp=t0)

        for i in range(1, n_steps):
            ts_i = t0 + datetime.timedelta(seconds=dt_s * i)
            e_i, n_i = meas_en(i)
            detection = Detection(state_vector=np.array([e_i, n_i]), timestamp=ts_i)
            prediction = predictor.predict(prior, timestamp=ts_i)
            hypothesis = SingleHypothesis(prediction, detection)
            post = updater.update(hypothesis)

            oracle_x = np.asarray(post.state_vector, dtype=float).flatten()
            oracle_p = np.asarray(post.covar, dtype=float)

            tf_x, tf_p = entry_after[i]
            assert np.allclose(oracle_x, tf_x, atol=1e-6), f"step {i} mean mismatch"
            assert np.allclose(oracle_p, tf_p, atol=1e-6), f"step {i} covar mismatch"

            prior = post


class TestEnuRoundTrip:
    """_enu_offset_m must be the exact algebraic inverse of
    services.geo.offset_latlon_m — the KF's measurement model depends on
    that being true to float precision, not just approximately true."""

    @pytest.mark.parametrize("ref_lat,ref_lon,east_m,north_m", [
        (35.0, -82.0, 0.0, 0.0),
        (35.0, -82.0, 1234.5, -876.3),
        (0.0, 0.0, 50_000.0, -50_000.0),
        (65.0, 178.0, -12_000.0, 30_000.0),
        (-33.9, 151.2, 8_432.1, -2_001.7),
    ])
    def test_round_trip_within_1e9_deg(self, ref_lat, ref_lon, east_m, north_m):
        lat, lon = offset_latlon_m(ref_lat, ref_lon, east_m=east_m, north_m=north_m)
        back_e, back_n = track_filter._enu_offset_m(ref_lat, ref_lon, lat, lon)
        lat2, lon2 = offset_latlon_m(ref_lat, ref_lon, east_m=back_e, north_m=back_n)

        assert abs(lat2 - lat) < 1e-9
        assert abs(lon2 - lon) < 1e-9
