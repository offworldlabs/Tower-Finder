#!/usr/bin/env python3
"""Offline benchmark for inter-node association: how many tracks are false?

Why this exists
---------------
The false-track ("ghost") rate cannot be measured on staging.  Sampling the
live feed gives a per-snapshot standard deviation of ~25 points on a metric
whose interesting differences are 10-20 points, and it drifts systematically
with fleet age -- a 40-sample window read 3% and a 70-sample window read 50%
for identical code.  Two algorithm changes were evaluated that way and both
conclusions were wrong, in opposite directions.

Nothing about the question needs a live fleet.  The simulator produces exactly
the frames the nodes send (`generate_detections_for_node` is the same call the
fleet orchestrator makes), and it knows where every aircraft actually is.  So
the whole pipeline -- world, detections, association, solve -- runs offline,
deterministically under a seed, in seconds.

A "ghost" here is a solve with no true aircraft within MATCH_KM.  That is the
same criterion used against staging, so numbers are comparable to the
measurements already taken.

Runs blind by default: the per-detection ADS-B tags the simulator attaches are
stripped before association, because no receiver gets them and using them
amounts to reading the answer off the simulator.  See _strip_adsb -- it is the
difference between a 14% and a 79% ghost rate on identical scenes.  --tagged
restores the old behaviour for comparison.

Optionally, --mode track can also score every run against Stone-Soup's GOSPA
and SIAP metrics (scripts/stonesoup_metrics.py) -- an assignment-based
complement to the MATCH_KM ghost/matched counters above.  Where the counters
above answer one question decided in advance (is a solve within MATCH_KM of
some aircraft?), GOSPA decomposes a single distance into localisation/missed/
false costs via an optimal assignment at each sampled instant, and SIAP scores
completeness, ambiguity, spuriousness and track continuity the way the
tracking literature usually does -- a second, independently-implemented read
on the same runs.  This is entirely optional: stonesoup ships only in the
bench's own docker image, never in production's services/ dependencies, and
--ss-metrics defaults to "auto", which is silently off whenever stonesoup
is not importable or the run isn't --mode track.

Usage
-----
    python backend/scripts/association_bench.py
    python backend/scripts/association_bench.py --assoc-interval 2 10 30
    python backend/scripts/association_bench.py --seconds 300 --seed 7
    python backend/scripts/association_bench.py --tagged     # pre-blind numbers
"""

from __future__ import annotations

import argparse
import importlib
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict, namedtuple
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Bench-only, optional: stonesoup_metrics itself imports cleanly whether or
# not the `stonesoup` package is installed (see its module docstring) -- it
# is the bench's own docker image that carries stonesoup, never a dependency
# of anything under services/.
# DetectionAssociator is a superset of InterNodeAssociator — it adds the
# superseded detection path that --mode detection measures as the baseline, so
# constructing it unconditionally leaves --mode track unaffected.
import retina_analytics.association as _assoc_module  # noqa: E402
from retina_analytics.detection_association import DetectionAssociator  # noqa: E402
from retina_analytics.manager import NodeAnalyticsManager  # noqa: E402
from retina_geolocator.consensus import solve_consensus  # noqa: E402
from retina_geolocator.multinode_solver import (  # noqa: E402
    fit_constant_velocity,
    solve_multinode,
)
from retina_simulation.generator import coverage_cells, generate_fleet  # noqa: E402
from retina_simulation.orchestrator import _cells_to_metrocells  # noqa: E402
from retina_simulation.world import (  # noqa: E402
    NodeConfig,
    SimulationWorld,
    waypoints_for_metro,
)
from retina_tracker.tracker import Tracker  # noqa: E402

import services.track_filter as track_filter_mod  # noqa: E402
from scripts.stonesoup_metrics import STONESOUP_AVAILABLE, MetricRecorder, format_report_lines  # noqa: E402
from services.frame_processor import confirmed_track_views  # noqa: E402
from services.geo import (
    bearing_deg,  # noqa: E402
    bistatic_differential_km,  # noqa: E402
    node_beam_params,  # noqa: E402
)
from services.geo import haversine_km as _haversine_km  # noqa: E402
from services.tasks.solver import (  # noqa: E402
    _ewma_smooth_track,
    claim_decision,
    fov_gate_verdict,
    multinode_key_decision,
    resolve_n2_chi2,
)
from services.tasks.solver import (
    _reset_for_tests as _solver_reset_for_tests,
)

# ── Overlap-zone memoization (bench-only monkeypatch) ────────────────────
# register_node recomputes compute_overlap_zone for every node pair (105
# pairs at 15 nodes; a 91x91x6 pure-Python grid sweep each) on every run()
# call, and identical fleets recur across --repeat seeds, the
# --assoc-interval/--chi2-max/--estimator sweeps in main(), and any
# in-process sweep driver that imports run() directly -- all rebuilding the
# same zones from scratch. compute_overlap_zone is referenced as a bare
# module-global inside InterNodeAssociator.register_node, so overwriting the
# name on retina_analytics.association intercepts every call
# DetectionAssociator makes (DetectionAssociator inherits register_node
# unchanged). This is safe because:
#   - OverlapZone is never mutated after construction: __post_init__ and the
#     idempotent _ensure_np() are the only writers, and _ensure_np derives
#     its arrays deterministically from delay_pairs, so one instance can be
#     shared across associators and runs with no risk of cross-run drift.
#   - The bench always constructs its associator with coverage_provider=None
#     and fov_provider=None, so geo.coverage_limit and geo.fov are always
#     None here. Both are dynamic, non-hashable inputs, so a real value on
#     either bypasses the cache below rather than being treated as
#     memoizable.
#   - compute_overlap_zone consumes no random state, so skipping it on a
#     cache hit cannot shift any downstream RNG draw.
_OVERLAP_MEMO: dict = {}
_uncached_compute_overlap_zone = _assoc_module.compute_overlap_zone


def _geo_signature(geo):
    return (
        geo.node_id,
        geo.rx_lat,
        geo.rx_lon,
        geo.rx_alt_km,
        geo.tx_lat,
        geo.tx_lon,
        geo.tx_alt_km,
        geo.fc_hz,
        geo.beam_azimuth_deg,
        geo.beam_width_deg,
        geo.max_range_km,
        geo.max_bistatic_range_km,
    )


def _cached_compute_overlap_zone(
    geo_a,
    geo_b,
    grid_step_km=3.0,
    altitudes_km=(1.5, 3.0, 5.0, 7.0, 9.0, 11.0),
    delay_gate_us=5.0,
    doppler_gate_hz=30.0,
):
    if (
        geo_a.fov is not None
        or geo_b.fov is not None
        or geo_a.coverage_limit is not None
        or geo_b.coverage_limit is not None
    ):
        return _uncached_compute_overlap_zone(
            geo_a,
            geo_b,
            grid_step_km=grid_step_km,
            altitudes_km=altitudes_km,
            delay_gate_us=delay_gate_us,
            doppler_gate_hz=doppler_gate_hz,
        )
    key = (
        _geo_signature(geo_a),
        _geo_signature(geo_b),
        grid_step_km,
        tuple(altitudes_km),
        delay_gate_us,
        doppler_gate_hz,
    )
    zone = _OVERLAP_MEMO.get(key)
    if zone is None:
        zone = _uncached_compute_overlap_zone(
            geo_a,
            geo_b,
            grid_step_km=grid_step_km,
            altitudes_km=altitudes_km,
            delay_gate_us=delay_gate_us,
            doppler_gate_hz=doppler_gate_hz,
        )
        _OVERLAP_MEMO[key] = zone
    return zone


_assoc_module.compute_overlap_zone = _cached_compute_overlap_zone


# --smoother-leg opt-in replay (see run()'s buffer+replay section below): a
# lightweight stand-in for retina_simulation's SimulatedAircraft carrying only
# the five attributes MetricRecorder.record_truth reads (lat, lon, object_id,
# speed_km_s, heading_deg), so a run()'s world.aircraft can be snapshotted
# into a plain list once per step without holding a reference to the live
# World/aircraft objects for the rest of the run.
_AcSnap = namedtuple("_AcSnap", "lat lon object_id speed_km_s heading_deg")


# --estimator name -> solve_fn.  "consensus-refine" hands the consensus
# winner's supporting nodes to solve_multinode for a final LM polish (see
# retina_geolocator.consensus.solve_consensus's refine_fn parameter).
_ESTIMATORS = {
    "lm": solve_multinode,
    "consensus": solve_consensus,
    "consensus-refine": lambda s_in, node_cfgs: solve_consensus(
        s_in,
        node_cfgs,
        refine_fn=solve_multinode,
    ),
}

# A solve is credited to an aircraft if it lands within this radius.  Matches
# resolve_ground_truth_hex's display radius so results line up with the staging
# numbers; a solve further out than this is not a fix of that aircraft.
MATCH_KM = 8.0

# Track-identity constants, mirroring solver.py's _MN_ASSOC_* so the harness
# counts *tracks* the way production does.  This matters more than it sounds:
# a real aircraft's many solves collapse onto one transponder identity while
# phantoms mint separate ids, so the same pipeline reads ~3% ghosts by solve
# and ~68% by track.  Comparing one against the other is what made the staging
# numbers look irreconcilable with the offline ones.
GHOST_ASSOC_MAX_DIST_KM = 6.0
GHOST_ASSOC_MAX_AGE_S = 60.0


