"""Tests for the known-lane solver (services/tasks/known_lane.py).

Slice B of the two-lane pipeline: identity-claimed detections from
``state.known_claims`` (owned by the claiming stage — the structure is
constructed directly on state here) are solved per hex unconditionally, so
known targets always produce a solve attempt and an accuracy sample, whatever
the displacement gate would have said.  Pinned here:

- a synthetic 2-node and 3-node claim set solves through the REAL LM solver
  and classifies truth_match;
- truth_match / ghost / no_converge classification, with the accuracy sample
  recorded on every converged outcome — ghost included — and the history
  record written on all three;
- shadow mode records but never touches the live feed; binding publishes
  truth_match solves under the hex's own mn-adsb-* key (and never ghosts);
- claim selection: staleness window, single-node and contested claims produce
  no attempt, per-hex dedup makes an unchanged registry free;
- graceful no-op when state.known_claims is absent (this branch's reality
  until slice A merges).

Style follows test_solver_worker.py / test_solver_anchor.py.
"""

import os
import time
from collections import deque

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from retina_geolocator.bistatic_models import bistatic_delay, bistatic_doppler  # noqa: E402
from retina_geolocator.multinode_solver import _lla_to_enu_km, solve_multinode  # noqa: E402

from core import state  # noqa: E402
from services.tasks import known_lane  # noqa: E402
from services.tasks import solver as solver_mod  # noqa: E402

HEX = "abc123"

# Realistic geometry around NYC, same as test_solver.py's two_node_configs,
# with a third node so the 3-node lane is exercised on real trigonometry.
NODE_CFGS = {
    "node_a": {
        "rx_lat": 40.7128,
        "rx_lon": -74.0060,
        "rx_alt_ft": 100,
        "tx_lat": 40.78,
        "tx_lon": -73.95,
        "tx_alt_ft": 500,
        "fc_hz": 100e6,
    },
    "node_b": {
        "rx_lat": 40.75,
        "rx_lon": -73.90,
        "rx_alt_ft": 150,
        "tx_lat": 40.70,
        "tx_lon": -73.85,
        "tx_alt_ft": 400,
        "fc_hz": 100e6,
    },
    "node_c": {
        "rx_lat": 40.62,
        "rx_lon": -73.99,
        "rx_alt_ft": 120,
        "tx_lat": 40.66,
        "tx_lon": -74.08,
        "tx_alt_ft": 450,
        "fc_hz": 100e6,
    },
}

# Target inside the node triangle: lat, lon, alt_km.
TARGET = (40.73, -73.95, 8.0)

_SENTINEL = object()


@pytest.fixture(autouse=True)
def _known_lane_state():
    """Install the slice-A claims registry directly on state, and restore
    whatever was (or was not) there afterward — save/restore rather than
    delattr so these tests keep passing unchanged once slice A declares the
    real attributes in core/state.py.

    Deliberately does NOT arm state.KNOWN_LANE_MODE: TestClient lifespans
    leak solver worker daemons into this process (see test_solver_worker's
    private-queue rationale), and every one of them polls maybe_run_pass
    against the live flag — arming it here would let a daemon race these
    tests for the per-hex dedup window.  Tests pass the mode explicitly
    instead; only the live-flag-semantics tests set the attribute, and only
    to values the lane reads as off.
    """
    prev_claims = getattr(state, "known_claims", _SENTINEL)
    prev_mode = getattr(state, "KNOWN_LANE_MODE", _SENTINEL)
    state.known_claims = {}
    yield
    for name, prev in (("known_claims", prev_claims), ("KNOWN_LANE_MODE", prev_mode)):
        if prev is _SENTINEL:
            # A test may have deleted or never set it.
            if hasattr(state, name):
                delattr(state, name)
        else:
            setattr(state, name, prev)


