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
from services.geo import in_node_beam  # noqa: E402
from services.tasks import solver as solver_mod  # noqa: E402

# An n=2 solver input whose track pairing has already passed the
# constant-velocity fit.  At n=2 a solve is published only once the pairing has
# justified itself — the residual gates cannot discriminate there, the solver
# having 5 unknowns against 4 residuals — so a bare {"n_nodes": 2} is now
# withheld and every test expecting publication has to say which case it is
# exercising.  TestN2ConfirmationGate covers the unconfirmed side.
_CONFIRMED_N2 = {"n_nodes": 2, "chi2_per_dof": 0.5, "n_epochs": 8}


def _reset_state():
    state.task_error_counts.clear()
    state.solver_failures = 0
    state.solver_successes = 0
    state.solver_total_solved = 0
    state.solver_total_latency_s = 0.0
    state.solver_last_latency_s = 0.0
    state.n2_unconfirmed = 0
    state.solver_stale_drops = 0
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

        item = (_CONFIRMED_N2, {}, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is not None
        assert state.solver_successes == 1
        assert state.solver_total_solved == 1
        assert state.solver_last_latency_s >= 0
        assert "solver" in state.task_last_success
        assert any(k.startswith("mn-dark-1000-") for k in state.multinode_tracks)
        # A dark solve contributes no coverage — see TestCoverageCalibration.
        assert stub.calibration_calls == []

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

        item = (_CONFIRMED_N2, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert state.solver_successes == 0
        assert not state.multinode_tracks
        # Unconverged solves used to vanish uncounted — staging showed
        # hundreds with no observable reason.  They are failures.
        assert state.solver_failures == 1
        assert state.solver_fail_unconverged == 1

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

        # enqueued 35 seconds in the past → latency > 30 triggers alert
        # (must be < _SOLVER_MAX_QUEUE_AGE_S = 45s so the item is not discarded)
        item = ({"n_nodes": 4}, {}, time.time() - 35.0)
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
        item = (_CONFIRMED_N2, {})
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

        item = (_CONFIRMED_N2, {}, time.time())
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

        item = (_CONFIRMED_N2, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert any(k.startswith("mn-dark-6000-") for k in state.multinode_tracks)
        assert state.solver_successes == 1

    def test_high_rms_doppler_rejected(self, monkeypatch):
        """Results with rms_doppler > _SOLVER_RMS_DOPPLER_MAX_HZ must not be stored.

        Mirrors the 3-node false-association case observed in production:
        rms_delay=1.233 µs (passes delay filter) but rms_doppler=248 Hz
        (physically unrealisable for FM illuminator ⇒ false association).
        """
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 32.97,
                "lon": -96.83,
                "alt_m": 3000.0,
                "rms_delay": 1.2,       # passes delay threshold
                "rms_doppler": 248.87,  # physically impossible (> 196 Hz FM max)
                "timestamp_ms": 7000,
                "contributing_node_ids": ["n1", "n2", "n3"],
                "n_nodes": 3,
            }

        item = ({"n_nodes": 3}, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert not state.multinode_tracks, "false association must not be stored"
        assert state.solver_successes == 0
        assert state.solver_failures == 1

    def test_low_rms_doppler_accepted(self, monkeypatch):
        """Results with rms_doppler below threshold are stored normally."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 32.97,
                "lon": -96.83,
                "alt_m": 9000.0,
                "rms_delay": 0.8,
                "rms_doppler": 12.5,    # well within FM physics
                "timestamp_ms": 8000,
                "contributing_node_ids": ["n1", "n2", "n3"],
                "n_nodes": 3,
            }

        item = ({"n_nodes": 3}, {}, time.time())
        solver_mod._process_solver_item(item, solve_fn)

        assert any(k.startswith("mn-dark-8000-") for k in state.multinode_tracks)
        assert state.solver_successes == 1


class TestStaleItemSkip:
    """Items that have been waiting too long in the queue must be discarded."""

    def test_stale_item_is_skipped(self, monkeypatch):
        """Item enqueued > _SOLVER_MAX_QUEUE_AGE_S seconds ago is dropped without solving."""
        _reset_state()
        solve_called = []

        def solve_fn(s_in, cfgs):
            solve_called.append(True)
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "rms_delay": 0.5,
                "timestamp_ms": 1000,
                "contributing_node_ids": ["n1", "n2"],
                "n_nodes": 2,
            }

        old_enqueued_at = time.time() - (solver_mod._SOLVER_MAX_QUEUE_AGE_S + 1.0)
        item = (_CONFIRMED_N2, {}, old_enqueued_at)
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is None
        assert not solve_called, "solver must not be invoked for stale items"
        assert state.solver_successes == 0
        assert state.solver_failures == 0
        # Observable as its own counter, not a failure: the queue-full and
        # drain-too-slow modes are distinct and this is the only trace of the
        # latter (the per-item reason is DEBUG, which staging does not emit).
        assert state.solver_stale_drops == 1
        assert not state.multinode_tracks

    def test_fresh_item_is_solved(self, monkeypatch):
        """Item enqueued just now must be passed to the solver normally."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "rms_delay": 0.5,
                "timestamp_ms": 9000,
                "contributing_node_ids": ["n1", "n2"],
                "n_nodes": 2,
            }

        item = (_CONFIRMED_N2, {}, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is not None and result.get("success")
        assert state.solver_successes == 1


class TestSolveBestAltitude:
    """Altitude-sweep helpers: n_nodes >= 3 uses a layer sweep, n_nodes = 2 uses initial_guess directly."""

    def test_n3_picks_minimum_rms_altitude(self, monkeypatch):
        """For n_nodes=3, _process_solver_item tries all altitude layers and picks best."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        calls: list[float] = []

        def solve_fn(s_in, cfgs):
            alt = s_in["initial_guess"]["alt_km"]
            calls.append(alt)
            # Simulate: 9 km layer gives best rms_delay; others give poor rms
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

        # Initial_guess alt_km=3.0 is already in the fixed layers [1.5, 3, 5, 7, 9, 11].
        # All layers tried: [1.5, 3, 5, 7, 9, 11] km
        assert set(calls) == {1.5, 3.0, 5.0, 7.0, 9.0, 11.0}
        # Best result (rms=0.1 at 9 km) selected
        assert result is not None
        assert result["alt_m"] == pytest.approx(9000.0)
        assert state.solver_successes == 1

    def test_n2_uses_initial_guess_altitude_directly(self, monkeypatch):
        """For n_nodes=2, solver is called once with initial_guess.alt_km.

        rms_delay≈0 and rms_doppler≈0 at every altitude layer for n=2 (exactly
        determined delay system; underdetermined velocity system).  Neither metric
        discriminates altitude.  The initial_guess.alt_km from association.py is
        the weighted-mean altitude from the association grid (delay-residual
        weighting; ties fall back to the ≈7.5 km grid mean) and is used directly.
        """
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        calls: list[float] = []

        def solve_fn(s_in, cfgs):
            alt = s_in["initial_guess"]["alt_km"]
            calls.append(alt)
            return {
                "success": True,
                "lat": 37.5,
                "lon": -122.1,
                "alt_m": alt * 1000,
                "rms_delay": 0.0,
                "rms_doppler": 0.0,
                "timestamp_ms": 8000,
                "contributing_node_ids": ["n1", "n2"],
                "n_nodes": 2,
            }

        s_in = {
            **_CONFIRMED_N2,
            "initial_guess": {"lat": 37.5, "lon": -122.1, "alt_km": 7.5},
            "measurements": [],
        }
        item = (s_in, {}, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        # Exactly one solver call, at the initial_guess altitude
        assert calls == [7.5]
        assert result is not None
        assert result["alt_m"] == pytest.approx(7500.0)
        assert state.solver_successes == 1


class TestBeamCoverageFilter:
    """Solver results outside a contributing node's beam must be rejected."""

    def _node_cfg(self, rx_lat, rx_lon, beam_az, beam_w=41.0, max_range=50.0):
        return {
            "rx_lat": rx_lat,
            "rx_lon": rx_lon,
            "tx_lat": rx_lat,
            "tx_lon": rx_lon,
            "beam_azimuth_deg": beam_az,
            "beam_width_deg": beam_w,
            "max_range_km": max_range,
        }

    def test_ghost_outside_beam_rejected(self, monkeypatch):
        """Result whose lat/lon falls outside a contributing node's beam is discarded."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        # Node at (40, -74) pointing North (az=0, width=41°).
        # A result at (40, -74.5) is due West — ~30° from North, outside the ±20.5° beam.
        node_cfgs = {"n1": self._node_cfg(40.0, -74.0, beam_az=0.0)}

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 40.0,
                "lon": -74.5,  # due West — outside beam
                "rms_delay": 0.0,
                "rms_doppler": 0.0,
                "timestamp_ms": 9001,
                "contributing_node_ids": ["n1"],
                "n_nodes": 2,
            }

        s_in = {"n_nodes": 2, "initial_guess": {"lat": 40.0, "lon": -74.0, "alt_km": 9.0}}
        item = (s_in, node_cfgs, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is None, "ghost outside beam must be rejected"
        assert not state.multinode_tracks
        assert state.solver_failures == 1
        assert state.solver_successes == 0

    def test_result_inside_beam_accepted(self, monkeypatch):
        """Result inside the node's beam is accepted normally."""
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)

        # Node at (40, -74) pointing North (az=0, width=41°).
        # A result at (40.3, -74.0) is due North — inside the beam.
        node_cfgs = {"n1": self._node_cfg(40.0, -74.0, beam_az=0.0)}

        def solve_fn(s_in, cfgs):
            return {
                "success": True,
                "lat": 40.3,
                "lon": -74.0,  # due North — inside beam
                "rms_delay": 0.0,
                "rms_doppler": 0.0,
                "timestamp_ms": 9002,
                "contributing_node_ids": ["n1"],
                "n_nodes": 2,
            }

        s_in = {**_CONFIRMED_N2,
                "initial_guess": {"lat": 40.3, "lon": -74.0, "alt_km": 9.0}}
        item = (s_in, node_cfgs, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is not None
        assert any(k.startswith("mn-dark-9002-") for k in state.multinode_tracks)
        assert state.solver_successes == 1
        assert state.solver_failures == 0


# ── _in_node_beam ─────────────────────────────────────────────────────────────


class TestInNodeBeam:
    """Test uncovered branches of in_node_beam: TX-derived azimuth and no-beam
    fallback.

    in_node_beam lives in services.geo, not this module — the solver's own
    beam gate stopped calling it when the gate was split into separately
    n=2/every-N range and bearing rules (see _process_solver_item), but the
    function itself is still used elsewhere (e.g. track_gates.py), so these
    branches are exercised directly against services.geo here.
    """

    def test_tx_lat_lon_derives_beam_azimuth_aircraft_outside(self):
        """TX-lat/lon branch: beam_az = bearing(RX→TX)+90; aircraft clearly outside."""
        # RX=(0,0), TX=(1,1) NE → bearing≈45° → beam_az≈135° (SE)
        # Aircraft at (0.1,-0.1) NW (~15 km, in range) → bearing≈315° → 180° off boresight
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0, "tx_lat": 1.0, "tx_lon": 1.0, "max_range_km": 100}
        assert in_node_beam(0.1, -0.1, cfg) is False

    def test_tx_lat_lon_derives_beam_azimuth_aircraft_inside(self):
        """TX-lat/lon branch: aircraft in the derived beam direction."""
        # RX=(0,0), TX=(1,1) NE → beam_az≈135° (SE)
        # Aircraft at (-0.1,0.1) SE (~15 km, in range) → bearing≈135° → 0° off boresight
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0, "tx_lat": 1.0, "tx_lon": 1.0, "max_range_km": 100}
        assert in_node_beam(-0.1, 0.1, cfg) is True

    def test_no_beam_direction_returns_true_within_range(self):
        """No beam_azimuth_deg and no tx_lat/tx_lon → beam_az=None → always True."""
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0}
        assert in_node_beam(0.1, 0.0, cfg) is True

    def test_out_of_range_returns_false_regardless_of_beam(self):
        """Haversine check fires before beam check; beyond max_range → False."""
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0, "max_range_km": 10.0}
        # ~111 km away, well outside 10 km range
        assert in_node_beam(1.0, 0.0, cfg) is False