def _frame_to_detections(frame: dict) -> list[dict]:
    """Parallel arrays → the per-detection dicts retina-tracker takes.

    Same conversion PassiveRadarPipeline.process_frame does, so the bench feeds
    the tracker exactly what production does.
    """
    adsb_list = frame.get("adsb")
    dets = []
    for i, (d, f, s) in enumerate(zip(frame.get("delay", []), frame.get("doppler", []), frame.get("snr", []))):
        det = {"delay": d, "doppler": f, "snr": s}
        if adsb_list and i < len(adsb_list) and adsb_list[i] is not None:
            det["adsb"] = adsb_list[i]
        dets.append(det)
    return dets


def _strip_adsb(frame: dict) -> dict:
    """Return the frame as a real receiver would see it.

    find_associations uses the per-detection ``adsb`` list three ways that no
    hardware can reproduce: it drops any pairing whose detections lack an entry
    (clutter filter), drops any whose two ``hex`` codes differ (same-aircraft
    filter), and overwrites the grid position with the reported lat/lon, which
    then feeds solver.py's 2 km displacement gate.  A deployment gets an ADS-B
    *feed* listing aircraft; it does not get a truth label stapled to each radar
    detection, so those three amount to reading the answer off the simulator.

    Measured on the same scenes, the difference is not a detail:

        ring/any   tagged 13.9% ghost tracks   blind 79.0%
        dual/vhf   tagged 21.8% ghost tracks   blind 85.0%

    and by candidate, ring/any produces *zero* cross-aircraft pairings tagged
    against 68% false blind.  Every ghost number taken before this flag was
    measuring truth injection, so blind is the default and the tagged path is
    kept only to reproduce the old figures.
    """
    return {k: v for k, v in frame.items() if k != "adsb"}


def _beam_gate_ok(out: dict, s_in: dict, node_cfgs: dict, fov_provider) -> bool:
    """--fov leg: emulate solver.py's beam gate for one solve result.

    Reimplements the range/bearing half locally (node_beam_params + the same
    n=2-only bearing rule) because that half is inline in solver.py, not a
    separately importable function — but the FOV overlay uses
    fov_gate_verdict imported directly from services.tasks.solver, so the
    bench measures the SAME rule the live gate applies rather than a second
    copy of it that could drift from the shipped one.

    fov_provider(node_id) -> EmpiricalCoverageState | None; pass a callable
    returning None for every node (the --fov off leg) to get exactly
    today's range+bearing rule with no FOV involvement.
    """
    contributing_ids = out.get("contributing_node_ids") or [m["node_id"] for m in s_in.get("measurements", [])]
    n_nodes = out.get("n_nodes", s_in.get("n_nodes", 0))
    for nid in contributing_ids:
        cfg = node_cfgs.get(nid)
        if not cfg:
            continue
        p = node_beam_params(cfg)
        rx_lat, rx_lon = p["rx_lat"], p["rx_lon"]
        range_km = _haversine_km(rx_lat, rx_lon, out["lat"], out["lon"])

        if p["max_bistatic_range_km"] and p["tx_lat"] is not None:
            bistatic_km = bistatic_differential_km(
                p["tx_lat"],
                p["tx_lon"],
                rx_lat,
                rx_lon,
                out["lat"],
                out["lon"],
            )
            range_fail = bistatic_km > p["max_bistatic_range_km"]
        else:
            range_fail = range_km > p["max_range_km"]

        bearing_fail = False
        if n_nodes == 2 and p["beam_azimuth_deg"] is not None:
            brg = bearing_deg(rx_lat, rx_lon, out["lat"], out["lon"])
            off = abs((brg - p["beam_azimuth_deg"] + 180.0) % 360.0 - 180.0)
            bearing_fail = off > p["beam_width_deg"] / 2.0

        node_fail = range_fail or bearing_fail
        fov = fov_provider(nid)
        if fov is not None:
            brg = bearing_deg(rx_lat, rx_lon, out["lat"], out["lon"])
            node_fail = not fov_gate_verdict(
                fov,
                n_nodes,
                brg,
                range_km,
                range_rule_pass=not range_fail,
            )
        if node_fail:
            return False
    return True


class DeferredN2Gate:
    """The solver-side n=2 gate, replayed against simulated time.

    Production does not run the associator's own chi2 gate: state.py builds the
    associator with cv_fit=None, so chi2_per_dof is never set inside
    _pair_tracks, `fitted` stays empty and stage-2 selection never executes.
    The pairings travel to the solver worker carrying cv_epochs, and *this* is
    the gate that decides publication — solver._claim_track_pair over
    claim_decision, which the strict policy here imports directly.

    The bench measured the inline path instead, which is why its numbers
    described a configuration that does not ship.  This mirrors the shipped one:
    fit here, apply the same threshold, and arbitrate the same claim.  The
    claims dict is keyed and expired exactly as _TRACK_CLAIMS is, on simulated
    time rather than wall-clock since a replay compresses both.
    """

    def __init__(self, chi2_max: float, claim_ttl_s: float = 60.0, claim_policy: str = "strict"):
        self.chi2_max = chi2_max
        self.claim_ttl_s = claim_ttl_s
        # off          — no arbitration; shows what the claim is worth at all
        # strict       — production: refuse unless strictly better than the holder
        # self-refresh — a pairing may always renew its own claim, so a track's
        #                own winner is not locked out by its earlier, slightly
        #                better score as chi2 drifts with a growing epoch set
        # all-tracks   — self-refresh, but claiming every track in the cluster
        #                rather than only the first pair.  production reads
        #                track_pair_ids, which format_track_pairs_for_solver
        #                truncates to [:1], so a 3-node cluster leaves its third
        #                track unclaimed and free to seed a competing target.
        self.claim_policy = claim_policy
        # strict: track_id → (chi2, at) — the exact shape solver._TRACK_CLAIMS
        # holds, operated on by the imported claim_decision.
        self._claims: dict[str, tuple[float, float]] = {}
        # non-production policies: track_id → (chi2, at, pairing_key)
        self._claims3: dict[str, tuple[float, float, tuple]] = {}
        # attribution shadow (all policies): track_id → (chi2, at, pairing_key)
        self._pairing_of: dict[str, tuple[float, float, tuple]] = {}
        # Attribution: a refusal by the *same* pairing is a self-lockout — the
        # gate suppressing the hypothesis it already agreed with — and is a
        # different failure from losing to a genuine competitor.
        self.refused_self = 0
        self.refused_other = 0
        self.claim_chi2_drift: list[float] = []

    @staticmethod
    def _pairing_key(s_in: dict) -> tuple:
        return tuple(sorted(tid for pair in (s_in.get("track_pair_ids") or []) for tid in pair))

    def _claimed_track_ids(self, s_in: dict) -> list[str]:
        if self.claim_policy == "all-tracks":
            return list(s_in.get("track_ids") or [])
        return [tid for pair in (s_in.get("track_pair_ids") or []) for tid in pair]

    def resolve_chi2(self, s_in: dict, node_cfgs: dict) -> float | None:
        # The worker's own resolver — the bench used to carry a copy of the
        # fit plumbing, which is the drift this class exists to rule out.
        return resolve_n2_chi2(s_in, node_cfgs)

    def claim(self, s_in: dict, chi2: float, now_s: float) -> bool:
        pairs = s_in.get("track_pair_ids") or []
        if not pairs:
            return True
        if self.claim_policy == "off":
            return True
        key = self._pairing_key(s_in)
        track_ids = self._claimed_track_ids(s_in)
        # Attribution pass — observational only, mirroring the decision's
        # first-refusal order.  A refusal by the SAME pairing is a
        # self-lockout (the gate suppressing the hypothesis it already
        # agreed with); losing to a genuine competitor is a different fact.
        for tid, (_c, held_at, _k) in list(self._pairing_of.items()):
            if now_s - held_at > self.claim_ttl_s:
                del self._pairing_of[tid]
        for tid in track_ids:
            held = self._pairing_of.get(tid)
            if held is None or held[0] >= chi2:
                continue
            if held[2] == key:
                self.refused_self += 1
                self.claim_chi2_drift.append(chi2 - held[0])
                if self.claim_policy in ("self-refresh", "all-tracks"):
                    continue
            else:
                self.refused_other += 1
            break

        if self.claim_policy == "strict":
            # Production's rule, imported — services.tasks.solver.claim_decision
            # is the exact function _claim_track_pair runs under its lock, so
            # the strict policy measures shipped code by construction.  (The
            # bench used to carry a copy; its docstring even cited solver line
            # numbers that had gone stale.)
            ok = claim_decision(self._claims, list(track_ids), chi2, now_s, self.claim_ttl_s)
            if ok:
                for tid in track_ids:
                    self._pairing_of[tid] = (chi2, now_s, key)
            return ok

        # Non-production policies (self-refresh / all-tracks) — deliberate
        # alternatives, kept for the A/B they exist to measure.
        for tid, (_c, held_at, _k) in list(self._claims3.items()):
            if now_s - held_at > self.claim_ttl_s:
                del self._claims3[tid]
        for tid in track_ids:
            held = self._claims3.get(tid)
            if held is None or held[0] >= chi2:
                continue
            if held[2] == key:
                continue
            return False
        for tid in track_ids:
            self._claims3[tid] = (chi2, now_s, key)
            self._pairing_of[tid] = (chi2, now_s, key)
        return True