def _mk_claims(node_ids, target=TARGET, vel=(0.0, 0.0), ts_ms=None, contested=False):
    """Physically consistent claims for `target`: delay/doppler computed from
    the same bistatic models the solver fits, adsb_fix at the true position."""
    lat, lon, alt_km = target
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    t_enu = _lla_to_enu_km(lat, lon, alt_km * 1000, lat, lon, 0.0)
    claims = []
    for nid in node_ids:
        cfg = NODE_CFGS[nid]
        rx = _lla_to_enu_km(cfg["rx_lat"], cfg["rx_lon"], cfg["rx_alt_ft"] * 0.3048, lat, lon, 0.0)
        tx = _lla_to_enu_km(cfg["tx_lat"], cfg["tx_lon"], cfg["tx_alt_ft"] * 0.3048, lat, lon, 0.0)
        delay = bistatic_delay(t_enu, tx, rx)
        doppler = bistatic_doppler(t_enu, (vel[0], vel[1], 0.0), tx, rx, cfg["fc_hz"])
        claims.append(
            {
                "node_id": nid,
                "delay_us": delay,
                "doppler_hz": doppler,
                "pred_delay_us": delay,
                "pred_doppler_hz": doppler,
                "ts_ms": ts_ms,
                "adsb_fix": {
                    "lat": lat,
                    "lon": lon,
                    "alt_baro": alt_km * 1000 / 0.3048,  # ft
                    "gs": 0.0,
                    "track": 0.0,
                    "fix_ts_ms": ts_ms,
                },
                "contested": contested,
            }
        )
    return claims


def _install(claims, hexn=HEX):
    state.known_claims.setdefault(hexn, deque(maxlen=64)).extend(claims)


def _stub_solve(lat_off=0.0, lon_off=0.0, success=True):
    """A solve_fn that converges lat_off/lon_off away from the initial guess
    (which the known lane places at truth), or fails to converge."""

    def fn(s_in, cfgs):
        if not success:
            return {"success": False}
        ig = s_in["initial_guess"]
        return {
            "success": True,
            "lat": ig["lat"] + lat_off,
            "lon": ig["lon"] + lon_off,
            "alt_m": ig["alt_km"] * 1000,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "rms_delay": 0.5,
            "rms_doppler": 2.0,
            "timestamp_ms": s_in["timestamp_ms"],
            "n_nodes": s_in["n_nodes"],
            "contributing_node_ids": [m["node_id"] for m in s_in["measurements"]],
        }

    return fn


def _run(solve_fn, mode="shadow"):
    return known_lane.run_known_lane_pass(solve_fn, NODE_CFGS, mode=mode)


def _known_records():
    return [r for r in state.mlat_solve_history if r.get("known_lane")]


class TestRealSolve:
    """The lane end-to-end through the real LM solver on clean geometry."""

    def test_two_node_claims_solve_and_truth_match(self):
        _install(_mk_claims(["node_a", "node_b"]))
        attempts = _run(solve_multinode)

        assert attempts == 1
        assert state.known_lane_attempts == 1
        assert state.known_lane_truth_match == 1
        assert state.known_lane_ghost == 0
        assert state.known_lane_no_converge == 0

        samples = list(state.accuracy_samples)
        assert len(samples) == 1
        s = samples[0]
        assert s["hex"] == HEX
        assert s["position_source"] == "known_lane_truth_match"
        assert s["label"] == "truth_match"
        assert s["n_nodes"] == 2
        assert s["error_km"] < solver_mod._MAX_DISPLACEMENT_KM

        recs = _known_records()
        assert len(recs) == 1
        r = recs[0]
        assert r["outcome"] == "known_truth_match"
        assert r["adsb_hex"] == HEX
        assert r["n_nodes"] == 2
        assert r["displacement_km"] == pytest.approx(s["error_km"], abs=0.01)

    def test_three_node_claims_solve_and_tag_n_nodes(self):
        _install(_mk_claims(["node_a", "node_b", "node_c"]))
        attempts = _run(solve_multinode)

        assert attempts == 1
        assert state.known_lane_truth_match == 1
        (sample,) = list(state.accuracy_samples)
        assert sample["n_nodes"] == 3
        (rec,) = _known_records()
        assert rec["n_nodes"] == 3


class TestClassification:
    def test_displaced_solve_is_a_ghost_and_still_sampled(self):
        """The displacement gate classifies; it must not destroy the record or
        the accuracy sample — a ghost's error is exactly the datum the regular
        pipeline's gate used to delete."""
        _install(_mk_claims(["node_a", "node_b"]))
        # ~5.6 km north of truth — well past _MAX_DISPLACEMENT_KM.
        _run(_stub_solve(lat_off=0.05))

        assert state.known_lane_ghost == 1
        assert state.known_lane_truth_match == 0
        (sample,) = list(state.accuracy_samples)
        assert sample["position_source"] == "known_lane_ghost"
        assert sample["error_km"] > solver_mod._MAX_DISPLACEMENT_KM
        (rec,) = _known_records()
        assert rec["outcome"] == "known_ghost"
        assert rec["published"] is False

    def test_ghost_is_never_published_even_in_binding(self):
        _install(_mk_claims(["node_a", "node_b"]))
        _run(_stub_solve(lat_off=0.05), mode="binding")

        assert state.known_lane_ghost == 1
        assert state.known_lane_published == 0
        assert not state.multinode_tracks

    def test_unconverged_solve_records_but_never_samples(self):
        _install(_mk_claims(["node_a", "node_b"]))
        _run(_stub_solve(success=False))

        assert state.known_lane_no_converge == 1
        assert not state.accuracy_samples
        (rec,) = _known_records()
        assert rec["outcome"] == "known_no_converge"

    def test_solver_exception_counts_as_no_converge(self):
        _install(_mk_claims(["node_a", "node_b"]))

        def boom(s_in, cfgs):
            raise ValueError("boom")

        attempts = _run(boom)
        assert attempts == 1
        assert state.known_lane_no_converge == 1
        (rec,) = _known_records()
        assert rec["outcome"] == "known_no_converge"