# ── _sweep_altitudes ──────────────────────────────────────────────────────────


class TestSweepAltitudes:
    """Test the altitude-sweep exception handling paths."""

    def test_all_altitudes_raise_reraises_last_exception(self):
        """If every altitude layer raises, the last exception propagates."""
        calls = []

        def bad_solve(s, cfgs):
            calls.append(s["initial_guess"]["alt_km"])
            raise ValueError(f"fail at {s['initial_guess']['alt_km']}")

        s_in = {"initial_guess": {"lat": 0.0, "lon": 0.0}}
        with pytest.raises(ValueError, match=r"fail at 2\.0"):
            solver_mod._sweep_altitudes(s_in, {}, bad_solve, [1.0, 2.0], "rms_delay")
        assert len(calls) == 2

    def test_one_fails_one_succeeds_returns_good_result(self):
        """An exception on one layer is swallowed; a successful layer wins."""
        calls: list[float] = []

        def mixed_solve(s, cfgs):
            calls.append(s["initial_guess"]["alt_km"])
            if s["initial_guess"]["alt_km"] == 1.0:
                raise ValueError("bad layer")
            return {"success": True, "rms_delay": 0.005}

        s_in = {"initial_guess": {"lat": 0.0, "lon": 0.0}}
        result = solver_mod._sweep_altitudes(s_in, {}, mixed_solve, [1.0, 2.0], "rms_delay")
        assert result is not None
        assert result["success"] is True
        assert 2.0 in calls

    def test_no_successful_result_and_no_exception_returns_none(self):
        """solve_fn returns None every time → returns None without raising."""
        def null_solve(s, cfgs):
            return None

        s_in = {"initial_guess": {"lat": 0.0, "lon": 0.0}}
        result = solver_mod._sweep_altitudes(s_in, {}, null_solve, [1.0, 2.0], "rms_delay")
        assert result is None