@dataclass
class Result:
    matched: int = 0
    ghosts: int = 0
    errors_km: list = None
    ghost_dist_km: list = None
    n_nodes_matched: Counter = None
    n_nodes_ghost: Counter = None
    speeds_kt: list = None
    solver_rejects: int = 0
    # Solves that cleared solve_fn but failed _beam_gate_ok (services/tasks/
    # solver.py's beam gate, emulated for the --fov leg — see _beam_gate_ok).
    # New this round: the bench had no beam-gate emulation before, so this
    # is nonzero even under --fov off (today's range+bearing rule, which
    # production always applies, now actually runs here too) — the go/no-go
    # comparison is the --fov off vs --fov active DELTA on n=2 ghost rate,
    # not this counter's absolute value.
    beam_rejects: int = 0
    matched_tracks: set = None
    ghost_tracks: set = None
    speed_err_ms: list = None
    err_by_n: dict = None
    # Split by n for the same reason as err_by_n: the vz-bound question is
    # specifically whether n=3's exactly-determined Doppler fit produces junk
    # horizontal velocity, and the aggregate hides it under the n=2 bulk.
    speed_err_by_n: dict = None
    dopp_rms_by_n: dict = None
    # What the constant-velocity gate did, straight off the associator, so its
    # effect is observable rather than inferred from the ghost rate moving.
    gate_gated: int = 0
    gate_rejected: int = 0
    gate_accepted: int = 0
    gate_unfitted: int = 0
    gate_superseded: int = 0
    # Deferred mode only: what the *solver-side* n=2 gate did.  In production the
    # associator emits unscored pairings and this gate is the one that runs, so
    # without these the shipped configuration's selection is invisible.
    n2_withheld_unfitted: int = 0
    n2_withheld_chi2: int = 0
    n2_withheld_claimed: int = 0
    # Of those, how many lost to a claim held by *their own* pairing rather than
    # a competing one — the gate suppressing a hypothesis it already accepted.
    claim_refused_self: int = 0
    claim_refused_other: int = 0
    claim_chi2_drift: list = None
    # How many single-node tracks each solver input spans.  The claim reads
    # track_pair_ids, which format_track_pairs_for_solver truncates to [:1],
    # so anything above 2 here is a track the claim cannot reach.
    cluster_sizes: Counter = None
    # Per track, the contributing-node counts its solves were made from.  A
    # track-level ghost rate split by n needs this because one track can be
    # solved at n=2 on one round and n=3 on the next; "an n=2 track" means
    # every solve behind it was n=2.
    track_n: dict = None
    # Per-solve wall time (ms), timed around the solve_fn call regardless of
    # estimator — the --estimator sweep exists partly to compare this against
    # the LM baseline.
    solve_ms: list = None

    # ── Top-down claiming (--claim-mode) ──────────────────────────────────
    # Straight off the library associator — same counters the live
    # /api/radar/association/status "claiming" block reports.
    claims_matched: int = 0
    claim_conflicts: int = 0
    anchored_inputs: int = 0
    # Bench-side, mirroring solver.py's solver_anchor_* / solver_anchored_
    # published: counted at the bench's own publish point (multinode_key_
    # decision against bench_mn), which is where an anchor is actually
    # honored or falls through.
    anchored_published: int = 0
    anchor_fallbacks: int = 0
    # Distinct state.multinode_tracks-equivalent keys minted over the whole
    # run (bench_mn) — the acceptance metric claiming exists to move:
    # distinct published keys should drop toward O(targets).  keys_real/
    # keys_ghost split that count by the same MATCH_KM binding the solve-
    # level matched/ghosts counters use, evaluated at each solve that
    # updates a key (a key can appear in both if its solves wander across
    # the gate — same non-exclusivity matched_tracks/ghost_tracks already
    # accept).
    distinct_keys: int = 0
    keys_real: int = 0
    keys_ghost: int = 0

    # Stone-Soup GOSPA/SIAP scalars for this one run (--ss-metrics), or None
    # when it was off, stonesoup wasn't available, or the recorder had
    # nothing to score (see stonesoup_metrics.MetricRecorder.compute).  Not
    # in _SUM_FIELDS/_EXTEND_FIELDS/_COUNTER_FIELDS: unlike those, a single
    # seed's dict of pre-averaged scalars cannot be summed or extended into
    # a cross-seed aggregate the way a plain counter or list can -- see
    # ss_metrics_all below, which is what merge() populates instead.
    ss_metrics: dict = None
    # --smoother-leg opt-in (off by default -- see run()'s buffer+replay
    # section): label -> that leg's MetricRecorder.compute() dict.  Stays the
    # empty dict (set in __post_init__) when smoother_legs was never passed,
    # so nothing downstream needs to guard on this being None.  Not summable
    # for the same reason ss_metrics itself is not in _SUM_FIELDS/etc -- a
    # dict of pre-averaged scalars can't be added across seeds -- so
    # ss_metrics_smoothed_all (also set in __post_init__) is what merge()
    # actually pools, per label.
    ss_metrics_smoothed: dict = None

    def __post_init__(self):
        self.errors_km = []
        self.ghost_dist_km = []
        self.n_nodes_matched = Counter()
        self.n_nodes_ghost = Counter()
        self.speeds_kt = []
        self.matched_tracks = set()
        self.ghost_tracks = set()
        self.speed_err_ms = []
        self.err_by_n = defaultdict(list)
        self.speed_err_by_n = defaultdict(list)
        self.dopp_rms_by_n = defaultdict(list)
        self.claim_chi2_drift = []
        self.cluster_sizes = Counter()
        self.track_n = defaultdict(Counter)
        self.solve_ms = []
        self._ghost_tracks = {}
        # Cross-seed pool for ss_metrics -- see merge() and report()'s
        # "stonesoup pooled over N seeds" block.  Populated regardless of
        # whether this Result is a per-seed result or the running aggregate;
        # only the aggregate's list is ever read.
        self.ss_metrics_all = []
        self.ss_metrics_smoothed = {}
        # label -> [dict, ...] across seeds, mirroring ss_metrics_all but
        # keyed per --smoother-leg label since each leg is scored
        # independently.  Only the running aggregate's is ever read.
        self.ss_metrics_smoothed_all = defaultdict(list)

    @property
    def track_ghost_pct(self):
        n = len(self.matched_tracks) + len(self.ghost_tracks)
        return 100.0 * len(self.ghost_tracks) / n if n else 0.0

    def n2_only(self, keys) -> int:
        """How many of these tracks were solved only ever from two nodes."""
        return sum(1 for k in keys if set(self.track_n.get(k, {})) == {2})

    @property
    def track_ghost_pct_n2(self):
        """Ghost rate restricted to tracks that never got beyond two nodes.

        This is the population the whole exercise is about: at n=2 the solver
        has 5 unknowns against 4 residuals, so its residual gates cannot tell a
        cross pairing from a real one.  Mixing in the n>=3 tracks, which those
        gates do filter, dilutes exactly the signal being measured.
        """
        g = self.n2_only(self.ghost_tracks)
        m = self.n2_only(self.matched_tracks)
        return 100.0 * g / (g + m) if (g + m) else 0.0

    @property
    def total(self):
        return self.matched + self.ghosts

    @property
    def keys_per_object(self):
        """Headline claiming metric: published keys per real GT object
        actually matched.  1.0 is the floor — one key per aircraft; the
        pre-claiming baseline runs to ~O(solves) per object instead."""
        n_objects = len(self.matched_tracks)
        return self.keys_real / n_objects if n_objects else 0.0

    _SUM_FIELDS = (
        "matched",
        "ghosts",
        "solver_rejects",
        "beam_rejects",
        "gate_gated",
        "gate_rejected",
        "gate_accepted",
        "gate_unfitted",
        "gate_superseded",
        "n2_withheld_unfitted",
        "n2_withheld_chi2",
        "n2_withheld_claimed",
        "claim_refused_self",
        "claim_refused_other",
        "claims_matched",
        "claim_conflicts",
        "anchored_inputs",
        "anchored_published",
        "anchor_fallbacks",
        "distinct_keys",
        "keys_real",
        "keys_ghost",
    )
    _EXTEND_FIELDS = (
        "errors_km",
        "ghost_dist_km",
        "speeds_kt",
        "speed_err_ms",
        "claim_chi2_drift",
        "solve_ms",
    )
    _COUNTER_FIELDS = ("n_nodes_matched", "n_nodes_ghost", "cluster_sizes")

    def merge(self, other: Result, tag: str) -> None:
        """Fold one seed's Result into this cross-seed aggregate.

        This exists because a --repeat N run used to print the LAST seed's
        diagnostics under an "across N seeds" heading: every counter and list
        on Result was rebound per iteration, so the CV-gate, claim-refusal and
        cluster-size lines described one seed while the ghost-rate spread
        block above them described all N.  Only the six scalars fed into the
        spread block were ever aggregated.

        Track keys are namespaced with `tag`: track identities repeat across
        seeds, so a plain set union would collide a seed-3 ghost with a
        seed-5 real track and silently corrupt both counts.
        """
        for f in self._SUM_FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))
        for f in self._EXTEND_FIELDS:
            getattr(self, f).extend(getattr(other, f))
        for f in self._COUNTER_FIELDS:
            getattr(self, f).update(getattr(other, f))
        self.matched_tracks.update((tag, k) for k in other.matched_tracks)
        self.ghost_tracks.update((tag, k) for k in other.ghost_tracks)
        for k, counts in other.track_n.items():
            self.track_n[(tag, k)].update(counts)
        for nn, v in other.err_by_n.items():
            self.err_by_n[nn].extend(v)
        for nn, v in other.speed_err_by_n.items():
            self.speed_err_by_n[nn].extend(v)
        for nn, v in other.dopp_rms_by_n.items():
            self.dopp_rms_by_n[nn].extend(v)
        if other.ss_metrics:
            self.ss_metrics_all.append(other.ss_metrics)
        for _label, _m in (other.ss_metrics_smoothed or {}).items():
            if _m:
                self.ss_metrics_smoothed_all[_label].append(_m)

    @property
    def ghost_pct(self):
        return 100.0 * self.ghosts / self.total if self.total else 0.0