class TestModeGating:
    def test_off_mode_does_nothing_at_all(self):
        state.KNOWN_LANE_MODE = "off"
        _install(_mk_claims(["node_a", "node_b"]))
        # No mode override: this is the live-flag read the worker loop uses.
        attempts = known_lane.run_known_lane_pass(_stub_solve(), NODE_CFGS)

        assert attempts == 0
        assert state.known_lane_attempts == 0
        assert not state.accuracy_samples
        assert not _known_records()

    def test_unrecognised_live_mode_falls_back_to_off(self):
        state.KNOWN_LANE_MODE = "banana"
        _install(_mk_claims(["node_a", "node_b"]))
        assert known_lane.run_known_lane_pass(_stub_solve(), NODE_CFGS) == 0

    def test_unrecognised_override_falls_back_to_off(self):
        _install(_mk_claims(["node_a", "node_b"]))
        assert _run(_stub_solve(), mode="banana") == 0

    def test_shadow_records_but_publishes_nothing(self):
        _install(_mk_claims(["node_a", "node_b"]))
        _run(_stub_solve())

        assert state.known_lane_truth_match == 1
        assert state.known_lane_published == 0
        assert not state.multinode_tracks
        assert not state.track_archive_buffer
        (rec,) = _known_records()
        assert rec["published"] is False
        assert rec["solve_key"] is None

    def test_binding_publishes_under_the_hex(self):
        _install(_mk_claims(["node_a", "node_b"]))
        _run(_stub_solve(), mode="binding")

        assert state.known_lane_published == 1
        key = f"mn-adsb-{HEX}"
        assert key in state.multinode_tracks
        entry = state.multinode_tracks[key]
        assert entry["adsb_hex"] == HEX
        assert entry["solve_count"] == 1
        # Archived for the Parquet stream like any published solve.
        assert len(state.track_archive_buffer) == 1
        (rec,) = _known_records()
        assert rec["published"] is True
        assert rec["solve_key"] == key
        assert rec["solver_hex"] is not None

    def test_binding_supersedes_the_regular_pipelines_entry(self):
        """Same key as the regular tagged path (mn-adsb-*), so a known-lane
        publish replaces the regular pipeline's output for that hex rather
        than coexisting with it."""
        key = f"mn-adsb-{HEX}"
        state.multinode_tracks[key] = {
            "lat": 1.0,
            "lon": 1.0,
            "timestamp_ms": 1,
            "solve_count": 4,
            "is_anomalous": True,
            "anomaly_types": ["supersonic"],
        }
        _install(_mk_claims(["node_a", "node_b"]))
        _run(_stub_solve(), mode="binding")

        entry = state.multinode_tracks[key]
        assert entry["lat"] != 1.0
        # solve_count continuity and the anomaly latch survive the takeover,
        # exactly as they do between successive regular solves.
        assert entry["solve_count"] == 5
        assert entry["is_anomalous"] is True
        assert "supersonic" in entry["anomaly_types"]