# ── _solve_best_altitude (direct) ─────────────────────────────────────────────


class TestSolveBestAltitudeDirect:
    """Test that ADS-B altitude injection adds a novel layer to the sweep."""

    def test_adsb_altitude_outside_layers_is_included_in_sweep(self):
        """When ig_alt is not in _SOLVER_ALT_LAYERS_KM, it must be tried."""
        novel_alt = 7.777
        assert novel_alt not in solver_mod._SOLVER_ALT_LAYERS_KM

        tried = []

        def track_solve(s, cfgs):
            tried.append(s["initial_guess"]["alt_km"])
            return None

        s_in = {"initial_guess": {"lat": 0.0, "lon": 0.0, "alt_km": novel_alt}}
        solver_mod._solve_best_altitude(s_in, {}, track_solve)
        assert novel_alt in tried

    def test_adsb_altitude_already_in_layers_not_duplicated(self):
        """When ig_alt is already in _SOLVER_ALT_LAYERS_KM, layers list is unchanged."""
        existing_alt = solver_mod._SOLVER_ALT_LAYERS_KM[0]
        tried = []

        def track_solve(s, cfgs):
            tried.append(s["initial_guess"]["alt_km"])
            return None

        s_in = {"initial_guess": {"lat": 0.0, "lon": 0.0, "alt_km": existing_alt}}
        solver_mod._solve_best_altitude(s_in, {}, track_solve)
        assert tried.count(existing_alt) == 1  # not duplicated
        assert len(tried) == len(solver_mod._SOLVER_ALT_LAYERS_KM)