def build_scene(
    seed: int,
    n_nodes: int,
    n_cluster: int,
    metro: str,
    min_aircraft: int,
    max_aircraft: int,
    metro_traffic_frac: float,
    layout: str = "ring",
    illuminator_band: str = "any",
    dual_aim: str = "core",
):
    """Fleet + world, wired exactly as FleetOrchestrator._build_world does.

    The hub-radial routing is not a detail.  With metro_cells set,
    metro_traffic_frac of all spawns are funnelled through the ring core --
    the one patch of sky every ring beam overlaps.  Leave it unset (the
    SimulationWorld default is an empty cell list) and traffic disperses along
    the en-route waypoint net instead, so aircraft rarely share an overlap
    zone.  Since a false pairing requires two aircraft in one overlap zone,
    that single omission removes the mechanism under study.
    """
    fleet = generate_fleet(
        n_nodes=n_nodes,
        metro=metro,
        n_cluster=n_cluster,
        n_clusters=1,
        use_tower_api=False,
        seed=seed,
        layout=layout,
        illuminator_band=illuminator_band,
        dual_aim=dual_aim,
    )
    cells = coverage_cells(
        n_cluster=n_cluster,
        n_clusters=1,
        metro=metro,
    )
    center_lat = sum(c["rx_lat"] for c in fleet) / len(fleet)
    center_lon = sum(c["rx_lon"] for c in fleet) / len(fleet)
    world = SimulationWorld(
        center_lat=center_lat,
        center_lon=center_lon,
        waypoints=waypoints_for_metro(metro),
    )
    world.metro_cells = _cells_to_metrocells(cells)
    world.frac_metro_traffic = metro_traffic_frac
    for nd in fleet:
        world.add_node(
            NodeConfig(
                node_id=nd["node_id"],
                rx_lat=nd["rx_lat"],
                rx_lon=nd["rx_lon"],
                rx_alt_ft=nd["rx_alt_ft"],
                tx_lat=nd["tx_lat"],
                tx_lon=nd["tx_lon"],
                tx_alt_ft=nd["tx_alt_ft"],
                fc_hz=nd["fc_hz"],
                fs_hz=nd["fs_hz"],
                beam_azimuth_deg=nd.get("beam_azimuth_deg"),
                beam_width_deg=nd["beam_width_deg"],
                max_range_km=nd["max_range_km"],
                max_bistatic_range_km=nd.get("max_bistatic_range_km"),
            )
        )
    # Traffic density is the independent variable the ghost rate scales with:
    # a false pairing needs two aircraft inside one overlap zone.
    world.min_aircraft = min_aircraft
    world.max_aircraft = max_aircraft
    return fleet, world