class TestClaimSelection:
    def test_stale_claims_produce_no_attempt(self):
        stale_ms = int(time.time() * 1000) - int((known_lane._CLAIM_MAX_AGE_S + 5.0) * 1000)
        _install(_mk_claims(["node_a", "node_b"], ts_ms=stale_ms))
        assert _run(_stub_solve()) == 0
        assert state.known_lane_attempts == 0

    def test_single_node_claims_produce_no_attempt(self):
        _install(_mk_claims(["node_a"]))
        assert _run(_stub_solve()) == 0
        assert state.known_lane_attempts == 0

    def test_contested_claims_are_skipped(self):
        _install(_mk_claims(["node_a"], contested=True))
        _install(_mk_claims(["node_b"]))
        # node_a's only claim is contested → one usable node → no attempt.
        assert _run(_stub_solve()) == 0

    def test_incompatible_timestamps_drop_the_lagging_node(self):
        now_ms = int(time.time() * 1000)
        lag_ms = now_ms - int((known_lane._CLAIM_SPREAD_S + 2.0) * 1000)
        _install(_mk_claims(["node_a"], ts_ms=now_ms))
        _install(_mk_claims(["node_b"], ts_ms=lag_ms))
        # Both fresh, but too far apart to be one epoch → no attempt.
        assert _run(_stub_solve()) == 0

    def test_newest_claim_per_node_wins(self):
        now_ms = int(time.time() * 1000)
        _install(_mk_claims(["node_a", "node_b"], ts_ms=now_ms - 3000))
        _install(_mk_claims(["node_a", "node_b"], ts_ms=now_ms))
        seen = {}

        def capture(s_in, cfgs):
            seen["ts"] = s_in["timestamp_ms"]
            return {"success": False}

        _run(capture)
        assert seen["ts"] == now_ms

    def test_unchanged_registry_is_not_resolved_twice(self):
        _install(_mk_claims(["node_a", "node_b"]))
        assert _run(_stub_solve()) == 1
        assert _run(_stub_solve()) == 0
        assert state.known_lane_attempts == 1

    def test_a_newer_claim_reopens_the_hex(self):
        now_ms = int(time.time() * 1000)
        _install(_mk_claims(["node_a", "node_b"], ts_ms=now_ms - 2000))
        assert _run(_stub_solve()) == 1
        _install(_mk_claims(["node_a", "node_b"], ts_ms=now_ms))
        assert _run(_stub_solve()) == 1
        assert state.known_lane_attempts == 2

    def test_malformed_claim_entries_are_survived(self):
        _install([None, {"node_id": "node_a"}, {"ts_ms": "not-a-number", "node_id": "x"}])
        _install(_mk_claims(["node_a", "node_b"]))
        assert _run(_stub_solve()) == 1

    def test_missing_adsb_fix_produces_no_attempt(self):
        claims = _mk_claims(["node_a", "node_b"])
        for c in claims:
            c["adsb_fix"] = None
        _install(claims)
        assert _run(_stub_solve()) == 0


class TestSliceAAbsence:
    """This branch predates slice A: neither state attribute exists in
    production code, and the lane must be a graceful no-op without them."""

    def test_absent_known_claims_is_a_noop(self):
        delattr(state, "known_claims")  # fixture restores
        assert _run(_stub_solve()) == 0
        assert state.known_lane_attempts == 0

    def test_absent_mode_reads_as_off(self):
        assert not hasattr(state, "KNOWN_LANE_MODE")
        assert known_lane._mode() == "off"
        state.known_claims = {HEX: deque(_mk_claims(["node_a", "node_b"]))}
        assert known_lane.run_known_lane_pass(_stub_solve(), NODE_CFGS) == 0


class TestMaybeRunPass:
    """The worker-loop entry point: interval-gated and unable to raise."""

    def test_interval_gate_holds_between_passes(self):
        _install(_mk_claims(["node_a", "node_b"]))
        known_lane.maybe_run_pass(_stub_solve(), mode="shadow")
        assert state.known_lane_attempts == 1
        # A newer claim arrives, but the pass interval has not elapsed.
        _install(_mk_claims(["node_a", "node_b"], ts_ms=int(time.time() * 1000) + 10))
        known_lane.maybe_run_pass(_stub_solve(), mode="shadow")
        assert state.known_lane_attempts == 1
        # Force the interval open (tests only) — now it runs.
        known_lane._last_pass_ts = 0.0
        known_lane.maybe_run_pass(_stub_solve(), mode="shadow")
        assert state.known_lane_attempts == 2

    def test_off_mode_returns_before_the_lock(self):
        _install(_mk_claims(["node_a", "node_b"]))
        known_lane.maybe_run_pass(_stub_solve(), mode="off")
        assert state.known_lane_attempts == 0

    def test_live_flag_absent_is_a_noop(self):
        """What the leaked worker daemons see on this branch: no flag, no
        pass, no side effects."""
        _install(_mk_claims(["node_a", "node_b"]))
        known_lane.maybe_run_pass(_stub_solve())
        assert state.known_lane_attempts == 0

    def test_never_raises_even_on_a_garbage_registry(self):
        state.known_claims = "not-a-dict"
        known_lane.maybe_run_pass(_stub_solve(), mode="shadow")  # must not raise
        assert state.known_lane_attempts == 0