class TestN2ConfirmationGate:
    """n=2 is published only once its track pairing has justified itself.

    Exercised with the gate forced on.  It ships disabled — see
    N2_TRACK_ASSOCIATION — because the fit it depends on cannot yet run on the
    frame path, and with no chi2 being produced a live gate would withhold
    every n=2 track rather than the false ones.

    This is the one place a false n=2 pairing can be stopped.  The residual
    gates above cannot: the solver fits [x, y, vx, vy, vz] with altitude pinned,
    5 unknowns against 4 residuals, so a cross pairing between two real aircraft
    drives rms_delay and rms_doppler to ~0 exactly as a real target does.
    """

    @pytest.fixture(autouse=True)
    def _gate_on(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_N2_REQUIRE_CONFIRMED", True)

    @staticmethod
    def _solve_fn(s_in, cfgs):
        return {
            "success": True,
            "lat": 37.5,
            "lon": -122.1,
            "alt_m": 8000.0,
            "rms_delay": 0.4,
            "rms_doppler": 3.0,
            "timestamp_ms": 7100,
            "contributing_node_ids": ["n1", "n2"],
            "n_nodes": 2,
        }

    def test_confirmed_pairing_is_published(self, monkeypatch):
        _reset_state()
        monkeypatch.setattr(state, "node_analytics", _StubAnalytics())
        solver_mod._process_solver_item((_CONFIRMED_N2, {}, time.time()), self._solve_fn)

        assert any(k.startswith("mn-dark-7100-") for k in state.multinode_tracks)
        assert state.solver_successes == 1
        assert state.n2_unconfirmed == 0

    def test_failing_chi2_is_withheld(self, monkeypatch):
        _reset_state()
        monkeypatch.setattr(state, "node_analytics", _StubAnalytics())
        s_in = {"n_nodes": 2, "chi2_per_dof": 40.0, "n_epochs": 8}
        result = solver_mod._process_solver_item((s_in, {}, time.time()), self._solve_fn)

        assert not state.multinode_tracks
        assert state.n2_unconfirmed == 1
        # Not a failure — the solve worked, it just has not earned publication.
        assert state.solver_failures == 0
        # The fix itself is still returned, so the display keeps its position.
        assert result is not None and result["lat"] == 37.5

    def test_unfitted_pairing_is_withheld(self, monkeypatch):
        """A pairing with too short an observation span is not yet evidence.

        Association re-tests it every round, so a real target is published as
        soon as it has the history to justify itself rather than being dropped.
        """
        _reset_state()
        monkeypatch.setattr(state, "node_analytics", _StubAnalytics())
        s_in = {"n_nodes": 2, "chi2_per_dof": None, "n_epochs": 2}
        solver_mod._process_solver_item((s_in, {}, time.time()), self._solve_fn)

        assert not state.multinode_tracks
        assert state.n2_unconfirmed == 1

    def test_n3_is_unaffected(self, monkeypatch):
        """n>=3 is overdetermined, so its residual gates already work."""
        _reset_state()
        monkeypatch.setattr(state, "node_analytics", _StubAnalytics())

        def solve_fn(s_in, cfgs):
            out = self._solve_fn(s_in, cfgs)
            out["n_nodes"] = 3
            out["contributing_node_ids"] = ["n1", "n2", "n3"]
            return out

        # No chi2 at all — an n=3 pairing must not need one.
        solver_mod._process_solver_item(({"n_nodes": 3}, {}, time.time()), solve_fn)

        assert any(k.startswith("mn-dark-7100-") for k in state.multinode_tracks)
        assert state.solver_successes == 1
        assert state.n2_unconfirmed == 0


class TestCoverageCalibration:
    """Publish-path calibration is banned — regression coverage.

    Attribution here rides on the very association the coverage polygon is
    used to judge: under an active FOV gate a ghost publish (wrong tracklet
    pairing tagged with a real hex) recorded positives for BOTH contributing
    nodes at another aircraft's real position, opening bins, widening the
    gate, and producing more ghosts.  Staging 2026-08-09: ghost precision
    25%, 15/29 synthetic nodes with out-of-wedge bins within ~25 min of the
    active flip.  A published multinode solve must now record NOTHING here
    — the frame path (services/track_gates.py) is the only calibration
    writer, gated on an actual fresh detection.
    """

    @staticmethod
    def _solve_fn(s_in, cfgs):
        return {
            "success": True,
            "lat": 37.5, "lon": -122.1,          # deliberately far from the ADS-B fix
            "rms_delay": 0.4, "rms_doppler": 3.0,
            "timestamp_ms": 8000,
            "contributing_node_ids": ["n1", "n2"],
            "n_nodes": 2,
        }

    def _run(self, monkeypatch, adsb_entry, hex_id="abc123"):
        _reset_state()
        stub = _StubAnalytics()
        monkeypatch.setattr(state, "node_analytics", stub)
        state.adsb_aircraft.clear()
        if adsb_entry is not None:
            state.adsb_aircraft[hex_id] = adsb_entry
        s_in = {**_CONFIRMED_N2, "adsb_hex": hex_id if adsb_entry else None}
        solver_mod._process_solver_item((s_in, {}, time.time()), self._solve_fn)
        return stub

    def test_published_solve_with_a_fresh_adsb_match_records_no_calibration(self, monkeypatch):
        """The regression: this used to record one point per contributing
        node at the ADS-B position.  It must now record none."""
        stub = self._run(monkeypatch, {
            "lat": 34.88, "lon": -82.35,
            "last_seen_ms": time.time() * 1000,
        })
        assert stub.calibration_calls == []

    def test_dark_solve_records_nothing(self, monkeypatch):
        assert self._run(monkeypatch, None).calibration_calls == []

    def test_stale_adsb_fix_still_records_nothing(self, monkeypatch):
        stub = self._run(monkeypatch, {
            "lat": 34.88, "lon": -82.35,
            "last_seen_ms": (time.time() - 60.0) * 1000,
        })
        assert stub.calibration_calls == []

    def test_null_island_adsb_still_records_nothing(self, monkeypatch):
        stub = self._run(monkeypatch, {
            "lat": 0, "lon": 0, "last_seen_ms": time.time() * 1000,
        })
        assert stub.calibration_calls == []


class TestN2GateShipsDisabled:
    """With the track path parked, an n=2 solve must still publish.

    The gate reads chi2_per_dof off the solver input, and the detection-level
    association path does not produce one.  Leaving the gate live while that
    path is in use would withhold every n=2 track — silently, since the solve
    itself succeeds — so the two have to move together.
    """

    def test_flag_is_off_by_default(self):
        from config.constants import N2_TRACK_ASSOCIATION
        assert solver_mod._N2_REQUIRE_CONFIRMED is N2_TRACK_ASSOCIATION

    def test_unfitted_n2_publishes_when_the_gate_is_off(self, monkeypatch):
        _reset_state()
        monkeypatch.setattr(state, "node_analytics", _StubAnalytics())
        monkeypatch.setattr(solver_mod, "_N2_REQUIRE_CONFIRMED", False)

        def solve_fn(s_in, cfgs):
            return {
                "success": True, "lat": 37.5, "lon": -122.1, "alt_m": 8000.0,
                "rms_delay": 0.4, "rms_doppler": 3.0, "timestamp_ms": 7300,
                "contributing_node_ids": ["n1", "n2"], "n_nodes": 2,
            }

        # No chi2 at all — the shape the detection path emits.
        solver_mod._process_solver_item(({"n_nodes": 2}, {}, time.time()), solve_fn)
        assert any(k.startswith("mn-dark-7300-") for k in state.multinode_tracks)
        assert state.n2_unconfirmed == 0


class TestFitRunsOnThisSideOfTheQueue:
    """Association hands over the epochs; the fit happens on the solver worker.

    An ~86 ms LM solve on the frame path is frame latency — measured on staging
    at 92% frame-queue depth with the processor 21 s behind a 6 frame/s feed.
    This worker already has threads and a queue with a staleness drop, which is
    where that cost belongs.
    """

    @staticmethod
    def _solve_fn(s_in, cfgs):
        return {
            "success": True, "lat": 34.88, "lon": -82.35, "alt_m": 7000.0,
            "rms_delay": 0.4, "rms_doppler": 3.0, "timestamp_ms": 9100,
            "contributing_node_ids": ["n1", "n2"], "n_nodes": 2,
        }

    def test_deferred_fit_is_run_here(self, monkeypatch):
        called = {}

        def fake_fit(fit_input, cfgs):
            called["epochs"] = len(fit_input["epochs"])
            return {"success": True, "chi2_per_dof": 0.4, "n_epochs": 6,
                    "dof": 18, "lat": 34.88, "lon": -82.35, "alt_m": 7000.0,
                    "vel_east": 180.0, "vel_north": -90.0}

        import retina_geolocator.multinode_solver as mns
        monkeypatch.setattr(mns, "fit_constant_velocity", fake_fit)

        s_in = {"n_nodes": 2, "chi2_per_dof": None,
                "cv_epochs": [{"t_s": float(i), "measurements": []} for i in range(6)],
                "initial_guess": {"lat": 34.88, "lon": -82.35, "alt_km": 7.0}}
        assert solver_mod._resolve_n2_chi2(s_in, {"n1": {}, "n2": {}}) == 0.4
        assert called["epochs"] == 6
        # Cached, so a retry of the same item does not refit.
        assert s_in["chi2_per_dof"] == 0.4

    def test_an_already_fitted_input_is_not_refitted(self, monkeypatch):
        import retina_geolocator.multinode_solver as mns

        def boom(*a, **kw):
            raise AssertionError("must not refit")

        monkeypatch.setattr(mns, "fit_constant_velocity", boom)
        assert solver_mod._resolve_n2_chi2({"chi2_per_dof": 1.2}, {}) == 1.2

    def test_no_epochs_means_no_confirmation(self):
        """The detection-level path supplies neither, and must not be confirmed."""
        assert solver_mod._resolve_n2_chi2({"n_nodes": 2}, {}) is None


class TestTrackPairExclusivity:
    """Two pairings sharing a single-node track are mutually exclusive.

    One track is one aircraft.  The chi2 gate alone cannot separate them when
    both clear it — which is precisely the case that produces a ghost beside a
    real target — so the better fit takes the tracks and the loser is withheld.
    """

    def setup_method(self):
        solver_mod._TRACK_CLAIMS.clear()

    def test_better_fit_wins_a_shared_track(self):
        good = {"track_pair_ids": [("a1", "b1")]}
        worse = {"track_pair_ids": [("a1", "b2")]}   # shares a1
        assert solver_mod._claim_track_pair(good, 0.4) is True
        assert solver_mod._claim_track_pair(worse, 3.0) is False

    def test_a_worse_incumbent_is_displaced(self):
        assert solver_mod._claim_track_pair({"track_pair_ids": [("a1", "b2")]}, 3.0) is True
        assert solver_mod._claim_track_pair({"track_pair_ids": [("a1", "b1")]}, 0.4) is True

    def test_a_pairing_keeps_its_own_claim_when_re_solved(self):
        """Re-solving must not lock a track out with its own previous score."""
        s_in = {"track_pair_ids": [("a1", "b1")]}
        assert solver_mod._claim_track_pair(s_in, 0.4) is True
        assert solver_mod._claim_track_pair(s_in, 0.4) is True

    def test_disjoint_pairings_both_claim(self):
        assert solver_mod._claim_track_pair({"track_pair_ids": [("a1", "b1")]}, 0.4) is True
        assert solver_mod._claim_track_pair({"track_pair_ids": [("a2", "b2")]}, 0.5) is True

    def test_claims_expire(self, monkeypatch):
        """A claim must not outlive the track it was made for."""
        assert solver_mod._claim_track_pair({"track_pair_ids": [("a1", "b1")]}, 0.4) is True
        real_time = time.time
        monkeypatch.setattr(
            solver_mod.time, "time",
            lambda: real_time() + solver_mod._TRACK_CLAIM_TTL_S + 1,
        )
        assert solver_mod._claim_track_pair({"track_pair_ids": [("a1", "b9")]}, 5.0) is True

    def test_detection_level_input_is_unaffected(self):
        """No track ids means nothing to arbitrate."""
        assert solver_mod._claim_track_pair({"n_nodes": 2}, 9.9) is True


class TestCollectTrackAnomalies:
    """Contributing-track anomaly flags must be stamped onto multinode results
    (and latched across solves at the write site) — before this, multinode
    entries hardcoded is_anomalous False and a dark anomalous target went
    quiet the moment it was solved."""

    def _pipeline(self, node_id, tracks):
        import types
        return types.SimpleNamespace(
            config={"node_id": node_id},
            tracker=types.SimpleNamespace(tracks=tracks),
        )

    def _track(self, track_id, anomaly_types=(), is_anomalous=False, max_vel=0.0):
        import types
        return types.SimpleNamespace(
            track_id=track_id,
            anomaly_types=set(anomaly_types),
            is_anomalous=is_anomalous,
            max_velocity_ms=max_vel,
        )

    def test_dark_solve_allowlisted(self):
        state.node_pipelines["na"] = self._pipeline("na", [
            self._track("t1", {"supersonic", "sustained_orbit"}, True, 420.0),
        ])
        try:
            result = {"contributing_node_ids": ["na"]}
            s_in = {"track_ids": ["t1", "t2"]}
            solver_mod._collect_track_anomalies(s_in, result)
            # No adsb_hex → dark: only the physically loud types survive.
            assert result["anomaly_types"] == ["supersonic"]
            assert result["is_anomalous"] is True
        finally:
            state.node_pipelines.pop("na", None)

    def test_dark_solve_disallowed_types_unflag(self):
        state.node_pipelines["na"] = self._pipeline("na", [
            self._track("t1", {"sustained_orbit"}, True),
        ])
        try:
            result = {"contributing_node_ids": ["na"]}
            solver_mod._collect_track_anomalies({"track_ids": ["t1"]}, result)
            assert result["anomaly_types"] == []
            assert result["is_anomalous"] is False
        finally:
            state.node_pipelines.pop("na", None)

    def test_adsb_solve_keeps_all_types(self):
        state.node_pipelines["na"] = self._pipeline("na", [
            self._track("t1", {"identity_swap"}, True),
        ])
        try:
            result = {"contributing_node_ids": ["na"], "adsb_hex": "abc123"}
            solver_mod._collect_track_anomalies({"track_ids": ["t1"]}, result)
            assert result["anomaly_types"] == ["identity_swap"]
            assert result["is_anomalous"] is True
        finally:
            state.node_pipelines.pop("na", None)

    def test_unrelated_tracks_ignored(self):
        state.node_pipelines["na"] = self._pipeline("na", [
            self._track("other", {"supersonic"}, True),
        ])
        try:
            result = {"contributing_node_ids": ["na"]}
            solver_mod._collect_track_anomalies({"track_ids": ["t1"]}, result)
            assert result["is_anomalous"] is False
        finally:
            state.node_pipelines.pop("na", None)

    def test_flag_latches_across_solves(self, monkeypatch):
        """The write site ORs with the previous entry: a tracker flag raised
        on one solve holds for the multinode track's lifetime even after the
        contributing track goes quiet."""
        _reset_state()
        monkeypatch.setattr(state, "node_analytics", _StubAnalytics())
        state.node_pipelines["na"] = self._pipeline("na", [
            self._track("t1", {"supersonic"}, True, 400.0),
        ])

        def solve_fn(s_in, cfgs):
            return {
                "success": True, "lat": 37.5, "lon": -122.1,
                "timestamp_ms": 1000, "contributing_node_ids": ["na"],
            }

        try:
            item = ({**_CONFIRMED_N2, "track_ids": ["t1"]}, {}, time.time())
            solver_mod._process_solver_item(item, solve_fn)
            key = next(iter(state.multinode_tracks))
            assert state.multinode_tracks[key]["is_anomalous"] is True

            # Second solve: the contributing track is now clean, but the
            # multinode track keeps its flag.
            state.node_pipelines["na"] = self._pipeline("na", [
                self._track("t1", set(), False),
            ])
            solver_mod._process_solver_item(item, solve_fn)
            assert state.multinode_tracks[key]["is_anomalous"] is True
            assert "supersonic" in state.multinode_tracks[key]["anomaly_types"]
        finally:
            state.node_pipelines.pop("na", None)
            _reset_state()


def _marker_fn(x):
    return {"marker": x}


class TestSolverProcessPool:
    """The pure LM compute ships to child processes; everything else stays put.

    The pool exists only after start_solver_workers, so tests and the offline
    bench run the same helpers inline.  A broken pool must degrade to inline
    solving, not to dropped items.
    """

    def test_pool_call_is_inline_without_a_pool(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_solver_pool", None)
        assert solver_mod._pool_call(_marker_fn, 7) == {"marker": 7}

    def test_round_trip_through_a_real_spawn_pool(self, monkeypatch):
        """Spawn a real pool once: proves the child can import the lib and the
        submitted functions/arguments survive pickling in this image."""
        from retina_geolocator.multinode_solver import solve_multinode

        pool = solver_mod._make_solver_pool()
        try:
            monkeypatch.setattr(solver_mod, "_solver_pool", pool)
            # <2 measurements short-circuits to None inside the child — the
            # assertion is about transport, not solving.
            noop = {"initial_guess": {"lat": 0.0, "lon": 0.0}, "measurements": []}
            assert solver_mod._pool_call(solve_multinode, noop, {}) is None
        finally:
            pool.shutdown(wait=True)

    def test_broken_pool_falls_back_inline_and_recreates(self, monkeypatch):
        from concurrent.futures.process import BrokenProcessPool

        events = []

        class _BrokenPool:
            def submit(self, fn, *args):
                raise BrokenProcessPool("child died")

            def shutdown(self, wait=False):
                events.append("shutdown")

        replacement = object()
        broken = _BrokenPool()
        monkeypatch.setattr(solver_mod, "_solver_pool", broken)
        monkeypatch.setattr(solver_mod, "_make_solver_pool", lambda: replacement)

        assert solver_mod._pool_call(_marker_fn, 9) == {"marker": 9}
        assert events == ["shutdown"]
        assert solver_mod._solver_pool is replacement
        monkeypatch.setattr(solver_mod, "_solver_pool", None)


class TestDarkSolveSmoothing:
    """Dark solves accumulate EWMA history under their track key.

    The smoother used to require an ADS-B hex AND a live ADS-B entry — dark
    targets, the one population whose only position source is MLAT, got raw
    single-frame solves.  History is now keyed by the multinode track key and
    dead-reckoned with the solved velocity when there is no ADS-B.

    This class documents the legacy EWMA path (TRACK_SMOOTHER=ewma); the
    default KF smoother is covered in test_track_filter.py.
    """

    # A moving dark target: 100 m/s due north, solves 20 s apart.  20 s of
    # motion is ~0.017965° of latitude.
    LAT1, LON = 35.0, -82.0
    _D20S_DEG = 2000.0 / 111_320.0

    def setup_method(self):
        _reset_state()
        solver_mod._reset_for_tests()
        state.adsb_aircraft.clear()
        os.environ["TRACK_SMOOTHER"] = "ewma"

    def teardown_method(self):
        solver_mod._reset_for_tests()
        state.adsb_aircraft.clear()
        os.environ.pop("TRACK_SMOOTHER", None)

    def _solve_fn(self, lat, lon, ts_ms, vel_east=0.0, vel_north=0.0):
        def fn(s_in, cfgs):
            return {
                "success": True,
                "lat": lat,
                "lon": lon,
                "timestamp_ms": ts_ms,
                "vel_east": vel_east,
                "vel_north": vel_north,
                "contributing_node_ids": ["n1", "n2"],
            }
        return fn

    def _stored(self):
        assert len(state.multinode_tracks) == 1, (
            f"expected one associated track, got {list(state.multinode_tracks)}"
        )
        return next(iter(state.multinode_tracks.values()))

    def test_moving_dark_target_is_dead_reckoned_not_lagged(self):
        """With the solved velocity matching the motion, the DR'd history
        lands on the current position — the average tracks the target instead
        of trailing at the midpoint."""
        lat2 = self.LAT1 + self._D20S_DEG
        item = (dict(_CONFIRMED_N2), {}, time.time())
        solver_mod._process_solver_item(
            item, self._solve_fn(self.LAT1, self.LON, 1_000, vel_north=100.0))
        solver_mod._process_solver_item(
            item, self._solve_fn(lat2, self.LON, 21_000, vel_north=100.0))

        stored = self._stored()
        assert stored["lat"] == pytest.approx(lat2, abs=2e-4)
        # And decisively NOT the un-reckoned midpoint ~0.009° behind.
        assert abs(stored["lat"] - (self.LAT1 + lat2) / 2) > 5e-3

    def test_stationary_noise_is_averaged(self):
        """Zero velocity: two noisy solves of a stationary target average to
        the midpoint — the √K reduction dark targets used to be denied."""
        item = (dict(_CONFIRMED_N2), {}, time.time())
        solver_mod._process_solver_item(
            item, self._solve_fn(self.LAT1, self.LON, 1_000))
        solver_mod._process_solver_item(
            item, self._solve_fn(self.LAT1 + 0.01, self.LON, 21_000))

        stored = self._stored()
        assert stored["lat"] == pytest.approx(self.LAT1 + 0.005, abs=1e-6)

    def test_adsb_velocity_is_preferred_over_solved(self):
        """Tagged solve with a live ADS-B entry: DR follows ADS-B ground
        speed/track even when the solved velocity is junk."""
        state.adsb_aircraft["abc123"] = {
            # 100 m/s = 194.384 kt, due north.
            "gs": 194.384, "track": 0.0,
            "lat": self.LAT1, "lon": self.LON,
            "last_seen_ms": int(time.time() * 1000),
        }
        lat2 = self.LAT1 + self._D20S_DEG
        s_in = {"n_nodes": 3, "adsb_hex": "abc123"}
        # Solved velocity claims 200 m/s due EAST — junk that would drag the
        # average ~0.02° of longitude if it were used for DR.
        solver_mod._process_solver_item(
            (dict(s_in), {}, time.time()),
            self._solve_fn(self.LAT1, self.LON, 1_000, vel_east=200.0))
        solver_mod._process_solver_item(
            (dict(s_in), {}, time.time()),
            self._solve_fn(lat2, self.LON, 21_000, vel_east=200.0))

        stored = self._stored()
        assert stored["lat"] == pytest.approx(lat2, abs=2e-4)
        assert stored["lon"] == pytest.approx(self.LON, abs=1e-3)


class TestWorkerLoopResilience:
    """The worker loop must survive any single bad item (2026-08-08 outage:
    an unhandled exception killed both worker threads and publishing stopped
    silently until the queue overflowed)."""

    def test_poison_item_is_swallowed_counted_and_loop_continues(self):
        import queue as _queue

        _reset_state()
        before = state.solver_worker_errors
        # A private queue: a worker daemon leaked by an earlier test polls
        # state.solver_queue and would race these items away.  Malformed
        # items: item[0] on None raises TypeError inside
        # _process_solver_item, past every gate's own handling.
        q = _queue.Queue()
        q.put_nowait(None)
        q.put_nowait(None)
        assert solver_mod._solver_worker_iteration(timeout=0.1, q=q) is True
        assert solver_mod._solver_worker_iteration(timeout=0.1, q=q) is True
        assert state.solver_worker_errors == before + 2
        # Queue drained, no exception escaped either iteration.
        assert solver_mod._solver_worker_iteration(timeout=0.05, q=q) is False