def run(
    seed,
    seconds,
    dt,
    frame_interval,
    assoc_interval,
    n_nodes,
    n_cluster,
    metro,
    min_aircraft,
    max_aircraft,
    metro_traffic_frac,
    layout="ring",
    illuminator_band="any",
    dual_aim="core",
    blind=True,
    mode="detection",
    chi2_max=2.0,
    min_span_s=12.0,
    history_n=20,
    exclusive=True,
    cv_fit_mode="inline",
    claim_policy="strict",
    claim_ttl_s=60.0,
    solve_fn=solve_multinode,
    claim_mode="off",
    fov="off",
    ss_metrics=False,
    ss_metric_dt=5.0,
    ss_hold_s=12.0,
    smoother_legs=None,
) -> Result:
    import random

    random.seed(seed)
    fleet, world = build_scene(
        seed,
        n_nodes,
        n_cluster,
        metro,
        min_aircraft,
        max_aircraft,
        metro_traffic_frac,
        layout,
        illuminator_band,
        dual_aim,
    )
    node_cfgs = {nd["node_id"]: nd for nd in fleet}

    # --ss-metrics: Stone-Soup GOSPA/SIAP scoring, --mode track only (see
    # scripts/stonesoup_metrics.py).  The reference point is the fleet
    # centroid -- same quantity build_scene already computes for the
    # simulated world's center_lat/center_lon, recomputed here rather than
    # threaded through because build_scene doesn't return it.  main()'s
    # --ss-metrics auto/on/off resolution is what actually decides whether
    # ss_metrics is ever True, so this is the only place that constructs a
    # MetricRecorder.
    recorder = None
    if ss_metrics and mode == "track":
        _ref_lat = sum(nd["rx_lat"] for nd in fleet) / len(fleet)
        _ref_lon = sum(nd["rx_lon"] for nd in fleet) / len(fleet)
        recorder = MetricRecorder(
            _ref_lat,
            _ref_lon,
            duration_s=seconds,
            sample_dt_s=ss_metric_dt,
            hold_max_age_s=ss_hold_s,
            match_km=MATCH_KM,
        )

    # --smoother-leg opt-in: buffer the truth/publish series so they can be
    # replayed once per requested leg at the end of this run (see the replay
    # block right before `return res`) -- an env sweep then pays the
    # world/tracker/solver cost once per seed instead of once per config, and
    # every leg scores the IDENTICAL solve stream.  Stays empty (zero new
    # work) unless smoother_legs was actually passed on a --mode track run
    # with --ss-metrics on: any of those false and _buffer_replay is False,
    # so nothing below ever appends to these lists.
    _buffer_replay = bool(smoother_legs) and mode == "track" and recorder is not None
    truth_buf: list = []
    publish_buf: list = []

    # --fov {off,active}: a bench-local NodeAnalyticsManager, fed from the
    # same per-detection ADS-B truth the sim attaches to a match (the sim's
    # equivalent of production's record_adsb_calibration choke point) —
    # BEFORE _strip_adsb below, since a real receiver's ADS-B calibration
    # feed is a separate channel from the blind detection stream association
    # measures, not something the association gate itself sees.
    # storage_dir="" -- nothing persisted, this is a fresh run every time.
    fov_analytics = NodeAnalyticsManager(fov_mode="active") if fov == "active" else None
    if fov_analytics is not None:
        for nd in fleet:
            fov_analytics.register_node(nd["node_id"], nd)

    def fov_provider(node_id):
        return fov_analytics.learned_fov_for(node_id) if fov_analytics is not None else None

    # "inline" fits inside the associator, which is what the bench has always
    # done and what every published figure was measured on.  "deferred" is what
    # production runs (core/state.py:56 passes cv_fit=None): the associator
    # emits unscored pairings and the solver worker fits and arbitrates.  The
    # two are different code paths, so they need separate baselines.
    deferred = mode == "track" and cv_fit_mode == "deferred"
    assoc = DetectionAssociator(
        grid_step_km=3.0,
        cv_fit=(fit_constant_velocity if (mode == "track" and not deferred) else None),
        cv_chi2_max=chi2_max,
        cv_min_span_s=min_span_s,
        cv_exclusive=exclusive,
    )
    n2_gate = DeferredN2Gate(chi2_max, claim_ttl_s=claim_ttl_s, claim_policy=claim_policy) if deferred else None
    # One tracker per node, driven by every frame — mirrors
    # frame_processor.py:476-477.  The bench previously fed raw detections
    # straight to the associator, skipping the stage production runs first, so
    # it could not evaluate anything track-level and its clutter numbers were
    # pessimistic by whatever M-of-N would have removed.
    trackers = {nd["node_id"]: Tracker() for nd in fleet}
    for nd in fleet:
        assoc.register_node(nd["node_id"], nd)
    # The library rate-limits on time.monotonic(), i.e. wall-clock.  A replay
    # compresses minutes of simulated time into a second of real time, so the
    # limiter would gate out nearly every round and the sweep would measure
    # nothing.  Disable it and drive the cadence from simulated time below,
    # which is what the parameter means on a live fleet anyway.
    assoc._ASSOC_MIN_INTERVAL_S = 0.0
    last_assoc: dict[str, float] = {}

    # Top-down claiming (--claim-mode).  Only meaningful in --mode track:
    # off-mode's associator ignores claim_mode == "off" anyway, but setting
    # it unconditionally keeps --mode detection's assoc untouched by
    # construction (it never calls submit_tracks_round).
    #
    # Anchored inputs are ALWAYS unscored (chi2_per_dof=None, cv_epochs
    # carried for the solver worker to fit) regardless of --cv-fit, because
    # _claim_round has no inline fit path of its own — production shape.  So
    # --claim-mode only measures anything faithful together with --cv-fit
    # deferred (n2_gate is not None below): that is what actually resolves
    # cv_epochs and runs the n=2 confirmation gate on them.  With --cv-fit
    # inline (the bench default) n2_gate is None and an anchored n=2 input
    # skips that gate entirely — not a bug so much as an unsupported
    # combination; the verification runbook always pairs claim-mode with
    # --cv-fit deferred for exactly this reason.
    #
    # bench_mn mimics state.multinode_tracks: keyed the same way
    # (multinode_key_decision, the shipped rule, imported rather than
    # reimplemented — same reason claim_decision is imported above), pruned
    # on the same 60 s window.  It stores RAW (unsmoothed) positions — no
    # EWMA — a deliberate divergence from production's smoothed store: the
    # bench has no frame-to-frame position history to smooth over, and the
    # claiming gates compare against the tracklet's own last measurement
    # either way, not the smoothed position.
    bench_mn: dict[str, dict] = {}
    assoc.claim_mode = claim_mode if mode == "track" else "off"
    assoc.global_track_provider = lambda: list(bench_mn.values())
    _BENCH_MN_MAX_AGE_MS = 60_000

    res = Result()
    _all_keys_seen: set = set()
    _keys_real: set = set()
    _keys_ghost: set = set()

    # Reproduce the orchestrator's staggered sending (orchestrator.py:508-531).
    # Nodes are geo-sorted so same-metro neighbours land in adjacent slots, then
    # spread evenly across frame_interval.  This matters more than it looks:
    # each node's frame therefore describes the world at a *different* instant,
    # up to frame_interval apart, so one aircraft's delay at node A and node B
    # correspond to positions hundreds of metres apart.  Emitting every node
    # from a single world snapshot removes exactly the timing skew that lets a
    # cross-aircraft pairing look self-consistent — i.e. it engineers the bug
    # out of the scenario.
    def _geo_key(nid):
        n = world.nodes[nid]
        return (round(n.rx_lat, 0), round(n.rx_lon, 0), n.rx_lat, n.rx_lon)

    node_ids = sorted(node_cfgs, key=_geo_key)
    n = max(len(node_ids), 1)
    next_send = {nid: i * (frame_interval / n) for i, nid in enumerate(node_ids)}

    t = 0.0
    while t < seconds:
        world.step(dt, mode="adsb")
        t += dt
        ts_ms = int(t * 1000)
        if recorder is not None:
            recorder.record_truth(t, world.aircraft)
            if _buffer_replay:
                # Same call sequence record_truth itself sees (it grid-snaps
                # internally) -- the replay recorder built per leg below must
                # observe an identical sequence of record_truth calls to lay
                # truth over the same grid the raw recorder used.
                truth_buf.append(
                    (t, [_AcSnap(ac.lat, ac.lon, ac.object_id, ac.speed_km_s, ac.heading_deg) for ac in world.aircraft])
                )
        due_nodes = [nid for nid in node_ids if next_send[nid] <= t]
        if not due_nodes:
            continue
        # Carry the object id so a real target keeps one identity across
        # solves — keying on a position would mint a new track per epoch.
        truth = [(ac.lat, ac.lon, ac.object_id, ac.speed_km_s * 1000.0) for ac in world.aircraft]
        for nid in due_nodes:
            next_send[nid] += frame_interval
            frame = world.generate_detections_for_node(nid, ts_ms)
            if fov_analytics is not None:
                # The truth channel, not the (possibly blind) association
                # stream below -- a real node's ADS-B calibration reaches
                # the node's own receiver directly, independent of whether
                # per-detection tags are forwarded to association.
                for _ae in frame.get("adsb") or []:
                    if _ae and _ae.get("lat") is not None and _ae.get("lon") is not None:
                        fov_analytics.record_calibration_point(nid, _ae["lat"], _ae["lon"])
            if blind:
                frame = _strip_adsb(frame)
            # Every frame drives the node's own tracker, whether or not this
            # node triggers an association round — the tracker's job is
            # continuity and it must not be starved by the association cadence.
            trackers[nid].process_frame(_frame_to_detections(frame), ts_ms)
            # Keep every node's pending frame current — a neighbour's latest
            # frame is what association pairs against — but only let a node
            # *trigger* a round on its own cadence.
            if mode == "track":
                assoc._pending_tracks[nid] = confirmed_track_views(trackers[nid], history_n)
            else:
                assoc._pending_frames[nid] = frame
            if (t - last_assoc.get(nid, -1e9)) < assoc_interval:
                continue
            last_assoc[nid] = t
            if mode == "track":
                round_ = assoc.submit_tracks_round(nid, assoc._pending_tracks.get(nid, []), ts_ms)
                # anchored_inputs are already solver-input shaped (see
                # _claim_round) — empty unless --claim-mode active (or
                # shadow's counters-only, per submit_tracks_round).
                solver_inputs = assoc.format_track_pairs_for_solver(round_.pairs) + round_.anchored_inputs
            else:
                cands = assoc.submit_frame(nid, frame, ts_ms)
                solver_inputs = assoc.format_candidates_for_solver(cands) if cands else []
            for s_in in solver_inputs:
                # Split by n_nodes: the claim only arbitrates n=2 solves
                # (solver.py gates on n_nodes == 2), so cluster width only
                # matters for those.
                _k = "n2" if s_in.get("n_nodes", 0) == 2 else "n3+"
                res.cluster_sizes[(_k, len(s_in.get("track_ids") or []))] += 1
                if s_in.get("n_nodes", 0) < 2:
                    continue
                try:
                    _t0 = time.perf_counter()
                    out = solve_fn(s_in, node_cfgs)
                    res.solve_ms.append((time.perf_counter() - _t0) * 1000.0)
                except Exception:
                    res.solver_rejects += 1
                    continue
                if not out or not out.get("success"):
                    res.solver_rejects += 1
                    continue
                # Beam gate (services/tasks/solver.py, emulated here for
                # every run — not just --fov active — so the bench measures
                # the same funnel production does; fov_provider returns None
                # for every node under --fov off, which reduces this to
                # exactly today's range+bearing rule).  Runs before the n=2
                # gate below, mirroring solver.py's order.
                if not _beam_gate_ok(out, s_in, node_cfgs, fov_provider):
                    res.beam_rejects += 1
                    continue
                # The shipped n=2 gate.  Mirrors solver.py:607-632, including
                # that a withheld solve never reaches state.multinode_tracks —
                # so it must not be counted here as matched or ghost either.
                if n2_gate is not None and out.get("n_nodes", 0) == 2:
                    _chi2 = n2_gate.resolve_chi2(s_in, node_cfgs)
                    if _chi2 is None:
                        res.n2_withheld_unfitted += 1
                        continue
                    if _chi2 > n2_gate.chi2_max:
                        res.n2_withheld_chi2 += 1
                        continue
                    if not n2_gate.claim(s_in, _chi2, t):
                        res.n2_withheld_claimed += 1
                        continue

                # Bench-side state.multinode_tracks equivalent — accepted
                # solves only, mirroring where production's _MN_TRACKS_LOCK
                # block runs (after every gate above, before GT matching).
                if mode == "track":
                    _anchor_key = s_in.get("anchor_key")
                    _key, _how = multinode_key_decision(bench_mn, out, None, _anchor_key)
                    if _anchor_key:
                        res.anchored_published += 1
                        if _how != "anchor":
                            res.anchor_fallbacks += 1
                    _new_ids = set(s_in.get("track_ids") or [])
                    # Source-track supersession, mirroring solver.py: a later
                    # solve sharing source tracks with an earlier entry under
                    # a DIFFERENT key is the same aircraft re-solved past the
                    # match radius, not a second one.
                    if _new_ids:
                        for _old_key in list(bench_mn.keys()):
                            if _old_key == _key:
                                continue
                            if _new_ids.intersection(bench_mn[_old_key].get("source_track_ids") or ()):
                                del bench_mn[_old_key]
                    _prev_mn = bench_mn.get(_key)
                    bench_mn[_key] = {
                        "key": _key,
                        "lat": out["lat"],
                        "lon": out["lon"],
                        "alt_m": out.get("alt_m", 0.0),
                        "vel_east": out.get("vel_east", 0.0),
                        "vel_north": out.get("vel_north", 0.0),
                        "timestamp_ms": ts_ms,
                        "n_nodes": out.get("n_nodes", s_in.get("n_nodes", 0)),
                        "solve_count": (_prev_mn.get("solve_count", 0) if _prev_mn else 0) + 1,
                        "source_track_ids": sorted(_new_ids),
                    }
                    if recorder is not None:
                        recorder.record_publish(
                            t,
                            _key,
                            out["lat"],
                            out["lon"],
                            out.get("vel_east", 0.0) or 0.0,
                            out.get("vel_north", 0.0) or 0.0,
                        )
                        if _buffer_replay:
                            # dict(out): out is reused/mutated by later
                            # iterations of this loop (it's solve_fn's return
                            # value, not copied elsewhere), so the buffered
                            # entry needs its own copy to still describe THIS
                            # solve when the replay reads it after the run.
                            publish_buf.append((t, _key, ts_ms, dict(out)))
                    for _k in [
                        k for k, v in bench_mn.items() if ts_ms - v.get("timestamp_ms", 0) > _BENCH_MN_MAX_AGE_MS
                    ]:
                        del bench_mn[_k]
                    _all_keys_seen.add(_key)

                d, best_id, best_speed = min(
                    ((_haversine_km(out["lat"], out["lon"], a, b), oid, sp) for a, b, oid, sp in truth),
                    default=(float("inf"), None, 0.0),
                )
                nn = out.get("n_nodes", s_in.get("n_nodes", 0))
                speed = math.hypot(out.get("vel_east", 0.0), out.get("vel_north", 0.0))
                res.speeds_kt.append(speed * 1.94384)
                if mode == "track":
                    # Key->GT binding at publish, the same MATCH_KM radius
                    # the solve-level matched/ghosts split uses below.
                    (_keys_real if d <= MATCH_KM else _keys_ghost).add(_key)
                if d <= MATCH_KM:
                    res.matched += 1
                    res.errors_km.append(d)
                    res.n_nodes_matched[nn] += 1
                    # Broken out because the whole dual-site hypothesis is
                    # about the n=2 case specifically: whether two illuminators
                    # sharing one receiver confine the fix better than two
                    # receivers far apart do.  An aggregate over all n hides it.
                    res.err_by_n[nn].append(d)
                    # Track identity, mirroring solver._multinode_track_key:
                    # a solve with a known transponder collapses onto that
                    # aircraft, so a real target is one track however many
                    # times it is solved.
                    res.matched_tracks.add(best_id)
                    res.track_n[best_id][nn] += 1
                    # Velocity error is the quantity the Doppler rework
                    # targets; ghost rate is not.
                    if best_speed > 0:
                        res.speed_err_ms.append(abs(speed - best_speed))
                        res.speed_err_by_n[nn].append(abs(speed - best_speed))
                    res.dopp_rms_by_n[nn].append(out.get("rms_doppler", 0.0) or 0.0)
                else:
                    res.ghosts += 1
                    res.ghost_dist_km.append(d)
                    res.n_nodes_ghost[nn] += 1
                    # A phantom has no transponder, so it falls to the
                    # proximity/dead-reckon branch: successive solves within
                    # _MN_ASSOC_MAX_DIST_KM and _MN_ASSOC_MAX_AGE_S join one
                    # track, otherwise a new id is minted.
                    hit = None
                    for gk, (glat, glon, gts) in res._ghost_tracks.items():
                        if (t - gts) <= GHOST_ASSOC_MAX_AGE_S and _haversine_km(
                            out["lat"], out["lon"], glat, glon
                        ) <= GHOST_ASSOC_MAX_DIST_KM:
                            hit = gk
                            break
                    if hit is None:
                        hit = f"gh-{len(res._ghost_tracks)}"
                    res._ghost_tracks[hit] = (out["lat"], out["lon"], t)
                    res.ghost_tracks.add(hit)
                    res.track_n[hit][nn] += 1

    if n2_gate is not None:
        res.claim_refused_self = n2_gate.refused_self
        res.claim_refused_other = n2_gate.refused_other
        res.claim_chi2_drift = list(n2_gate.claim_chi2_drift)
    res.gate_gated = assoc.track_pairs_gated
    res.gate_rejected = assoc.track_pairs_rejected
    res.gate_accepted = assoc.track_pairs_accepted
    res.gate_unfitted = assoc.track_pairs_unfitted
    res.gate_superseded = assoc.track_pairs_superseded
    res.claims_matched = assoc.claims_matched
    res.claim_conflicts = assoc.claim_conflicts
    res.anchored_inputs = assoc.anchored_inputs_emitted
    res.distinct_keys = len(_all_keys_seen)
    res.keys_real = len(_keys_real)
    res.keys_ghost = len(_keys_ghost)
    if recorder is not None:
        res.ss_metrics = recorder.compute()
        if smoother_legs:
            # Buffer + replay, never inline: bench_mn (the pipeline's own
            # accepted-solve store) stayed raw for the whole run above --
            # only this end-of-run pass ever sees a smoothed position, and
            # only to feed a throwaway MetricRecorder per leg.  This is what
            # lets an env sweep (e.g. several TRACK_KF_SIGMA_A values) pay
            # the world/tracker/solver cost once per seed and score every leg
            # against the IDENTICAL buffered solve stream, rather than
            # rebuilding the whole scene once per config.
            for _label, _smoother, _env in smoother_legs:
                _saved_env: dict[str, str | None] = {
                    "TRACK_SMOOTHER": os.environ.get("TRACK_SMOOTHER"),
                }
                for _k in _env:
                    _saved_env[_k] = os.environ.get(_k)
                try:
                    os.environ["TRACK_SMOOTHER"] = _smoother
                    for _k, _v in _env.items():
                        os.environ[_k] = _v
                    # track_filter reads TRACK_SMOOTHER fresh on every
                    # smooth_solve() call, but reads its other numeric
                    # tunables (TRACK_KF_SIGMA_A etc.) once at import time --
                    # reload() re-reads the env just set above AND resets the
                    # module's own _KF_TRACKS state.  reload() mutates the
                    # module object in place, so the reference
                    # services.tasks.solver already holds (`from services
                    # import track_filter`) stays valid -- nothing there
                    # needs re-importing.
                    importlib.reload(track_filter_mod)
                    # Fresh EWMA history (_MN_POS_HISTORY) and KF state
                    # (track_filter.reset(), called inside this) per leg --
                    # otherwise leg N would start smoothing against leg
                    # N-1's trailing state instead of a clean slate.
                    _solver_reset_for_tests()
                    rec = MetricRecorder(
                        _ref_lat,
                        _ref_lon,
                        duration_s=seconds,
                        sample_dt_s=ss_metric_dt,
                        hold_max_age_s=ss_hold_s,
                        match_km=MATCH_KM,
                    )
                    for _t, _snaps in truth_buf:
                        rec.record_truth(_t, _snaps)
                    for _t, _key, _ts_ms, _out in publish_buf:
                        sm_in = dict(_out)
                        sm_in["timestamp_ms"] = _ts_ms
                        # adsb_hex=None: state.adsb_aircraft is empty in the
                        # bench process anyway, so this always takes the
                        # dark-target path, same as production would for
                        # every solve the bench can produce.
                        sm = track_filter_mod.smooth_solve(
                            sm_in, _key, None, ewma_fn=(_ewma_smooth_track if _smoother == "ewma" else None)
                        )
                        rec.record_publish(
                            _t,
                            _key,
                            sm["lat"],
                            sm["lon"],
                            _out.get("vel_east", 0.0) or 0.0,
                            _out.get("vel_north", 0.0) or 0.0,
                        )
                    res.ss_metrics_smoothed[_label] = rec.compute()
                finally:
                    # Restore exactly -- pop keys that were absent rather
                    # than setting them to the string "None" (os.environ
                    # values must be str).  Deliberately do NOT reload
                    # track_filter after restoring: the next leg (or the next
                    # run() call, or main()'s own process-wide env) always
                    # reloads before its first use, so leaving the module's
                    # cached tunables mid-leg here for the instant between
                    # legs is unobservable and saves a reload nobody reads.
                    for _k, _v in _saved_env.items():
                        if _v is None:
                            os.environ.pop(_k, None)
                        else:
                            os.environ[_k] = _v
    return res


