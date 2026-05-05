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

        assert any(k.startswith("mn-8000-") for k in state.multinode_tracks)
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
        item = ({"n_nodes": 2}, {}, old_enqueued_at)
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is None
        assert not solve_called, "solver must not be invoked for stale items"
        assert state.solver_successes == 0
        assert state.solver_failures == 0
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

        item = ({"n_nodes": 2}, {}, time.time())
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
            "n_nodes": 2,
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

        s_in = {"n_nodes": 2, "initial_guess": {"lat": 40.3, "lon": -74.0, "alt_km": 9.0}}
        item = (s_in, node_cfgs, time.time())
        result = solver_mod._process_solver_item(item, solve_fn)

        assert result is not None
        assert any(k.startswith("mn-9002-") for k in state.multinode_tracks)
        assert state.solver_successes == 1
        assert state.solver_failures == 0


# ── _in_node_beam ─────────────────────────────────────────────────────────────


class TestInNodeBeam:
    """Test uncovered branches of _in_node_beam: TX-derived azimuth and no-beam fallback."""

    def test_tx_lat_lon_derives_beam_azimuth_aircraft_outside(self):
        """TX-lat/lon branch: beam_az = bearing(RX→TX)+90; aircraft clearly outside."""
        # RX=(0,0), TX=(1,1) NE → bearing≈45° → beam_az≈135° (SE)
        # Aircraft at (0.1,-0.1) NW (~15 km, in range) → bearing≈315° → 180° off boresight
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0, "tx_lat": 1.0, "tx_lon": 1.0}
        assert solver_mod._in_node_beam(0.1, -0.1, cfg) is False

    def test_tx_lat_lon_derives_beam_azimuth_aircraft_inside(self):
        """TX-lat/lon branch: aircraft in the derived beam direction."""
        # RX=(0,0), TX=(1,1) NE → beam_az≈135° (SE)
        # Aircraft at (-0.1,0.1) SE (~15 km, in range) → bearing≈135° → 0° off boresight
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0, "tx_lat": 1.0, "tx_lon": 1.0}
        assert solver_mod._in_node_beam(-0.1, 0.1, cfg) is True

    def test_no_beam_direction_returns_true_within_range(self):
        """No beam_azimuth_deg and no tx_lat/tx_lon → beam_az=None → always True."""
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0}
        assert solver_mod._in_node_beam(0.1, 0.0, cfg) is True

    def test_out_of_range_returns_false_regardless_of_beam(self):
        """Haversine check fires before beam check; beyond max_range → False."""
        cfg = {"rx_lat": 0.0, "rx_lon": 0.0, "max_range_km": 10.0}
        # ~111 km away, well outside 10 km range
        assert solver_mod._in_node_beam(1.0, 0.0, cfg) is False


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
        with pytest.raises(ValueError, match="fail at"):
            solver_mod._sweep_altitudes(s_in, {}, bad_solve, [1.0, 2.0], "rms_delay")
        assert len(calls) == 2

    def test_one_fails_one_succeeds_returns_good_result(self):
        """An exception on one layer is swallowed; a successful layer wins."""
        def mixed_solve(s, cfgs):
            if s["initial_guess"]["alt_km"] == 1.0:
                raise ValueError("bad layer")
            return {"success": True, "rms_delay": 0.005}

        s_in = {"initial_guess": {"lat": 0.0, "lon": 0.0}}
        result = solver_mod._sweep_altitudes(s_in, {}, mixed_solve, [1.0, 2.0], "rms_delay")
        assert result is not None
        assert result["success"] is True

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