def _print_pooled_ss_metrics(vals_list, indent: str = "    ") -> None:
    """Mean/sd of each GOSPA/SIAP headline scalar across a list of
    MetricRecorder.compute() dicts.

    Factored out of report()'s original raw-pooled block so every
    --smoother-leg pooled block (report() below) prints in the exact same
    shape -- one helper, reused, rather than a copy that could drift.  The
    default indent matches that original block's hardcoded four spaces, so
    calling this with no indent argument reproduces its output byte-for-byte.
    """
    _headline = (
        ("gospa_distance_m", "gospa dist", 1 / 1000.0, "km"),
        ("siap_completeness", "completeness", 1.0, ""),
        ("siap_ambiguity", "ambiguity", 1.0, ""),
        ("siap_spuriousness", "spuriousness", 1.0, ""),
        ("siap_pos_accuracy_m", "pos-acc", 1 / 1000.0, "km"),
        ("siap_vel_accuracy_ms", "vel-acc", 1.0, "m/s"),
    )
    for key, hlabel, scale, unit in _headline:
        vals = [m[key] * scale for m in vals_list if key in m]
        if not vals:
            continue
        mean = statistics.mean(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        print(f"{indent}{hlabel}: mean {mean:.2f}{unit}  sd {sd:.2f}{unit}")


def report(label: str, r: Result, truth_max_kt: float | None = None):
    print(f"\n=== {label} ===")
    print(
        f"  solves {r.total:>5}   matched {r.matched:>5}   ghosts {r.ghosts:>5}"
        f"   -> {r.ghost_pct:>5.1f}% ghosts (by solve)"
    )
    if r.solve_ms:
        _t = sorted(r.solve_ms)
        print(f"  solve time: median {statistics.median(_t):.1f} ms   p95 {_t[int(0.95 * (len(_t) - 1))]:.1f} ms")
    print(
        f"  tracks: real {len(r.matched_tracks):>3}   false {len(r.ghost_tracks):>3}"
        f"   -> {r.track_ghost_pct:>5.1f}% ghosts (by track — comparable to staging)"
    )
    _g2, _m2 = r.n2_only(r.ghost_tracks), r.n2_only(r.matched_tracks)
    print(
        f"  n=2-only tracks: real {_m2:>3}   false {_g2:>3}"
        f"   -> {r.track_ghost_pct_n2:>5.1f}% ghosts (the population under study)"
    )
    if r.errors_km:
        e = sorted(r.errors_km)
        print(
            f"  matched position error: median {statistics.median(e):.2f} km"
            f"  p90 {e[int(0.9 * (len(e) - 1))]:.2f}  max {max(e):.2f}"
        )
    if r.ghost_dist_km:
        g = sorted(r.ghost_dist_km)
        print(f"  ghost distance to nearest aircraft: median {statistics.median(g):.1f} km  max {max(g):.1f}")
    print(f"  matched n_nodes {dict(sorted(r.n_nodes_matched.items()))}")
    print(f"  ghost   n_nodes {dict(sorted(r.n_nodes_ghost.items()))}")
    if r.err_by_n:
        print("  position error by contributing-node count:")
        for nn in sorted(r.err_by_n):
            v = sorted(r.err_by_n[nn])
            print(
                f"      n={nn}: {len(v):>4} solves   median {statistics.median(v):5.2f} km"
                f"   p90 {v[int(0.9 * (len(v) - 1))]:5.2f}"
            )
    if r.speed_err_ms:
        e = sorted(r.speed_err_ms)
        print(
            f"  matched SPEED error: median {statistics.median(e):6.1f} m/s"
            f"  p90 {e[int(0.9 * (len(e) - 1))]:6.1f}  max {max(e):6.1f}"
        )
    if r.speed_err_by_n:
        print("  speed error / doppler RMS by contributing-node count:")
        for nn in sorted(r.speed_err_by_n):
            v = sorted(r.speed_err_by_n[nn])
            d = sorted(r.dopp_rms_by_n.get(nn, [])) or [0.0]
            print(
                f"      n={nn}: {len(v):>4} solves   speed err median"
                f" {statistics.median(v):6.1f} m/s   p90"
                f" {v[int(0.9 * (len(v) - 1))]:6.1f}"
                f"   dopp rms median {statistics.median(d):6.2f} Hz"
                f" max {max(d):6.2f}"
            )
    if r.speeds_kt and truth_max_kt:
        over = sum(1 for s in r.speeds_kt if s > truth_max_kt)
        print(
            f"  solves faster than any real aircraft ({truth_max_kt:.0f} kt): "
            f"{over} ({100 * over / len(r.speeds_kt):.0f}%)"
        )
    if r.gate_gated:
        print(
            f"  CV gate: {r.gate_gated} pairings past the delay grid  "
            f"-> {r.gate_accepted} fitted+kept, {r.gate_rejected} rejected on χ², "
            f"{r.gate_superseded} superseded by a better fit, "
            f"{r.gate_unfitted} not yet fittable"
        )
    _n2w = r.n2_withheld_unfitted + r.n2_withheld_chi2 + r.n2_withheld_claimed
    if _n2w:
        print(
            f"  solver n=2 gate: {_n2w} solves withheld  "
            f"-> {r.n2_withheld_unfitted} unfitted, "
            f"{r.n2_withheld_chi2} over χ², "
            f"{r.n2_withheld_claimed} outbid on a track claim"
        )
        if r.n2_withheld_claimed:
            _drift = sorted(r.claim_chi2_drift)
            _med = _drift[len(_drift) // 2] if _drift else 0.0
            print(
                f"    claim refusals: {r.claim_refused_self} by the pairing's "
                f"OWN earlier claim, {r.claim_refused_other} by a competitor"
                + (f"  (median chi2 drift +{_med:.3f})" if _drift else "")
            )
    if r.cluster_sizes:
        _tot = sum(r.cluster_sizes.values())
        for grp in ("n2", "n3+"):
            sizes = {k[1]: n for k, n in r.cluster_sizes.items() if k[0] == grp}
            if not sizes:
                continue
            tot = sum(sizes.values())
            wide = sum(n for k, n in sizes.items() if k > 2)
            note = (
                "arbitrated by the claim" if grp == "n2" else "never reaches the claim — solver gates it on n_nodes==2"
            )
            print(
                f"  cluster sizes {grp:4s} {dict(sorted(sizes.items()))}  "
                f"-> {100 * wide / max(tot, 1):.1f}% span >2 tracks  ({note})"
            )
    print(f"  solver rejects/failures: {r.solver_rejects}  beam gate rejects: {r.beam_rejects}")
    # Top-down claiming.  Gated on either counter moving: shadow mode counts
    # without ever emitting an anchored_input, active mode does both.
    if r.claims_matched + r.anchored_inputs > 0:
        print(
            f"  claiming: {r.claims_matched} matched, {r.claim_conflicts} "
            f"conflicts -> {r.anchored_inputs} anchored inputs emitted "
            f"({r.anchored_published} published, {r.anchor_fallbacks} "
            "fell back off the anchor)"
        )
        print(
            f"    distinct published keys: {r.distinct_keys}  "
            f"(real {r.keys_real}, ghost {r.keys_ghost})  "
            f"-> {r.keys_per_object:.2f} keys/matched-object "
            "(1.0 is the floor)"
        )
        print(f"    n=2-only track ghost rate: {r.track_ghost_pct_n2:.1f}%  (claiming must not raise this)")
    # Stone-Soup GOSPA/SIAP (--ss-metrics).  A single-seed Result carries its
    # own ss_metrics dict directly; the cross-seed aggregate merge() builds
    # has ss_metrics=None (a pooled dict of pre-averaged scalars isn't a
    # thing) but ss_metrics_all populated with each seed's dict instead --
    # see the pooled block below.
    if getattr(r, "ss_metrics", None):
        for line in format_report_lines(r.ss_metrics):
            print(line)
    elif getattr(r, "ss_metrics_all", None) and len(r.ss_metrics_all) > 1:
        print(f"  stonesoup pooled over {len(r.ss_metrics_all)} seeds:")
        _print_pooled_ss_metrics(r.ss_metrics_all)
    # --smoother-leg opt-in blocks.  ss_metrics_smoothed[_all] are empty
    # unless smoother_legs was actually passed to run() (see its buffer+
    # replay section), so both loops below are silent no-ops for every
    # existing call site -- default output is unchanged.
    for _sm_label in sorted(getattr(r, "ss_metrics_smoothed", None) or {}):
        _sm_metrics = r.ss_metrics_smoothed[_sm_label]
        if not _sm_metrics:
            continue
        print(f"  stonesoup smoothed[{_sm_label}]:")
        for line in format_report_lines(_sm_metrics, indent="    "):
            print(line)
    for _sm_label in sorted(getattr(r, "ss_metrics_smoothed_all", None) or {}):
        _sm_vals = r.ss_metrics_smoothed_all[_sm_label]
        if len(_sm_vals) <= 1:
            continue
        print(f"  stonesoup smoothed[{_sm_label}] pooled over {len(_sm_vals)} seeds:")
        _print_pooled_ss_metrics(_sm_vals)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--seconds", type=float, default=180.0, help="simulated seconds per run")
    p.add_argument("--dt", type=float, default=1.0, help="world step (s)")
    p.add_argument("--frame-interval", type=float, default=2.0, help="node frame cadence (s) — matches FLEET_INTERVAL")
    p.add_argument(
        "--assoc-interval", type=float, nargs="+", default=[2.0], help="per-node association rate limit(s) to compare"
    )
    p.add_argument("--nodes", type=int, default=15)
    p.add_argument("--n-cluster", type=int, default=10)
    p.add_argument("--metro", default="gvl")
    p.add_argument("--layout", choices=("ring", "dual", "scatter"), default="ring")
    p.add_argument("--illuminator-band", choices=("any", "vhf"), default="any")
    p.add_argument("--dual-aim", choices=("core", "random"), default="core")
    p.add_argument(
        "--tagged",
        dest="blind",
        action="store_false",
        default=True,
        help="feed the per-detection ADS-B tags to association (reproduces pre-blind numbers; see _strip_adsb)",
    )
    p.add_argument(
        "--mode",
        choices=("detection", "track"),
        default="detection",
        help="pair raw detections (as shipped) or confirmed single-node tracks with a constant-velocity fit",
    )
    p.add_argument("--chi2-max", type=float, nargs="+", default=[2.0], help="track mode: chi2/dof ceiling(s) to sweep")
    p.add_argument(
        "--min-span-s", type=float, default=12.0, help="track mode: observation span before a pairing is fitted"
    )
    p.add_argument("--history-n", type=int, default=20, help="track mode: samples of per-node track history to fit")
    p.add_argument(
        "--cv-fit",
        dest="cv_fit_mode",
        choices=("inline", "deferred"),
        default="inline",
        help="track mode: where the constant-velocity fit runs. "
        "'inline' fits inside the associator — what every "
        "published bench figure was measured on. 'deferred' is "
        "what production runs: the associator emits unscored "
        "pairings and the solver fits and arbitrates them.",
    )
    p.add_argument(
        "--claim-policy",
        choices=("off", "strict", "self-refresh", "all-tracks"),
        default="strict",
        help="deferred mode: how the solver-side track claim "
        "arbitrates. off disables it (what is it worth?); "
        "strict is production; self-refresh lets a pairing "
        "renew its own claim as its chi2 drifts.",
    )
    p.add_argument("--claim-ttl-s", type=float, default=60.0, help="deferred mode: how long a track claim is held")
    p.add_argument(
        "--claim-mode",
        choices=("off", "shadow", "active"),
        default="off",
        help="track mode: top-down claiming (ASSOC_CLAIM_MODE). "
        "off is shipped default; shadow computes and counts "
        "without ever excluding a tracklet or emitting an "
        "anchored input; active does both.",
    )
    p.add_argument(
        "--fov",
        choices=("off", "active"),
        default="off",
        help="empirical FOV beam gate (FOV_MODE).  off is the "
        "shipped default: the beam gate emulation applies "
        "only today's range+bearing rule.  active feeds a "
        "bench-local NodeAnalyticsManager from the sim's "
        "ADS-B truth and applies the same n-split rule the "
        "live solver gate does (services.tasks.solver."
        "fov_gate_verdict).  Go/no-go: n=2 ghost-rate delta "
        "<= +3 points vs off, real tracks flat.",
    )
    p.add_argument(
        "--no-select",
        dest="exclusive",
        action="store_false",
        default=True,
        help="track mode: disable one-to-one hypothesis selection "
        "(each pairing then answers only to the chi2 threshold)",
    )
    p.add_argument("--min-aircraft", type=int, default=10, help="matches FLEET_AIRCRAFT lower bound")
    p.add_argument("--max-aircraft", type=int, default=20)
    p.add_argument("--metro-traffic-frac", type=float, default=0.85, help="matches FLEET_METRO_TRAFFIC_FRAC")
    p.add_argument(
        "--repeat", type=int, default=1, help="repeat each config with different seeds and report the spread"
    )
    p.add_argument(
        "--estimator",
        nargs="+",
        choices=("lm", "consensus", "consensus-refine"),
        default=["lm"],
        help="position solver(s) to sweep. 'lm' is solve_multinode "
        "(shipped). 'consensus' is the pairwise-intersection "
        "estimator (retina_geolocator.consensus) — "
        "no LM, no Doppler. 'consensus-refine' takes the "
        "consensus winner's supporting nodes and hands them "
        "to solve_multinode for a final LM polish.",
    )
    p.add_argument(
        "--ss-metrics",
        choices=("auto", "on", "off"),
        default="auto",
        help="Stone-Soup GOSPA/SIAP scoring (scripts/stonesoup_metrics.py), "
        "--mode track only. 'auto' (default) enables it iff "
        "stonesoup is importable AND --mode track, printing a "
        "one-line notice when it auto-disables for the former "
        "reason. 'on' forces it and errors out if stonesoup "
        "is not installed. 'off' never computes it.",
    )
    p.add_argument(
        "--ss-metric-dt",
        type=float,
        default=5.0,
        help="stonesoup: the uniform grid (seconds) both truth and "
        "published tracks are resampled onto before scoring -- "
        "see stonesoup_metrics.py's module docstring for why.",
    )
    p.add_argument(
        "--ss-hold-s",
        type=float,
        default=12.0,
        help="stonesoup: how long (seconds) a track's last publish "
        "is held, unmoved, across grid points before it counts "
        "as missing rather than tracked",
    )
    p.add_argument(
        "--smoother-leg",
        nargs="+",
        choices=("kf", "ewma", "off"),
        default=None,
        help="opt-in: replay the buffered truth/solve stream "
        "through services.track_filter.smooth_solve once per "
        "named leg and score each replay with its own "
        "stonesoup MetricRecorder -- bench_mn itself always "
        "stays raw (see run()'s buffer+replay section). "
        "--mode track and --ss-metrics on only; omitting "
        "this flag is zero new work and byte-identical "
        "output to today.",
    )
    p.add_argument(
        "--smoother-env",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help="env override applied to every --smoother-leg replay "
        "(e.g. TRACK_KF_SIGMA_A=3.0); repeatable, shared by "
        "all legs in this invocation",
    )
    args = p.parse_args()

    # --ss-metrics auto/on/off resolution.  "on" without stonesoup installed
    # is a hard error (the user explicitly asked for numbers this image
    # cannot produce); "auto" degrades quietly except for one notice line so
    # a --mode track run doesn't silently print fewer sections than expected
    # with no explanation.
    if args.ss_metrics == "on" and not STONESOUP_AVAILABLE:
        p.error(
            "--ss-metrics on requires the `stonesoup` package, which is "
            "not importable here. It ships only in the association "
            "bench's own docker image -- never a dependency of the "
            "shipped services/ code -- so this means the image this "
            "process is running in wasn't built with it. Use "
            "--ss-metrics auto or off, or rebuild with stonesoup."
        )
    if args.ss_metrics == "off":
        ss_metrics_enabled = False
    elif args.ss_metrics == "on":
        ss_metrics_enabled = True
    else:
        ss_metrics_enabled = STONESOUP_AVAILABLE and args.mode == "track"
        if not STONESOUP_AVAILABLE and args.mode == "track":
            print(
                "note: stonesoup not installed -- GOSPA/SIAP scoring "
                "auto-disabled (--ss-metrics on to force this to error "
                "instead, off to silence this notice)"
            )

    # --smoother-leg: dict.fromkeys dedups while preserving first-seen order,
    # so repeating a leg on the command line scores it once, not twice.
    # label == smoother name -- there is only one env_dict, shared by every
    # CLI leg, so the label needs nothing else to stay unique here.
    smoother_legs = None
    if args.smoother_leg:
        _smoother_env: dict[str, str] = {}
        for _entry in args.smoother_env or []:
            if "=" not in _entry:
                p.error(f"--smoother-env expects NAME=VALUE, got {_entry!r}")
            _name, _, _value = _entry.partition("=")
            _smoother_env[_name] = _value
        smoother_legs = [(m, m, _smoother_env) for m in dict.fromkeys(args.smoother_leg)]

    print(
        f"scene: metro={args.metro} layout={args.layout}/{args.illuminator_band} "
        f"nodes={args.nodes} budget={args.n_cluster} "
        f"{args.seconds:.0f}s @ {args.frame_interval:.0f}s frames, seed {args.seed}, "
        f"{'BLIND' if args.blind else 'ADS-B-tagged'}, mode={args.mode}"
        + (
            f", span>={args.min_span_s:.0f}s, cv-fit={args.cv_fit_mode}, claim-mode={args.claim_mode}"
            if args.mode == "track"
            else ""
        )
        + f", fov={args.fov}"
        + f", ss-metrics={'on' if ss_metrics_enabled else 'off'}"
        + (f", smoother-legs={','.join(lbl for lbl, _, _ in smoother_legs)}" if smoother_legs else "")
    )

    # chi2 only means anything in track mode; keep one pass otherwise.
    chi2_values = args.chi2_max if args.mode == "track" else [None]
    for interval in args.assoc_interval:
        for chi2_max in chi2_values:
            for estimator_name in args.estimator:
                solve_fn = _ESTIMATORS[estimator_name]
                rates, solve_rates, reals, fakes, speed_errs = [], [], [], [], []
                n2_rates = []
                agg = Result()
                last = None
                for k in range(args.repeat):
                    last = run(
                        args.seed + k,
                        args.seconds,
                        args.dt,
                        args.frame_interval,
                        interval,
                        args.nodes,
                        args.n_cluster,
                        args.metro,
                        args.min_aircraft,
                        args.max_aircraft,
                        args.metro_traffic_frac,
                        args.layout,
                        args.illuminator_band,
                        args.dual_aim,
                        args.blind,
                        args.mode,
                        chi2_max if chi2_max is not None else 2.0,
                        args.min_span_s,
                        args.history_n,
                        args.exclusive,
                        args.cv_fit_mode,
                        args.claim_policy,
                        args.claim_ttl_s,
                        solve_fn=solve_fn,
                        claim_mode=args.claim_mode,
                        fov=args.fov,
                        ss_metrics=ss_metrics_enabled,
                        ss_metric_dt=args.ss_metric_dt,
                        ss_hold_s=args.ss_hold_s,
                        smoother_legs=smoother_legs,
                    )
                    agg.merge(last, tag=f"s{args.seed + k}")
                    # Track-level is the comparable metric — solve-level and
                    # track-level differ by ~20x on the same data, so mixing them is
                    # how two staging conclusions went wrong.
                    rates.append(last.track_ghost_pct)
                    n2_rates.append(last.track_ghost_pct_n2)
                    solve_rates.append(last.ghost_pct)
                    reals.append(len(last.matched_tracks))
                    fakes.append(len(last.ghost_tracks))
                    if last.speed_err_ms:
                        speed_errs.append(statistics.median(last.speed_err_ms))
                label = f"assoc_interval={interval:g}s"
                if chi2_max is not None:
                    label += f"  chi2/dof<={chi2_max:g}"
                label += f"  estimator={estimator_name}"
                if args.repeat > 1:
                    label += f"  (pooled over {args.repeat} seeds)"
                report(label, agg if args.repeat > 1 else last)
                if args.repeat > 1:
                    mean = statistics.mean(rates)
                    sd = statistics.pstdev(rates)
                    print(f"  across {args.repeat} seeds (by track): {', '.join(f'{x:.0f}%' for x in rates)}")
                    print(
                        f"    mean {mean:.0f}%   sd {sd:.0f}   "
                        f"range {min(rates):.0f}-{max(rates):.0f}%   "
                        f"real {min(reals)}-{max(reals)}  false {min(fakes)}-{max(fakes)}"
                    )
                    print(
                        f"    n=2-only tracks: mean {statistics.mean(n2_rates):.0f}%   "
                        f"sd {statistics.pstdev(n2_rates):.0f}   "
                        f"({', '.join(f'{x:.0f}%' for x in n2_rates)})"
                    )
                    print(f"    by solve: {', '.join(f'{x:.1f}%' for x in solve_rates)}")
                    if speed_errs:
                        print(
                            f"    median speed error per seed: "
                            f"{', '.join(f'{x:.0f}' for x in speed_errs)} m/s"
                            f"   mean {statistics.mean(speed_errs):.0f}"
                        )


if __name__ == "__main__":
    main()
