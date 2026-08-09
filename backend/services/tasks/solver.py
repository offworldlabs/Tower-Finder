"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import concurrent.futures
import logging
import math
import multiprocessing
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures.process import BrokenProcessPool

from config.constants import (
    ARC_ONLY_ANOMALY_ALLOWLIST,
    CV_VEL_ADOPT_CHI2_MAX,
    N2_CONFIRM_CHI2_MAX,
    N2_TRACK_ASSOCIATION,
)
from core import state
from services import track_filter

# Beam-coverage geometry, used to reject solver results whose range or (at
# n=2) bearing fall outside a contributing node's detection area.  This
# module carried its own haversine, bearing and in-beam rule until those
# were consolidated into services.geo.
from services.geo import bearing_deg, bistatic_differential_km, node_beam_params, offset_latlon_m
from services.geo import haversine_km as _haversine_km
from services.id_utils import multinode_hex_from_key

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

# ── Solver process pool ──────────────────────────────────────────────────────
# The LM solves are pure CPU on picklable dicts, and running them on worker
# *threads* meant competing for the GIL with frame processing and the
# analytics refresh: py-spy on staging showed the two solver threads getting
# ~25% of interpreter time between them, the queue draining at ~0.8 items/s,
# and burst tails aging past the 45 s staleness drop.  The threads remain —
# they own the gates, the track claims and the counters, which live in this
# process — but the solve_multinode / fit_constant_velocity calls are shipped
# to child processes so the optimizer runs on its own core.
#
# spawn, not fork: this process is full of threads holding locks, and a forked
# child would inherit them mid-flight.  A spawn child imports only
# retina_geolocator (the submitted functions' module), none of the backend.
#
# The pool is created by start_solver_workers, never at import: tests and the
# offline bench reach the compute inline through the same helper (pool is
# None → direct call), so they pay no child startup and need no teardown.  If
# the pool breaks mid-flight (a child OOM-killed, say) the affected call runs
# inline and the first thread to notice rebuilds the pool for the rest.
_solver_pool: concurrent.futures.ProcessPoolExecutor | None = None
_solver_pool_lock = threading.Lock()

# Never under pytest: route tests boot the whole app via TestClient, so the
# lifespan's start_solver_workers would hang a real pool off the test process
# — and any later test that monkeypatches the compute functions would ship an
# unpicklable closure to a child.  Pool transport has its own tests, which
# build the pool explicitly.
_POOL_ENABLED = os.getenv("RETINA_ENV", "").lower() != "test"


def _make_solver_pool() -> concurrent.futures.ProcessPoolExecutor:
    return concurrent.futures.ProcessPoolExecutor(
        max_workers=_N_SOLVER_WORKERS,
        mp_context=multiprocessing.get_context("spawn"),
    )


def _pool_call(fn, *args):
    """Run fn in the solver process pool; inline when there is no pool."""
    global _solver_pool
    pool = _solver_pool
    if pool is None:
        return fn(*args)
    try:
        return pool.submit(fn, *args).result()
    except BrokenProcessPool:
        with _solver_pool_lock:
            if _solver_pool is pool:
                try:
                    pool.shutdown(wait=False)
                except Exception:
                    pass
                try:
                    _solver_pool = _make_solver_pool()
                    logging.warning("Solver process pool broke — recreated")
                except Exception:
                    _solver_pool = None
                    logging.exception(
                        "Solver process pool broke and could not be recreated"
                        " — solving inline on worker threads from now on"
                    )
        return fn(*args)

# Altitude layers (km) tried when n_nodes ≥ 3.  For an overdetermined system
# (3+ delay equations, 2 unknowns after altitude pinning) only the correct
# altitude layer yields rms_delay ≈ 0; wrong layers give rms > 0, so picking
# the minimum selects the true altitude.  Layers match the association grid
# (5, 7, 9, 11) so that the correct altitude is always ≤ 1 km from a layer for
# Altitude sweep layers for n≥3 solver. Must match the altitudes_km used in
# compute_overlap_zone so the initial_guess alt from association matches a sweep
# point. Range 1.5–11 km covers simulation aircraft (0.3–15 km spawns) and
# commercial aviation. The 1.5 and 3.0 km layers fix systematic 7–10 km errors
# for low-altitude aircraft where the old [5,7,9,11] set forced wrong altitude.
_SOLVER_ALT_LAYERS_KM = [1.5, 3.0, 5.0, 7.0, 9.0, 11.0]

# Reject solver results whose RMS delay residual exceeds this value.
# For n≥3 nodes with altitude pinned (overdetermined: 3 equations, 2 unknowns),
# a true association converges with rms_delay ≈ measurement_noise ≈ 1-2 µs.
# False associations (delay measurements from different aircraft) produce
# inconsistent equations → rms_delay = 3-10 µs.
# For n=2, rms=0 at BOTH the true and mirror positions (exactly determined),
# so the threshold can't distinguish mirror from truth — keep generous.
# A single threshold of 3.0 µs cleans up false n≥3 associations while
# letting all n=2 results through (n=2 mirrors always have rms ≈ 0).
#
# 2026-08 live re-evaluation at n≥4: this blanket gate had near-zero
# discrimination — the bad-solve-through rate sat flat at ~2% across every
# threshold tried, while raising it enough to matter halved the good solves
# let through.  Cause: one contaminated measurement (a single-node track
# from a different aircraft, bundled in by association) inflates rms_delay
# without moving the Huber-fitted position, so rms stopped tracking position
# quality at n≥4.  _trim_and_resolve below drops the offending node instead
# of the whole solve, which restores the gate's original meaning.  Kept
# env-overridable for live tuning while that policy beds in.
_SOLVER_RMS_DELAY_MAX_US = float(os.getenv("SOLVER_RMS_DELAY_MAX_US", "3.0"))

# Consensus hypothesis stage (retina_geolocator.consensus.select_consensus):
# runs once at n≥3, ahead of the LM altitude sweep, and picks the subset of
# contributing nodes whose pairwise delay-ellipse intersections corroborate
# each other.  Bench measurement (association_bench.py --estimator
# consensus-refine vs lm, 5 seeds × default + dense scenes): n≥3 ghost
# solves ~3x down (13-16 vs ~46), position error slightly better, zero real
# tracks lost, LM-equivalent per-solve cost (consensus picks the subset,
# the existing LM solve still runs — this is not a solver replacement).
#   off    (default) — consensus never runs.
#   shadow — consensus runs and its selection is recorded on the history
#            record (consensus_meta), but the LM sees the unfiltered input;
#            for comparing the two without touching what publishes.
#   active — consensus's selection replaces the LM's input measurements
#            when it corroborates >= _CONSENSUS_MIN_NODES nodes; otherwise
#            (abstain, too few nodes, or an exception) it falls back to the
#            unfiltered input silently — see _consensus_select.
_CONSENSUS_MODE = os.getenv("SOLVER_CONSENSUS_MODE", "off").lower()
# Below this many corroborated nodes, a consensus selection is not trusted
# enough to filter on — the blanket rms_delay gate (and, if it still fails,
# trimming) is left to make the call on the unfiltered input instead.
_CONSENSUS_MIN_NODES = 3

# Node-trimming policy for the rms_delay gate at n≥4 (see above).  Iteratively
# drop the worst-residual node(s) and re-solve, down to a floor of
# _TRIM_MIN_NODES — below that the geometry is too thin to trust a trim's own
# residuals, and the blanket gate is left to make the call.
_TRIM_MAX_ROUNDS = 4
_TRIM_RESID_FACTOR = 1.5   # drop nodes with |res| > factor × rms_delay
_TRIM_MIN_NODES = 3

# Reject solver results whose RMS Doppler residual exceeds this value.
# Physics: for FM illuminators (fc ≈ 98–108 MHz, λ ≈ 2.8–3.1 m), the maximum
# bistatic Doppler for any real aircraft is 2 × v_max / λ ≈ 2 × 300 / 3.06 ≈ 196 Hz.
# For a true n-node association the solver fits velocity to n Doppler equations;
# with n=2 the system is exactly determined → rms_doppler ≈ 0 regardless.
# With n≥3 it is overdetermined → rms_doppler reflects measurement noise (< 20 Hz).
# False associations (delays/Dopplers from different aircraft) leave large, physically
# unrealisable Doppler residuals (observed: 248 Hz for confirmed false associations).
# Threshold at 200 Hz = max bistatic Doppler + 2% margin; only rejects impossible cases.
_SOLVER_RMS_DOPPLER_MAX_HZ = 200.0

# Reject n=2 solver results whose position moved more than this many km from
# the initial_guess supplied by the association layer.
#
# For n=2 (exactly-determined position), the LM solver can converge to the
# false bistatic ellipse intersection (the mirror point) instead of the true
# one.  The beam-coverage check above catches mirror points that land outside
# a node's detection beam; this check catches the remainder.
#
# The association grid step is 3 km, so the initial_guess is within ~3 km of
# the true aircraft position (the delay-residual-minimising grid point is
# always close to the real bistatic intersection).  A good solve therefore
# stays within a few km of the initial_guess.  Mirror points are typically
# 15–50 km from the true position, meaning they are ≥12 km from an
# initial_guess that was placed near the truth.
#
# Threshold: with the ADS-B position override in find_associations(),
# the initial_guess is within ~100 m of the true aircraft position.
# Displacement from initial_guess therefore approximates the position error.
# With σ_delay = 0.1 µs the displacement = GDOP × 0.1 × 0.3 km.
# 2.0 km → GDOP ≤ 67 (reasonable bistatic geometry).
# Mirror-point ghosts land 15–50 km away and are safely rejected.
#
# Originally N=2-only because that's where mirror-points dominate. Production
# /api/test/mlat-accuracy stats showed N=2 medians 4-6× tighter than N≥4
# medians even after dedup-by-aircraft (964 unique N=2 vs 68 unique N=4 — N=2
# median 0.47 km, N=4 median 2.48 km). Root cause: the gate was the only
# stage rejecting solver-vs-ADS-B disagreements, so N≥3 stats kept every bad
# convergence (wrong-frame association, local-minimum trap), while N=2 was
# pre-filtered. Generalising the gate to every N puts the comparison on the
# same footing — bad N≥3 solves are now rejected on the same criterion.
_MAX_DISPLACEMENT_KM = 2.0

# An n=2 solve is published only once its track pairing has justified itself.
#
# The residual gates above are structurally blind here: the solver fits
# [x, y, vx, vy, vz] with altitude pinned, 5 unknowns against 4 residuals, so
# rms_delay and rms_doppler go to ~0 for a cross pairing exactly as they do for
# a real target — which _SOLVER_RMS_DELAY_MAX_US already documents ("letting all
# n=2 results through").  Watching the phantom does not help either: its own
# motion is identically its Doppler-implied velocity, so it stays self-consistent
# for as long as both aircraft are tracked.
#
# What does discriminate is fitting one constant-velocity trajectory to the whole
# observation window of both single-node tracks — 4K measurements against 6
# unknowns instead of 4 against 6.  The associator does that and attaches
# chi2_per_dof; this gate reads it.  Solving still happens either way, so the
# position fix stays available to the display; only the *track* is withheld.
# Only meaningful when association is producing chi2 values at all; with
# the track path parked, requiring one would withhold every n=2 track.
_N2_REQUIRE_CONFIRMED = N2_TRACK_ASSOCIATION
_N2_CONFIRM_CHI2_MAX = N2_CONFIRM_CHI2_MAX

# The calibration age rule moved to config.constants.CAL_MAX_ADSB_AGE_S and is
# applied by services.calibration, which both recording sites now go through.
# It lived here while the frame path had its own, looser, unstated one.


def _sweep_altitudes(s_in: dict, node_cfgs: dict, solve_fn,
                     layers_km: list[float], metric: str) -> dict | None:
    """Try each altitude layer; return the result with lowest value of `metric`.

    Args:
        metric: Solver output key to minimise across layers.  Currently always
                'rms_delay' (used by n≥3 where the overdetermined system gives
                rms≈0 at the correct altitude).
    """
    base_guess = s_in["initial_guess"]
    best_result: dict | None = None
    best_rms = float("inf")
    last_exc: BaseException | None = None

    for alt_km in layers_km:
        s_try = dict(s_in)
        s_try["initial_guess"] = dict(base_guess, alt_km=alt_km)
        try:
            result = solve_fn(s_try, node_cfgs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        if result and result.get("success"):
            rms_raw = result.get(metric)
            rms = float("inf") if rms_raw is None else float(rms_raw)
            logging.debug(
                "altitude sweep: z=%.1fkm %s=%.3f (best so far=%.3f)",
                alt_km, metric, rms, best_rms,
            )
            if rms < best_rms:
                best_rms = rms
                best_result = result

    if best_result is None and last_exc is not None:
        raise last_exc

    return best_result


def _solve_best_altitude(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude sweep for n≥3: pick by minimum rms_delay.

    If the initial_guess already carries an ADS-B altitude (not one of the fixed
    grid layers), include it in the sweep so the correct exact altitude is tried.
    """
    ig_alt = s_in.get("initial_guess", {}).get("alt_km")
    if ig_alt is not None and ig_alt not in _SOLVER_ALT_LAYERS_KM:
        layers = sorted(set(_SOLVER_ALT_LAYERS_KM + [round(float(ig_alt), 3)]))
    else:
        layers = _SOLVER_ALT_LAYERS_KM
    return _sweep_altitudes(s_in, node_cfgs, solve_fn, layers, "rms_delay")


def _solve_best_altitude_n2(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude solve for n=2: use the initial_guess altitude from association.

    For n=2 the solver state [x, y, vx, vy, vz] with altitude fixed is:
    - Exactly determined by the 2 delay equations for (x, y)
    - Underdetermined for (vx, vy, vz): 2 Doppler equations, 3 unknowns

    Both rms_delay and rms_doppler are ≈0 at every altitude layer (the solver
    always finds a zero-residual solution within bounds).  Neither metric can
    discriminate altitude.

    The initial_guess.alt_km from association.py is set to the delay-residual
    weighted mean of all candidate altitudes in the group.  When the correct
    altitude layer has smaller delay residuals it is upweighted; when all layers
    tie (high altitude ambiguity), the mean falls back to ≈(7+9+11)/3 = 9 km,
    which covers the typical commercial aviation cruise band (7–12 km).
    """
    return solve_fn(s_in, node_cfgs)


def _filter_s_in_to_nodes(s_in: dict, survivors) -> dict:
    """Filter a solver input down to a node subset, rebuilding provenance.

    Shared rebuild idiom: filter ``measurements`` to the surviving node ids,
    recompute ``n_nodes``, and rebuild ``track_ids_by_node``/``track_ids``
    from it when the input carries per-node track provenance (else
    ``track_ids`` passes through unchanged — the supersession block reads
    ``s_in["track_ids"]`` into ``result["source_track_ids"]`` regardless of
    which caller narrowed the node set).  Used by both _trim_and_resolve
    (dropping the worst-residual node after a failed rms gate) and
    _consensus_select (dropping nodes consensus's cross-pair corroboration
    didn't support) — so provenance is identical-by-construction between
    the two.
    """
    survivors = set(survivors)
    measurements = s_in.get("measurements") or []
    s_next = dict(s_in)
    s_next["measurements"] = [
        m for m in measurements if m.get("node_id") in survivors
    ]
    s_next["n_nodes"] = len(
        {m.get("node_id") for m in s_next["measurements"]}
    )
    by_node = s_in.get("track_ids_by_node")
    if by_node:
        surviving_by_node = {
            nid: ids for nid, ids in by_node.items() if nid in survivors
        }
        s_next["track_ids_by_node"] = surviving_by_node
        s_next["track_ids"] = sorted(
            {tid for ids in surviving_by_node.values() for tid in ids}
        )
    return s_next


def _trim_and_resolve(
    s_in: dict, node_cfgs: dict, solve_fn, result: dict,
) -> tuple[dict, dict, dict | None]:
    """Drop the worst-residual node(s) and re-solve, down to _TRIM_MIN_NODES.

    Called only when a solve has failed the rms_delay gate at n≥4 and carries
    per-node residuals (see _SOLVER_RMS_DELAY_MAX_US).  A contaminated
    measurement inflates rms_delay without moving the Huber-fitted position,
    so re-solving on the survivors after dropping the offending node recovers
    a solve the blanket gate would otherwise discard outright.

    Returns (final_result, final_s_in, trim_meta).  trim_meta is None only
    when no round ever produced a successful re-solve — i.e. no trimming was
    actually performed — never when trimming ran but rms stayed high (that
    case still returns the (last-trimmed result, s_in, trim_meta) so the
    caller's gates see the best attempt reached, and the reject record still
    carries what was tried).  A re-solve that raises, returns None, or does
    not converge abandons trimming and returns whatever was reached in the
    previous round — a failed re-solve is never surfaced as the result.
    """
    trimmed_ids: list[str] = []
    trim_meta: dict | None = None
    pre_trim_rms_delay = result.get("rms_delay")
    pre_trim_n_nodes = result.get("n_nodes")

    for round_idx in range(_TRIM_MAX_ROUNDS):
        rms_delay = result.get("rms_delay") or 0
        if rms_delay <= _SOLVER_RMS_DELAY_MAX_US:
            break
        if (result.get("n_nodes") or 0) <= _TRIM_MIN_NODES:
            break
        residuals = result.get("per_node_delay_res_us")
        if not residuals:
            break

        threshold = _TRIM_RESID_FACTOR * rms_delay
        candidates = {nid for nid, res in residuals.items() if res > threshold}
        if not candidates:
            # Nothing clears the factor — the single worst node is still the
            # best lead available.
            candidates = {max(residuals, key=residuals.get)}

        survivors = [nid for nid in residuals if nid not in candidates]
        if len(survivors) < _TRIM_MIN_NODES:
            # Dropping every candidate would starve the fit below the floor:
            # keep the _TRIM_MIN_NODES lowest-residual nodes instead and drop
            # the rest, so a round never re-solves with fewer than that.
            survivors = sorted(residuals, key=residuals.get)[:_TRIM_MIN_NODES]
            candidates = {nid for nid in residuals if nid not in survivors}
        if not candidates:
            break

        s_next = _filter_s_in_to_nodes(s_in, survivors)

        try:
            new_result = _solve_best_altitude(s_next, node_cfgs, solve_fn)
        except Exception:
            logging.exception("Solver trim re-solve failed")
            break
        if not new_result or not new_result.get("success"):
            break

        trimmed_ids.extend(sorted(candidates))
        trim_meta = {
            "trimmed_node_ids": sorted(set(trimmed_ids)),
            "trim_rounds": round_idx + 1,
            "pre_trim_rms_delay": pre_trim_rms_delay,
            "pre_trim_n_nodes": pre_trim_n_nodes,
        }
        result, s_in = new_result, s_next

    return result, s_in, trim_meta


# ── Multi-epoch EWMA position smoother (all N) ───────────────────────────────
# This EWMA machinery is now the TRACK_SMOOTHER=ewma fallback; the default
# smoother is the Kalman filter in services/track_filter.py.
#
# Each multinode solve has position error σ_pos = GDOP × σ_delay.  By
# accumulating K successive solver positions for the same aircraft (identified
# by ICAO hex) and dead-reckoning earlier positions forward to the current
# solve time using ADS-B velocity, we average K independent noise realisations
# and reduce the effective σ_pos by 1/√K.
#
# K=3 frames (every ~40 s) reduces mean error by ~√3 = 1.73×.
#
# Originally gated on n_nodes == 2 only, but the math is N-agnostic — a single
# bad measurement at any N drags that frame's solve off by O(σ × GDOP), and
# multi-frame averaging on the same aircraft pulls the result back to the
# track-mean for the same √K reason.  Production stats showed the apparent
# "N=2 better than N=3" inversion was an artefact of the smoother only being
# applied to N=2; lifting the gate puts every solve on the same footing.
#
# Dead-reckoning prefers ADS-B ground-speed + track (independent of solver
# error) and falls back to the solved velocity when the target is dark.  Dark
# targets are the population where MLAT is the only position source — and they
# used to be the only population excluded from this smoothing (the old code
# required an ADS-B hex AND a live ADS-B entry, returning dark solves raw).
# History is keyed by the multinode track key so identity is shared with
# state.multinode_tracks by construction.

# Per-hex rolling buffer: hex → deque of (lat, lon, timestamp_s)
_MN_POS_HISTORY: dict[str, deque] = {}
_MN_POS_HISTORY_LOCK = threading.Lock()
_MN_HISTORY_K = 3   # number of past frames to average (including current)
_MN_DR_MAX_AGE_S = 160.0  # discard history entries older than 4 frame intervals
# Dict-level TTL sweep (the deques cap per-hex growth, but the dict itself
# grew one entry per distinct hex for the process lifetime — same shape as
# _TRACK_CLAIMS, which already expires).  Swept opportunistically on insert.
_MN_HISTORY_TTL_S = 600.0
_mn_history_last_sweep = 0.0


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    global _mn_history_last_sweep
    with _MN_POS_HISTORY_LOCK:
        _MN_POS_HISTORY.clear()
        _mn_history_last_sweep = 0.0
    with _TRACK_CLAIMS_LOCK:
        _TRACK_CLAIMS.clear()
    track_filter.reset()


def _sweep_mn_history(now_s: float) -> None:
    """Drop hexes whose newest sample is stale.  Caller holds the lock."""
    global _mn_history_last_sweep
    if now_s - _mn_history_last_sweep < _MN_HISTORY_TTL_S / 10:
        return
    _mn_history_last_sweep = now_s
    for h in [
        h for h, dq in _MN_POS_HISTORY.items()
        if not dq or now_s - dq[-1][2] > _MN_HISTORY_TTL_S
    ]:
        del _MN_POS_HISTORY[h]


def _ewma_smooth_track(result: dict, track_key: str, adsb_hex: str | None) -> dict:
    """Apply dead-reckoned multi-epoch averaging to a multinode solver result.

    Dead-reckon previous solve positions for the same track to the current
    solve timestamp, then return the simple mean of the dead-reckoned history
    and the current solve.  Thread-safe via lock.

    History is keyed by the multinode track key, so dark solves accumulate
    and average exactly like ADS-B-tagged ones.  The DR velocity prefers live
    ADS-B ground-speed/track (known independently of the solver) and falls
    back to the solved velocity for dark targets — association-seeded and
    CV-confirmed for n=2, and honest for n≥3 now that vz cannot absorb the
    Doppler misfit.
    """
    r_lat = result["lat"]
    r_lon = result["lon"]
    r_ts = result.get("timestamp_ms", 0) / 1000.0

    adsb = state.adsb_aircraft.get(adsb_hex) if adsb_hex else None
    if adsb:
        gs_knots = float(adsb.get("gs", 0) or 0)
        track_deg = float(adsb.get("track", 0) or 0)
        gs_kms = gs_knots * 0.514444 / 1000.0   # knots → km/s
        # Geographic track: 0° = North, 90° = East.
        vel_north_kms = gs_kms * math.cos(math.radians(track_deg))
        vel_east_kms  = gs_kms * math.sin(math.radians(track_deg))
    else:
        vel_east_kms = float(result.get("vel_east") or 0.0) / 1000.0
        vel_north_kms = float(result.get("vel_north") or 0.0) / 1000.0

    with _MN_POS_HISTORY_LOCK:
        _sweep_mn_history(r_ts)
        hist = _MN_POS_HISTORY.setdefault(track_key, deque(maxlen=_MN_HISTORY_K))

        # Dead-reckon each past position forward to the current solve time
        # and collect valid points (not too stale, not too far after dr).
        positions: list[tuple[float, float]] = [(r_lat, r_lon)]

        for prev_lat, prev_lon, prev_ts in hist:
            dt = r_ts - prev_ts
            if dt <= 0 or dt > _MN_DR_MAX_AGE_S:
                continue  # skip future or stale entries
            dr_lat, dr_lon = offset_latlon_m(
                prev_lat, prev_lon,
                east_m=vel_east_kms * 1000.0 * dt,
                north_m=vel_north_kms * 1000.0 * dt,
            )
            positions.append((dr_lat, dr_lon))

        # Push current position before averaging (so it is included next time).
        hist.append((r_lat, r_lon, r_ts))

    if len(positions) < 2:
        return result  # no usable history yet—return raw solve

    avg_lat = sum(p[0] for p in positions) / len(positions)
    avg_lon = sum(p[1] for p in positions) / len(positions)

    smoothed = dict(result)
    smoothed["lat"] = round(avg_lat, 6)
    smoothed["lon"] = round(avg_lon, 6)
    logging.debug(
        "EWMA: key=%s K=%d raw=(%.4f,%.4f) → smooth=(%.4f,%.4f)",
        track_key, len(positions), r_lat, r_lon, avg_lat, avg_lon,
    )
    return smoothed


# ── Multinode track identity ─────────────────────────────────────────────────
# An entry in state.multinode_tracks is ONE AIRCRAFT, not one solve.  Every node
# runs its own association round (ASSOC_MIN_INTERVAL_S), so the same aircraft is
# re-solved repeatedly from different nodes' frames; those solves must update a
# single entry.  Keying on the solve timestamp and latitude — the original
# behaviour — made every solve a distinct "aircraft": one target rendered as 4-6
# overlapping icons that drifted apart and then tripped the position-mismatch
# and supersonic anomaly detectors.

# Dark-target association gate.  retina_analytics.association uses the same 6 km
# (_MERGE_DIST_KM) for within-round clustering, so this keeps the cross-round
# gate no tighter than the one already applied per round.
_MN_ASSOC_MAX_DIST_KM = 6.0
# Never associate to an entry the map has already dropped (the 60 s expiry in
# frame_processor.build_combined_aircraft_json).
_MN_ASSOC_MAX_AGE_S = 60.0
# Guards the read-modify-write in _process_solver_item.  Solver workers run
# _N_SOLVER_WORKERS-way concurrently, and two threads associating the same
# aircraft at once would each miss the other's entry and mint two tracks — the
# very duplication this exists to prevent.
_MN_TRACKS_LOCK = threading.Lock()


def _collect_track_anomalies(s_in, result: dict) -> None:
    """Stamp contributing-track anomaly flags onto a multinode result.

    A multinode solve is built from per-node tracker tracks; if any of them
    flagged an anomaly (supersonic Doppler for dark targets, the ADS-B streams
    otherwise), the solved track should carry it — before this, multinode
    entries hardcoded is_anomalous False, so a dark anomalous target went
    quiet the moment it was solved.  Dark solves are restricted to the same
    physically-loud allowlist as arc-only tracks.  The caller latches the
    flags with the previous entry under _MN_TRACKS_LOCK so a one-frame tracker
    flag survives for the multinode track's lifetime.
    """
    track_ids = set(s_in.get("track_ids") or []) if isinstance(s_in, dict) else set()
    anom_types: set[str] = set()
    is_anom = False
    max_vel = 0.0
    if track_ids:
        for nid in result.get("contributing_node_ids", []):
            pipeline = state.node_pipelines.get(nid)
            if pipeline is None:
                continue
            try:
                tracks = list(pipeline.tracker.tracks)
            except Exception:
                continue
            for t in tracks:
                if getattr(t, "track_id", None) not in track_ids:
                    continue
                anom_types |= set(getattr(t, "anomaly_types", None) or ())
                is_anom = is_anom or bool(getattr(t, "is_anomalous", False))
                max_vel = max(max_vel, getattr(t, "max_velocity_ms", 0.0) or 0.0)
    if not result.get("adsb_hex"):
        anom_types &= ARC_ONLY_ANOMALY_ALLOWLIST
        is_anom = bool(anom_types)
    result["anomaly_types"] = sorted(anom_types)
    result["is_anomalous"] = is_anom or bool(anom_types)
    if max_vel:
        result["max_velocity_ms"] = round(max_vel, 1)


def multinode_key_decision(
    tracks: dict[str, dict],
    result: dict,
    adsb_hex: str | None,
    anchor_key: str | None,
    max_dist_km: float = _MN_ASSOC_MAX_DIST_KM,
    max_age_s: float = _MN_ASSOC_MAX_AGE_S,
) -> tuple[str, str]:
    """The keying rule itself, pure and clock-free — the multinode-track
    analogue of claim_decision.  Extracted so the offline bench measures the
    SHIPPED rule by construction (the same reason claim_decision is imported
    at association_bench.py's top-of-file import), and so _process_solver_item can observe
    which branch fired for the anchor counters below.

    Caller holds _MN_TRACKS_LOCK — it reads `tracks` and the caller writes
    back into it under the same lock.  Returns (key, how) with
    how in {"adsb", "anchor", "proximity", "minted"}.

    Order:
      1. ADS-B-tagged solves key on the transponder hex — unconditional, and
         this key is also the smoother's history key (_ewma_smooth_track),
         so the smoother and the track store agree by construction.
      2. Anchor honoring.  anchor_key is set only by an anchored solver
         input (top-down claiming, ASSOC_CLAIM_MODE=active) — mn-dark-*,
         still live in `tracks`, and within max_dist_km of THIS solve's own
         result.  That distance check is what closes the
         consensus-anchored-displacement edge case: the n>=3 displacement
         gate at :~1320 already re-anchors to the consensus centroid rather
         than the claim guess for a consensus-selected solve, so an anchor
         whose claim guess was wrong but whose consensus-corrected result
         still landed near the claimed track is legitimate, while one that
         converged somewhere else entirely is not honored just because a
         claim was attempted.
      3. Existing DR proximity scan, moved verbatim from the pre-claiming
         _multinode_track_key: only dark tracks are claimable — an untagged
         solve must never steal the identity of an ADS-B-tagged aircraft
         that happens to be nearby — dead-reckoned forward so a fast target
         is not rejected purely for having moved since its last solve.
      4. Mint.  This key only needs to be unique at birth; every later solve
         associates to it above (by proximity, or by anchor once a claim
         forms), so it stays stable.
    """
    if adsb_hex:
        return f"mn-adsb-{adsb_hex}", "adsb"

    lat, lon = result["lat"], result["lon"]

    if anchor_key and anchor_key.startswith("mn-dark-") and anchor_key in tracks:
        anchor = tracks[anchor_key]
        a_lat, a_lon = anchor.get("lat"), anchor.get("lon")
        if (a_lat is not None and a_lon is not None
                and _haversine_km(lat, lon, a_lat, a_lon) <= max_dist_km):
            return anchor_key, "anchor"

    ts_s = result.get("timestamp_ms", 0) / 1000.0
    best_key, best_dist = None, max_dist_km

    for key, prev in tracks.items():
        # Only dark tracks are claimable; an untagged solve must never steal the
        # identity of an ADS-B-tagged aircraft that happens to be nearby.
        if not key.startswith("mn-dark-"):
            continue
        dt = ts_s - prev.get("timestamp_ms", 0) / 1000.0
        if not (0.0 <= dt <= max_age_s):
            continue
        p_lat, p_lon = prev.get("lat"), prev.get("lon")
        if p_lat is None or p_lon is None:
            continue
        # Dead-reckon the existing track forward before measuring, so a fast
        # target is not rejected purely for having moved since its last solve.
        p_lat, p_lon = offset_latlon_m(
            p_lat, p_lon,
            east_m=prev.get("vel_east", 0.0) * dt,
            north_m=prev.get("vel_north", 0.0) * dt,
        )
        d = _haversine_km(lat, lon, p_lat, p_lon)
        if d < best_dist:
            best_key, best_dist = key, d

    if best_key is not None:
        return best_key, "proximity"
    # No claimant — a genuinely new target.
    return f"mn-dark-{result.get('timestamp_ms', 0)}-{lat:.3f}-{lon:.3f}", "minted"


def _multinode_track_key(result: dict, adsb_hex: str | None,
                         anchor_key: str | None = None) -> str:
    """Thin wrapper over multinode_key_decision against state.multinode_tracks.

    Call under _MN_TRACKS_LOCK — multinode_key_decision reads
    state.multinode_tracks and the caller writes back into it.
    """
    key, _how = multinode_key_decision(state.multinode_tracks, result, adsb_hex, anchor_key)
    return key


# Maximum age (seconds) of a solver queue item before it is discarded without
# solving.  Items older than this are already stale — the multinode_tracks
# expiry is 60 s, and a solve itself can take a few seconds — so spending CPU
# on them can never produce a visible result.  Raising this number allows a
# deeper backlog but increases latency; lowering it drops items too aggressively.
_SOLVER_MAX_QUEUE_AGE_S = 45.0


# Which single-node track pair currently owns a published n=2 track, and how
# well it fitted.  One track is one aircraft, so two pairings sharing a track
# are mutually exclusive; the better chi2 wins and the loser is withheld.
#
# On the frame path this was a sort within one association round.  Here the
# pairings arrive as separate queue items, possibly on different worker
# threads, so ownership is a claim against shared state instead — same rule,
# different shape.  Entries expire on the same window the map does, so a claim
# cannot outlive the track it was made for.
_TRACK_CLAIMS: dict[str, tuple[float, float]] = {}   # track_id → (chi2, claimed_at)
_TRACK_CLAIMS_LOCK = threading.Lock()
_TRACK_CLAIM_TTL_S = 60.0


def _claim_track_pair(s_in: dict, chi2: float) -> bool:
    """Take ownership of both single-node tracks, or refuse if outbid.

    The held score is a **best-ever high-water mark**, not the holder's current
    score, and a refusal deliberately does not refresh it.  Two consequences,
    both measured rather than assumed:

    1. A pairing can be refused by its *own* earlier claim.  chi2 is refitted
       every association round over a growing epoch set, so a stable winning
       pairing's score wanders — median drift +0.11 between rounds — and
       upward drift loses to its own high-water mark until the TTL expires.
       Self-lockouts are roughly 30% of all refusals (707 of 2,403 pooled
       over 6 seeds); the rest are genuine competitor losses.

    2. It is nonetheless the better behaviour.  Letting a pairing renew its own
       claim at a worse score raises the bar competitors must beat, so tracks
       change hands more often and every hand-over publishes another track.
       Measured offline, blind, dual/vhf, 6 seeds, pooled AND paired by seed
       (association_bench.py --claim-policy self-refresh): never better on any
       seed, worse on 5 of 6, +3.2 pooled points of track ghost rate
       (73.4% -> 76.6%) and +9.5 at n=2-only (76.2% -> 85.7%; false n=2
       tracks 16 -> 30), at no gain in real tracks (29 both ways).
       Competitor refusals rose 1,696 -> 1,854 — the churn showing up
       directly.

    So the high-water mark is hysteresis: a pairing must keep fitting at least
    as well as its own best to stay published, and a pairing whose fit is
    degrading is one whose accumulated evidence is turning against it.  Keep it,
    and do not "fix" the self-refusal without re-running that sweep.

    Against no arbitration at all (--claim-policy off) the claim is worth
    8.7 pooled points of track ghost rate (82.1% -> 73.4%), better or tied
    on every seed, and cuts false n=2-only tracks 63 -> 16.

    (Earlier revisions of this docstring quoted "refusals 1 -> 21" and
    "worth 3.7 points": both were read off a --repeat run that printed only
    the LAST seed's counters — see Result.merge in association_bench.py.
    The numbers above are pooled across all six seeds, measured after the
    stage-1 simulation fixes changed the scenes.)
    """
    pairs = s_in.get("track_pair_ids") or []
    if not pairs:
        return True          # detection-level input: nothing to arbitrate
    # Note this claims only the first pair of a cluster: format_track_pairs_for_solver
    # truncates track_pair_ids to [:1], so a cluster spanning three tracks leaves
    # the third unclaimed.  s_in["track_ids"] carries the full set if that is
    # ever worth closing — see --claim-policy all-tracks.
    track_ids = [tid for pair in pairs for tid in pair]
    with _TRACK_CLAIMS_LOCK:
        return claim_decision(
            _TRACK_CLAIMS, track_ids, chi2, time.time(), _TRACK_CLAIM_TTL_S
        )


def claim_decision(
    claims: dict[str, tuple[float, float]],
    track_ids: list[str],
    chi2: float,
    now: float,
    ttl_s: float,
) -> bool:
    """The claim rule itself, pure and clock-free: expire, compare, record.

    Extracted so the offline bench measures the SHIPPED rule by construction
    (association_bench.DeferredN2Gate used to carry its own copy — the exact
    drift the bench exists to rule out).  Caller holds whatever lock guards
    `claims`; production passes _TRACK_CLAIMS under _TRACK_CLAIMS_LOCK, the
    bench passes its own dict on simulated time.
    """
    for tid, (_held_chi2, held_at) in list(claims.items()):
        if now - held_at > ttl_s:
            del claims[tid]
    for tid in track_ids:
        held = claims.get(tid)
        if held is not None and held[0] < chi2:
            return False
    for tid in track_ids:
        claims[tid] = (chi2, now)
    return True


def _resolve_cv_fit(s_in: dict, node_cfgs) -> dict | None:
    """Constant-velocity fit over the whole observation window, cached.

    Fits one constant-velocity trajectory to the associated track pairing's
    full epoch history — an ~86 ms LM solve.  Association runs inside the
    frame worker, so doing it there is frame latency: measured on staging at
    92% frame-queue depth with the processor 21 s behind a 6 frame/s feed.
    This worker already has its own threads and a queue with a staleness
    drop, which is exactly the place for it — so association hands over the
    epochs and the fit happens on this side.

    Cached on the input as ``s_in["_cv_fit"]`` so a caller reached twice for
    the same solve (the n=2 confirmation gate, then velocity adoption) pays
    for the pool call once.  ``chi2_per_dof``/``n_epochs`` are cached onto
    ``s_in`` exactly as before this fit dict existed, so association-fitted
    inputs (chi2_per_dof arrives already set) keep short-circuiting through
    _resolve_n2_chi2 without ever reaching here — which is also the one case
    this function cannot reconstruct a fit dict for: with the fit already
    done inline, no epochs survive on the input to refit from.
    """
    cached = s_in.get("_cv_fit")
    if cached is not None:
        return cached
    epochs = s_in.get("cv_epochs")
    if not epochs or not isinstance(node_cfgs, dict):
        return None
    try:
        from retina_geolocator.multinode_solver import fit_constant_velocity
        fit = _pool_call(
            fit_constant_velocity,
            {
                "initial_guess": s_in.get("initial_guess"),
                "initial_velocity": s_in.get("initial_velocity"),
                "epochs": epochs,
                "timestamp_ms": s_in.get("timestamp_ms", 0),
            },
            node_cfgs,
        )
    except Exception:
        logging.exception("constant-velocity fit failed")
        return None
    if not fit or not fit.get("success"):
        return None
    s_in["_cv_fit"] = fit
    # Cache it so a retry of the same input does not refit.
    s_in["chi2_per_dof"] = fit["chi2_per_dof"]
    s_in["n_epochs"] = fit["n_epochs"]
    return fit


def _resolve_n2_chi2(s_in: dict, node_cfgs) -> float | None:
    """chi2/dof for an n=2 pairing, fitting here if association deferred it.

    Association may fit inline (the offline bench does, having no queue), in
    which case chi2_per_dof arrives already set and this is a no-op.
    """
    chi2 = s_in.get("chi2_per_dof")
    if chi2 is not None:
        return chi2
    fit = _resolve_cv_fit(s_in, node_cfgs)
    return fit["chi2_per_dof"] if fit else None


# Public alias: the offline bench resolves chi2 through the same code path the
# worker uses, rather than carrying a copy of the fit plumbing.
resolve_n2_chi2 = _resolve_n2_chi2


# ── Per-solve history (debug) ────────────────────────────────────────────────
# Every solver outcome — published or gate-rejected — is appended to
# state.mlat_solve_history so /api/test/mlat-history can decompose a bad map
# marker into raw solves, smoothing, dead-reckoning, GT binding and gate
# rejections after the fact.  Retention ~30 min (age-pruned on write, hard
# capped by the deque's maxlen).
_MLAT_HISTORY_MAX_AGE_MS = 35 * 60 * 1000
# GT trail points are pushed every 2 s; beyond this gap the trail is stale
# enough that a stamp would mislead more than inform.
_MLAT_HISTORY_GT_MAX_DT_S = 90.0


_GT_NO_MATCH = {
    "gt_hex": None, "gt_error_km": None, "gt_lat": None, "gt_lon": None,
    "gt_speed_ms": None, "gt_heading_deg": None,
}


def _trail_velocity(trail, pt) -> tuple[float | None, float | None]:
    """Ground-truth speed/heading AT ``pt``, from the temporally nearest
    other point in the same trail.

    ``pt`` is excluded by identity, not value, so a duplicate-position point
    elsewhere in the trail is not mistaken for it.  |dt| > 0.1 s avoids
    dividing by a near-zero interval; the two points are then ordered by
    timestamp so the heading is the direction of travel, not its reverse.
    Returns (None, None) when the trail has no other usable point.
    """
    other = None
    best_dt = None
    for p in trail:
        if p is pt:
            continue
        dt = abs(p[3] - pt[3])
        if dt <= 0.1:
            continue
        if best_dt is None or dt < best_dt:
            best_dt, other = dt, p
    if other is None:
        return None, None
    earlier, later = (pt, other) if pt[3] <= other[3] else (other, pt)
    dt_s = later[3] - earlier[3]
    if dt_s <= 0:
        return None, None
    dist_km = _haversine_km(earlier[0], earlier[1], later[0], later[1])
    speed_ms = dist_km * 1000.0 / dt_s
    heading = bearing_deg(earlier[0], earlier[1], later[0], later[1])
    return speed_ms, heading


def _nearest_gt(lat: float, lon: float, ts_s: float) -> dict:
    """Nearest ground-truth trail point at solve time, no distance threshold.

    Frozen into each history record so "how far was this solve really off,
    and from whom" survives later display dead-reckoning and GT re-binding.
    Timestamp-closest point per trail — the same rule the MLAT verification
    matcher uses.  gt_speed_ms/gt_heading_deg are truth velocity AT that
    point (see _trail_velocity), falling back to ground_truth_meta when the
    trail alone can't derive one (e.g. a single-point trail).
    """
    best_hex = best_km = best_pt = None
    best_trail = ()
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        # tuple() snapshots the deque in one C call; iterating the live deque
        # here (min below, _trail_velocity after) raises "deque mutated during
        # iteration" when the sim-ingest thread appends a trail point — both
        # solver workers died exactly that way on staging (2026-08-08).
        trail = tuple(trail)
        if not trail:
            continue
        pt = min(trail, key=lambda p: abs(p[3] - ts_s))
        if abs(pt[3] - ts_s) > _MLAT_HISTORY_GT_MAX_DT_S:
            continue
        d = _haversine_km(lat, lon, pt[0], pt[1])
        if best_km is None or d < best_km:
            best_hex, best_km, best_pt, best_trail = gt_hex, d, pt, trail
    if best_hex is None:
        return dict(_GT_NO_MATCH)
    gt_speed_ms, gt_heading_deg = _trail_velocity(best_trail, best_pt)
    if gt_speed_ms is None:
        meta = state.ground_truth_meta.get(best_hex) or {}
        gt_speed_ms = meta.get("speed_ms")
        gt_heading_deg = meta.get("heading")
    return {
        "gt_hex": best_hex,
        "gt_error_km": round(best_km, 3),
        "gt_lat": round(best_pt[0], 6),
        "gt_lon": round(best_pt[1], 6),
        "gt_speed_ms": round(gt_speed_ms, 1) if gt_speed_ms is not None else None,
        "gt_heading_deg": (
            round(gt_heading_deg, 1) if gt_heading_deg is not None else None
        ),
    }


def _record_solve_history(
    outcome: str,
    s_in,
    result: dict | None,
    *,
    solve_key: str | None = None,
    raw_lat: float | None = None,
    raw_lon: float | None = None,
    displacement_km: float | None = None,
    chi2_per_dof: float | None = None,
    extra: dict | None = None,
) -> None:
    """Append one solve outcome to state.mlat_solve_history.

    ``raw_lat/raw_lon`` is the solver's own position before EWMA smoothing;
    for published records ``lat/lon`` additionally carries the smoothed
    position actually stored in multinode_tracks.  Rejected solves have no
    track key (it is minted after the gates), so ``solver_hex`` is None for
    them and lookup by map ID returns published records plus nearby rejects.

    ``extra`` merges caller-supplied fields (trim metadata, beam-rejection
    diagnostics) into the record.  Applied before the GT stamp so it can
    never clobber gt_hex/gt_error_km/gt_lat/gt_lon.
    """
    r = result if isinstance(result, dict) else {}
    s = s_in if isinstance(s_in, dict) else {}
    now_ms = int(time.time() * 1000)
    if raw_lat is None:
        raw_lat = r.get("lat")
        raw_lon = r.get("lon")
    ig = s.get("initial_guess") or {}
    if (
        displacement_km is None
        and raw_lat is not None
        and ig.get("lat") and ig.get("lon")
    ):
        displacement_km = _haversine_km(
            float(ig["lat"]), float(ig["lon"]), float(raw_lat), float(raw_lon)
        )
    rec = {
        "ts_ms": now_ms,
        "measurement_ts_ms": int(r.get("timestamp_ms") or s.get("timestamp_ms") or 0),
        "outcome": outcome,
        "solve_key": solve_key,
        "solver_hex": multinode_hex_from_key(solve_key) if solve_key else None,
        "n_nodes": int(r.get("n_nodes") or s.get("n_nodes") or 0),
        "contributing_node_ids": list(r.get("contributing_node_ids") or []),
        "adsb_hex": s.get("adsb_hex"),
        # Set only on an anchored solver input (top-down claiming, active
        # mode) — present on rejects too, not just "published", so the
        # windowed fragmentation breakdown can see what fraction of ALL
        # attempts (not just successful ones) were anchor-carrying.
        "anchor_key": s.get("anchor_key"),
        "raw_lat": round(float(raw_lat), 6) if raw_lat is not None else None,
        "raw_lon": round(float(raw_lon), 6) if raw_lon is not None else None,
        "lat": round(float(r["lat"]), 6) if outcome == "published" else None,
        "lon": round(float(r["lon"]), 6) if outcome == "published" else None,
        "alt_m": round(float(r.get("alt_m") or 0), 0),
        "vel_east": round(float(r.get("vel_east") or 0), 1),
        "vel_north": round(float(r.get("vel_north") or 0), 1),
        "rms_delay": round(float(r.get("rms_delay") or 0), 3),
        "rms_doppler": round(float(r.get("rms_doppler") or 0), 2),
        "chi2_per_dof": round(float(chi2_per_dof), 3) if chi2_per_dof is not None else None,
        "guess_lat": round(float(ig["lat"]), 6) if ig.get("lat") else None,
        "guess_lon": round(float(ig["lon"]), 6) if ig.get("lon") else None,
        "guess_alt_km": ig.get("alt_km"),
        "displacement_km": round(displacement_km, 3) if displacement_km is not None else None,
        "solve_count": r.get("solve_count"),
        "source_track_ids": list(r.get("source_track_ids") or []),
        "vel_source": r.get("vel_source"),
        "solver_vel_east": (
            round(float(r["solver_vel_east"]), 1)
            if r.get("solver_vel_east") is not None else None
        ),
        "solver_vel_north": (
            round(float(r["solver_vel_north"]), 1)
            if r.get("solver_vel_north") is not None else None
        ),
    }
    if extra:
        rec.update(extra)
    if raw_lat is not None and raw_lon is not None:
        meas_ts_s = (rec["measurement_ts_ms"] or now_ms) / 1000.0
        rec.update(_nearest_gt(float(raw_lat), float(raw_lon), meas_ts_s))
    else:
        rec.update(_GT_NO_MATCH)
    # Direction error: how far off the solved velocity vector points from
    # ground truth at the matched trail point.  Heading is undefined near
    # hover, so this (and the vector-norm error below) only assert anything
    # once truth is actually moving and the solve has a direction to compare.
    gt_speed_ms = rec.get("gt_speed_ms")
    gt_heading_deg = rec.get("gt_heading_deg")
    ve, vn = rec["vel_east"], rec["vel_north"]
    if (
        gt_heading_deg is not None
        and gt_speed_ms and gt_speed_ms >= 20.0
        and math.hypot(ve, vn) > 1.0
    ):
        solved_heading = math.degrees(math.atan2(ve, vn)) % 360
        rec["heading_err_deg"] = round(
            abs((solved_heading - gt_heading_deg + 180.0) % 360.0 - 180.0), 1
        )
    else:
        rec["heading_err_deg"] = None
    if gt_speed_ms is not None and gt_heading_deg is not None:
        gt_ve = gt_speed_ms * math.sin(math.radians(gt_heading_deg))
        gt_vn = gt_speed_ms * math.cos(math.radians(gt_heading_deg))
        rec["vel_err_ms"] = round(math.hypot(ve - gt_ve, vn - gt_vn), 1)
    else:
        rec["vel_err_ms"] = None
    buf = state.mlat_solve_history
    buf.append(rec)
    # Age-prune from the left; append/popleft are atomic, and a racing second
    # writer at worst re-checks an already-fresh head.
    try:
        while buf and now_ms - buf[0]["ts_ms"] > _MLAT_HISTORY_MAX_AGE_MS:
            buf.popleft()
    except IndexError:
        pass


def _pool_select_consensus(s_in, node_cfgs):
    """select_consensus via the process pool (inline when no pool exists).

    Mirrors _pool_solve_multinode below, but has to live here rather than
    beside it: it is _process_solver_item's select_fn default, and a
    default argument value is resolved when the ``def`` executes, so it
    must already be bound by the time that line is reached.
    """
    from retina_geolocator.consensus import select_consensus
    return _pool_call(select_consensus, s_in, node_cfgs)


def _consensus_select(
    s_in: dict, node_cfgs: dict, select_fn,
) -> tuple[dict, dict | None]:
    """Run the consensus hypothesis stage once, ahead of the LM altitude
    sweep, and decide whether to act on its selection.

    Called only at n>=3 with an initial_guess and _CONSENSUS_MODE != "off"
    (see the dispatch in _process_solver_item) — n=2 and detection-level
    (no-guess) inputs never reach this.  select_fn is select_consensus by
    default, routed through the spawn pool exactly like solve_fn; tests
    substitute a stub.

    Every non-selecting outcome (an exception, an abstain, or too few
    corroborated nodes) returns the ORIGINAL s_in unchanged — a consensus
    failure must never block a solve the unfiltered input could still
    produce.  initial_guess is never touched here either way: the
    displacement gate downstream measures the LM's own solve against the
    association guess regardless of what consensus decided, and
    centroid_offset_km in the meta is the observability channel for how
    often the two disagree.

    Returns (s_in_for_solve, consensus_meta).  consensus_meta is always a
    dict (never None) once this runs, carrying at least "outcome" —
    _process_solver_item threads it into every history record for this
    item, published or rejected.
    """
    try:
        selection = select_fn(s_in, node_cfgs)
    except Exception:
        logging.exception("Consensus selection failed")
        state.bump_counter("solver_consensus_fallback")
        return s_in, {"outcome": "fallback_error"}

    if selection is None:
        state.bump_counter("solver_consensus_fallback")
        return s_in, {"outcome": "fallback_abstained"}

    node_ids = selection.get("node_ids") or []
    if len(node_ids) < _CONSENSUS_MIN_NODES:
        meta = dict(selection)
        meta["outcome"] = "fallback_small"
        state.bump_counter("solver_consensus_fallback")
        return s_in, meta

    input_node_ids = selection.get("input_node_ids") or []
    meta = dict(selection)
    meta["input_node_ids"] = input_node_ids
    meta["selected_node_ids"] = sorted(node_ids)
    meta["dropped_node_ids"] = sorted(set(input_node_ids) - set(node_ids))

    if _CONSENSUS_MODE == "shadow":
        meta["outcome"] = "shadow_selected"
        state.bump_counter("solver_consensus_shadow")
        return s_in, meta

    meta["outcome"] = "selected"
    state.bump_counter("solver_consensus_selected")
    if meta["dropped_node_ids"]:
        state.bump_counter("solver_consensus_filtered")
    return _filter_s_in_to_nodes(s_in, node_ids), meta


def fov_gate_verdict(fov, n_nodes: int, brg: float, dist_km: float,
                     range_rule_pass: bool) -> bool:
    """The FOV_MODE beam-gate rule for one contributing node, pure over an
    already-resolved fov (EmpiricalCoverageState) and today's range-rule
    outcome.  Shared by the live solver gate below and the offline bench's
    --fov leg (association_bench.py) so the bench measures the rule that
    actually ships, not a reimplementation of it.

    n == 2: fov.contains alone.  This REPLACES today's rules entirely — the
    autopsy behind this feature found the invented broadside-+90 azimuth
    (geo.node_beam_params, for a node with no declared aim) killed 66% of
    good real-data n=2 solves, and the wedge test is the n=2 mirror-
    disambiguation gate that invented azimuth feeds.

    n >= 3: range_rule_pass OR fov.contains — FOV only ever WIDENS here.  A
    separate autopsy showed the bearing wedge is structurally wrong at n>=3
    (a node's wedge constrains where a detection was MADE, not where a
    multi-epoch track is NOW), so this must never cost a solve the range
    rule already keeps; it can only rescue one the range rule alone would
    have killed.
    """
    fov_pass = fov.contains(brg, dist_km)
    if n_nodes == 2:
        return fov_pass
    return range_rule_pass or fov_pass


def _process_solver_item(item: tuple, solve_fn, select_fn=_pool_select_consensus) -> dict | None:
    """Process a single solver queue entry. Returns the solver result (or None).

    Extracted from the worker loop so the success/failure/latency bookkeeping
    can be unit-tested without spinning up daemon threads.

    select_fn is the consensus hypothesis stage (_pool_select_consensus by
    default; tests substitute a stub).  It only ever runs at n>=3 with an
    initial_guess and _CONSENSUS_MODE != "off" — n=2 (mirror-disambiguation
    is the displacement/beam gates' job, not consensus's) and detection-level
    inputs (no initial_guess to pin an altitude with) never call it.
    """
    s_in, node_cfgs = item[0], item[1]
    enqueued_at: float | None = item[2] if len(item) > 2 else None
    # Discard items that have been waiting too long in the queue.  By the time
    # they are solved, the result's timestamp_ms will be > 60 s old and the
    # entry will be immediately pruned from multinode_tracks — wasting CPU.
    age_s = time.time() - enqueued_at if enqueued_at is not None else 0.0
    if enqueued_at is not None and age_s > _SOLVER_MAX_QUEUE_AGE_S:
        state.bump_counter("solver_stale_drops")
        logging.debug(
            "Solver: dropping stale item (age=%.1fs > %.1fs, n_nodes=%d)",
            age_s,
            _SOLVER_MAX_QUEUE_AGE_S,
            s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0,
        )
        return None
    n_nodes = s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0
    consensus_meta: dict | None = None
    try:
        if "initial_guess" not in s_in:
            result = solve_fn(s_in, node_cfgs)
        elif n_nodes >= 3:
            if _CONSENSUS_MODE != "off":
                s_in, consensus_meta = _consensus_select(s_in, node_cfgs, select_fn)
                n_nodes = s_in.get("n_nodes", n_nodes)
            result = _solve_best_altitude(s_in, node_cfgs, solve_fn)
        else:
            result = _solve_best_altitude_n2(s_in, node_cfgs, solve_fn)
    except Exception:
        state.task_error_counts["solver"] += 1
        state.bump_counter("solver_failures")
        state.bump_counter("solver_fail_exception")
        logging.exception("Multinode solver failed")
        result = None
    if result and result.get("success"):
        # Trim before the rms_delay gate reads it: a contaminated measurement
        # inflates rms_delay without moving the Huber-fitted position (see
        # _SOLVER_RMS_DELAY_MAX_US), so at n≥4 a re-solve on the surviving
        # nodes can recover a solve the blanket gate would otherwise sink.
        # The rest of the gate stack below runs on whatever this reaches —
        # the trimmed result/s_in on a successful trim, the original
        # otherwise — unchanged.  A consensus-filtered n=3 input skips this
        # (below the n≥4 floor) by construction, which is intended: consensus
        # already chose the subset it trusts.
        trim_meta: dict | None = None
        if (
            "initial_guess" in s_in
            and result.get("n_nodes", 0) >= 4
            and (result.get("rms_delay") or 0) > _SOLVER_RMS_DELAY_MAX_US
            and result.get("per_node_delay_res_us")
        ):
            result, s_in, trim_meta = _trim_and_resolve(
                s_in, node_cfgs, solve_fn, result
            )
            n_nodes = result.get("n_nodes", n_nodes)

        # Built once and threaded through every history record below
        # (published or rejected) so a bad map marker can be traced back to
        # both what trimming tried and what consensus selected.
        _extra: dict | None = dict(trim_meta) if trim_meta else {}
        if consensus_meta is not None:
            _extra["consensus_meta"] = consensus_meta
        _extra = _extra or None

        rms_delay = result.get("rms_delay", 0) or 0
        if rms_delay > _SOLVER_RMS_DELAY_MAX_US:
            logging.debug(
                "Solver result rejected: rms_delay=%.1f µs > %.1f µs threshold "
                "(n_nodes=%d, lat=%.3f, lon=%.3f)",
                rms_delay, _SOLVER_RMS_DELAY_MAX_US,
                result.get("n_nodes", 0), result.get("lat", 0), result.get("lon", 0),
            )
            state.bump_counter("solver_failures")
            state.bump_counter("solver_fail_rms_delay")
            _record_solve_history(
                "rejected_rms_delay", s_in, result, extra=_extra,
            )
            return result
        rms_doppler = result.get("rms_doppler", 0) or 0
        if rms_doppler > _SOLVER_RMS_DOPPLER_MAX_HZ:
            logging.debug(
                "Solver result rejected: rms_doppler=%.1f Hz > %.1f Hz threshold "
                "(n_nodes=%d, lat=%.3f, lon=%.3f) — physically unrealisable Doppler",
                rms_doppler, _SOLVER_RMS_DOPPLER_MAX_HZ,
                result.get("n_nodes", 0), result.get("lat", 0), result.get("lon", 0),
            )
            state.bump_counter("solver_failures")
            state.bump_counter("solver_fail_rms_doppler")
            _record_solve_history(
                "rejected_rms_doppler", s_in, result, extra=_extra,
            )
            return result
        # Beam gate: range and bearing are two different physical claims, and
        # a fresh 35-min instrumentation autopsy (474 failing-node
        # evaluations) showed they do not deserve the same treatment at
        # every N.
        #
        # RANGE — differential-range ceiling when the node declares a
        # bistatic limit, else the monostatic circle.  Applied at every N and
        # never false-fired: 0 of 474 failing-node evaluations were bad
        # range rejects.  Mirrors point_in_beam's range half exactly.
        #
        # BEARING — the beam-wedge azimuth test.  At n>=3 it killed 105 good
        # solves (<3 km from truth) to stop 8 bad ones (>8 km) — and half of
        # those 8 the displacement gate below catches anyway.  Worse: 316 of
        # the 474 failing-node evaluations had the TRUE detected target
        # outside the gate's wedge even under the most favorable azimuth
        # candidate.  The reason is structural, not a tuning problem — for
        # multi-epoch track association a node's wedge constrains where its
        # detection was MADE, not where the target is *now*, and by
        # publication time it may well have flown out of that beam.  So the
        # bearing test is now n=2-only, where it is the documented
        # mirror-disambiguation gate (92 bad vs 9 good rejected) and stays
        # exactly as before.
        #
        # FOV_MODE — a further autopsy of the SAME 474 evaluations found the
        # bearing wedge's azimuth was itself often invented: a node with no
        # declared aim and a known TX falls back to broadside+90 (see
        # geo.node_beam_params), and that guess alone killed 66% of good
        # real-data n=2 solves.  The learned FOV (empirical_coverage.py)
        # replaces the invented wedge with what the node has actually been
        # seen to detect, broadening fast off ADS-B and shrinking only on
        # sustained negative evidence.  active: per node with a learned fov,
        # fov_gate_verdict (above) decides instead of range+bearing above —
        # n=2 replaces both rules, n>=3 only ever widens.  shadow: today's
        # rules keep deciding; the verdict is only counted/tagged.  off:
        # untouched — no fov is ever looked up.  A node with no learned fov
        # yet (state.node_analytics.learned_fov_for returns None) falls back
        # to today's rules verbatim regardless of mode.
        #
        # Evaluates every contributing node (not just the first failure) and
        # records per-node geometry for each one that fails, tagged with
        # which rule tripped ("range", "bearing" or "fov"; "range" when both
        # range and bearing do — the physical impossibility dominates).
        contributing_ids = result.get("contributing_node_ids", [])
        beam_failures: list[dict] = []
        _n2_bearing_check = result.get("n_nodes") == 2
        _fov_mode = state.FOV_MODE
        _fov_n_nodes = result.get("n_nodes")
        # Per-node {node_id, today_pass, fov_verdict} records in shadow mode
        # only — attached to the rejected_beam history entry below.  Shadow's
        # whole purpose is this comparison, so it must be visible after the
        # fact, not just folded into a counter.
        _fov_shadow_records: list[dict] = []
        if contributing_ids and isinstance(node_cfgs, dict):
            for nid in contributing_ids:
                cfg = node_cfgs.get(nid)
                if not cfg:
                    continue
                p = node_beam_params(cfg)
                rx_lat, rx_lon = p["rx_lat"], p["rx_lon"]
                range_km = _haversine_km(rx_lat, rx_lon, result["lat"], result["lon"])

                raw_bistatic_km = None
                if p["max_bistatic_range_km"] and p["tx_lat"] is not None:
                    raw_bistatic_km = bistatic_differential_km(
                        p["tx_lat"], p["tx_lon"], rx_lat, rx_lon,
                        result["lat"], result["lon"],
                    )
                    range_fail = raw_bistatic_km > p["max_bistatic_range_km"]
                else:
                    range_fail = range_km > p["max_range_km"]
                bistatic_km = (
                    round(raw_bistatic_km, 1) if raw_bistatic_km is not None else None
                )

                bearing_off_deg = None
                bearing_fail = False
                if p["beam_azimuth_deg"] is not None:
                    brg = bearing_deg(rx_lat, rx_lon, result["lat"], result["lon"])
                    raw_offset = abs((brg - p["beam_azimuth_deg"] + 180.0) % 360.0 - 180.0)
                    bearing_off_deg = round(raw_offset, 1)
                    bearing_fail = (
                        _n2_bearing_check and raw_offset > p["beam_width_deg"] / 2.0
                    )

                node_fail = range_fail or bearing_fail
                fov_rule: str | None = None
                fov_state: str | None = None
                fov_limit_km: float | None = None

                if _fov_mode != "off":
                    fov = state.node_analytics.learned_fov_for(nid)
                    if fov is not None:
                        brg_for_fov = bearing_deg(rx_lat, rx_lon, result["lat"], result["lon"])
                        fov_verdict = fov_gate_verdict(
                            fov, _fov_n_nodes, brg_for_fov, range_km,
                            range_rule_pass=not range_fail,
                        )
                        if _fov_mode == "active":
                            node_fail = not fov_verdict
                            if node_fail:
                                fov_state = fov.wedge_state(brg_for_fov)
                                fov_limit_km = round(fov.limit_km(brg_for_fov), 1)
                                if _fov_n_nodes == 2:
                                    fov_rule = "fov"
                        else:   # shadow
                            today_pass = not (range_fail or bearing_fail)
                            if today_pass == fov_verdict:
                                state.bump_counter("fov_shadow_agree")
                            elif fov_verdict:
                                # Today rejects, FOV would pass — the radar3
                                # recovery number.
                                state.bump_counter("fov_shadow_would_pass")
                            else:
                                state.bump_counter("fov_shadow_would_reject")
                            _fov_shadow_records.append({
                                "node_id": nid, "today_pass": today_pass,
                                "fov_verdict": fov_verdict,
                            })

                if not node_fail:
                    continue
                beam_failures.append({
                    "node_id": nid,
                    "range_km": round(range_km, 1),
                    "max_range_km": p["max_range_km"],
                    "bistatic_km": bistatic_km,
                    "max_bistatic_range_km": p["max_bistatic_range_km"],
                    "bearing_off_deg": bearing_off_deg,
                    "half_width_deg": p["beam_width_deg"] / 2.0,
                    "rule": fov_rule or ("range" if range_fail else "bearing"),
                    **({"fov_state": fov_state, "fov_limit_km": fov_limit_km}
                       if fov_state is not None else {}),
                })
        if beam_failures:
            _bf = beam_failures[0]
            logging.debug(
                "Solver result rejected: outside beam of node %s "
                "(lat=%.3f lon=%.3f range_km=%s bearing_off_deg=%s)",
                _bf["node_id"], result["lat"], result["lon"],
                _bf["range_km"], _bf["bearing_off_deg"],
            )
            state.bump_counter("solver_failures")
            state.bump_counter("solver_fail_beam")
            _beam_extra = {**(_extra or {}), "beam_failures": beam_failures}
            if _fov_mode == "shadow" and _fov_shadow_records:
                _beam_extra["fov_verdict"] = _fov_shadow_records
            _record_solve_history(
                "rejected_beam", s_in, result,
                extra=_beam_extra,
            )
            return None
        # Reject if the solution drifted more than _MAX_DISPLACEMENT_KM from
        # its sanity anchor. For N=2 this catches mirror-point ghosts (false
        # bistatic ellipse intersection 15-50 km away). For N≥3 it catches
        # solves where the inter-node associator bound a wrong frame and the
        # LM converged on a non-target position — production stats showed
        # those were the dominant source of the per-N inversion in
        # /api/test/mlat-accuracy.
        #
        # The anchor is the association guess by default (that mis-
        # association protection).  But for a consensus-vetted solve
        # (consensus_meta outcome == "selected" — active mode actually
        # filtered this solve's input on a corroborated node subset) the
        # corroborated centroid replaces guess-proximity as the sanity
        # reference instead.  Measured in the first live active-mode window:
        # 131 displacement kills, 80 of them <3 km from truth and 110
        # consensus-selected, median centroid offset 3.04 km against this
        # same 2 km cap — the gate was anchored to the contaminated guess
        # consensus+LM exist to correct, so it was killing the corrections
        # rather than the mis-associations.  shadow mode and every
        # fallback_* outcome keep the guess anchor: consensus either never
        # ran against this solve's input or was not acted on, so its
        # centroid is not a vetted reference here.
        _disp_km: float | None = None
        if "initial_guess" in s_in:
            _ig = s_in["initial_guess"]
            _anchor_lat, _anchor_lon = _ig.get("lat"), _ig.get("lon")
            _anchor_label = "guess"
            if consensus_meta is not None:
                if consensus_meta.get("outcome") == "selected":
                    _anchor_lat = consensus_meta.get("lat")
                    _anchor_lon = consensus_meta.get("lon")
                    _anchor_label = "consensus"
                consensus_meta["displacement_anchor"] = _anchor_label
            if _anchor_lat and _anchor_lon:
                _disp_km = _haversine_km(
                    float(_anchor_lat), float(_anchor_lon),
                    result["lat"], result["lon"],
                )
                if _disp_km > _MAX_DISPLACEMENT_KM:
                    logging.debug(
                        "n=%d result rejected: %.1f km from %s "
                        "(lat=%.3f lon=%.3f) — likely mirror or wrong-frame "
                        "convergence",
                        n_nodes, _disp_km,
                        "consensus centroid" if _anchor_label == "consensus"
                        else "initial_guess",
                        result["lat"], result["lon"],
                    )
                    state.bump_counter("solver_failures")
                    state.bump_counter("solver_fail_displacement")
                    _record_solve_history(
                        "rejected_displacement", s_in, result,
                        displacement_km=_disp_km, extra=_extra,
                    )
                    return None
        # n=2 publication gate.  The pairing must have been fitted and passed;
        # an unfitted one (chi2_per_dof None — too short an observation span so
        # far) is not yet evidence of anything.  Association re-tests it every
        # round, so a real target is published as soon as it has the history to
        # earn it rather than being discarded.
        if _N2_REQUIRE_CONFIRMED and n_nodes == 2 and isinstance(s_in, dict):
            _chi2 = _resolve_n2_chi2(s_in, node_cfgs)
            if _chi2 is None or _chi2 > _N2_CONFIRM_CHI2_MAX:
                logging.debug(
                    "n=2 solve withheld: chi2/dof=%s (limit %.1f, span %s epochs) "
                    "— lat=%.3f lon=%.3f",
                    "unfitted" if _chi2 is None else f"{_chi2:.2f}",
                    _N2_CONFIRM_CHI2_MAX, s_in.get("n_epochs"),
                    result.get("lat", 0), result.get("lon", 0),
                )
                state.bump_counter("n2_unconfirmed")
                _record_solve_history(
                    "n2_unconfirmed", s_in, result,
                    chi2_per_dof=_chi2, extra=_extra,
                )
                return result
            if not _claim_track_pair(s_in, _chi2):
                # A better-fitting pairing already owns one of these two
                # single-node tracks.  One track is one aircraft, so the two are
                # mutually exclusive hypotheses — and the chi2 gate alone cannot
                # separate them when both clear it, which is exactly the case
                # this catches.  Measured offline, competition of this kind is
                # worth ~9 points of n=2 ghost rate at no cost in real tracks.
                state.bump_counter("n2_unconfirmed")
                _record_solve_history(
                    "n2_outbid", s_in, result,
                    chi2_per_dof=_chi2, extra=_extra,
                )
                return result
        state.bump_counter("solver_successes")
        with state.solver_latency_lock:
            state.solver_total_solved += 1
        if enqueued_at is not None:
            latency = time.time() - enqueued_at
            with state.solver_latency_lock:
                state.solver_last_latency_s = latency
                state.solver_total_latency_s += latency
            if latency > 30.0:
                logging.warning("Solver latency high: %.1fs for %d-node candidate",
                                latency, s_in.get("n_nodes", 0))
                from services.alerting import send_alert
                send_alert(
                    "solver_latency_high",
                    f"Solver latency {latency:.1f}s — pipeline may be falling behind",
                    {"latency_s": round(latency, 1), "n_nodes": s_in.get("n_nodes", 0)},
                )
        state.task_last_success["solver"] = time.time()
        _adsb_hex = s_in.get("adsb_hex") if isinstance(s_in, dict) else None
        # Propagate the input ADS-B hex into the result so verification can match
        # the solve back to the *aircraft that produced the measurements* rather
        # than guessing by proximity. Critical for spoofed targets: their solver
        # converges near the frozen ADS-B init position, far from real position;
        # the proximity matcher would otherwise bind them to whatever innocent
        # aircraft happens to be near the spoof point and tag the result with
        # the wrong hex's is_anomalous=False — pollutting the normal_only stats.
        if _adsb_hex:
            result["adsb_hex"] = _adsb_hex
        # Publish-path calibration is banned: attribution rides on the very
        # association the coverage polygon is used to judge, and under an
        # active FOV gate it formed a feedback loop — a ghost publish (wrong
        # tracklet pairing tagged with a real hex) recorded positives for
        # BOTH contributing nodes at another aircraft's real position, which
        # opened bins, widened the gate, and produced more ghosts.  Staging
        # 2026-08-09: ghost precision 25%, 15/29 synthetic nodes with
        # out-of-wedge bins within ~25 min of the active flip.  The frame
        # path (services/track_gates.py) records the same aircraft per node,
        # gated on an actual fresh detection instead of a solve result.
        _collect_track_anomalies(s_in, result)
        # Adopt the constant-velocity fit's velocity for display, when it
        # clears its own quality gate.  Velocity is Doppler-determined — one
        # projection per node — so n=2 is underdetermined and n=3 exactly
        # determined, and single-epoch noise maps straight into the vector;
        # the CV fit ties the whole observation window to one
        # constant-velocity trajectory instead and is materially better.
        # Outside the lock deliberately: for n=2 the confirmation gate above
        # already ran this fit and cached it on s_in, so this is free; for
        # n≥3 it is a fresh ~86 ms pool call (publish rate here is ~0.2/s,
        # so affordable) and must not run while other solver workers are
        # blocked on _MN_TRACKS_LOCK.  Gates and rms residuals above are
        # untouched — they already ran on the single-epoch values.
        fit = _resolve_cv_fit(s_in, node_cfgs) if isinstance(s_in, dict) else None
        result["solver_vel_east"] = result.get("vel_east")
        result["solver_vel_north"] = result.get("vel_north")
        if (
            fit and fit.get("success")
            and (fit.get("n_epochs") or 0) >= 4
            and fit.get("chi2_per_dof") is not None
            and fit["chi2_per_dof"] <= CV_VEL_ADOPT_CHI2_MAX
        ):
            result["vel_east"] = fit["vel_east"]
            result["vel_north"] = fit["vel_north"]
            result["vel_up"] = fit["vel_up"]
            result["vel_source"] = "cv_fit"
        else:
            result["vel_source"] = "solve"
        with _MN_TRACKS_LOCK:
            # Identity before smoothing: the track key is the smoother's
            # history key, so dark targets accumulate history too.  Key
            # association uses the raw solve position — the same position its
            # own dead-reckoned 6 km gate was tuned against.  Both steps stay
            # under this lock so two workers solving the same aircraft cannot
            # each mint a fresh key.  Lock order _MN_TRACKS_LOCK →
            # _MN_POS_HISTORY_LOCK (inside the smoother) is never taken in
            # reverse anywhere.
            _anchor_key = s_in.get("anchor_key") if isinstance(s_in, dict) else None
            key, _key_how = multinode_key_decision(state.multinode_tracks, result,
                                                    _adsb_hex, _anchor_key)
            if _anchor_key:
                # Only an anchored solver input (top-down claiming, active
                # mode) ever sets s_in["anchor_key"] — this whole block is
                # inert off/shadow, by construction, with no mode read here.
                state.bump_counter("solver_anchored_published")
                state.bump_counter(
                    "solver_anchor_hits" if _key_how == "anchor"
                    else "solver_anchor_fallbacks"
                )
            # Raw solve position, before smoothing — the history record keeps
            # both so display-side drift can be separated from solver error.
            _raw_lat, _raw_lon = result["lat"], result["lon"]
            # Multi-epoch averaging cuts single-frame noise by ~√K.  Originally
            # n=2-with-ADS-B only — production showed dark targets (where MLAT
            # is the only position source) were the one population left raw.
            # Smoothing now runs through the env-gated KF in
            # services/track_filter.py, with this module's EWMA kept as the
            # TRACK_SMOOTHER=ewma fallback.
            result = track_filter.smooth_solve(result, key, _adsb_hex, ewma_fn=_ewma_smooth_track)
            prev = state.multinode_tracks.get(key)
            if prev:
                # Latch: a tracker flag raised on an earlier solve holds for
                # the multinode track's lifetime (≤60 s expiry) even if the
                # contributing track has since despawned or gone quiet.
                result["is_anomalous"] = bool(
                    result.get("is_anomalous")
                ) or bool(prev.get("is_anomalous"))
                result["anomaly_types"] = sorted(
                    set(result.get("anomaly_types", []))
                    | set(prev.get("anomaly_types", []))
                )
            # Source-track identity: the single-node track ids this solve was
            # built from.  Used below for supersession and carried into the
            # history record so a bad map marker can be traced to its inputs.
            result["source_track_ids"] = sorted(
                s_in.get("track_ids") or []
            ) if isinstance(s_in, dict) else []

            # Supersession: one aircraft is one set of source tracks.  A later
            # solve that consumes any of the same single-node tracks under a
            # DIFFERENT key is the same aircraft re-solved past the 6 km match
            # radius (multinode_key_decision), not a second one — replace the
            # earlier entry instead of letting it keep rendering for up to
            # 60 s beside the new one.  solve_count carries forward so the
            # re-solved aircraft does not fall back under the n=2 gate below.
            #
            # Unchanged by anchor honoring: `old_key == key: continue` below
            # already protects an anchor from superseding itself, and a
            # proximity-minted fragment sharing the anchor's source tracks
            # merging INTO the anchor (old_key != key, key == anchor_key) is
            # exactly the fragmentation-collapse this whole feature exists
            # for — not a bug to guard against.
            max_superseded_count = 0
            if result["source_track_ids"]:
                new_ids = set(result["source_track_ids"])
                for old_key, old_r in list(state.multinode_tracks.items()):
                    if old_key == key:
                        continue
                    if not new_ids.intersection(old_r.get("source_track_ids") or ()):
                        continue
                    state.multinode_tracks.pop(old_key, None)
                    with state.anomaly_lock:
                        state.anomaly_hexes.discard(
                            multinode_hex_from_key(old_key)
                        )
                    with _MN_POS_HISTORY_LOCK:
                        _MN_POS_HISTORY.pop(old_key, None)
                    track_filter.drop_key(old_key)
                    max_superseded_count = max(
                        max_superseded_count, old_r.get("solve_count", 0)
                    )
                    state.bump_counter("mn_superseded")

            result["solve_count"] = max(
                prev.get("solve_count", 0) if prev else 0, max_superseded_count
            ) + 1
            state.multinode_tracks[key] = result
            if trim_meta:
                state.bump_counter("solver_trimmed")
        # Append a snapshot to the track-archive buffer for Parquet persistence.
        # solve_ts_ms records when the solve completed (server wallclock) so
        # analysts can measure end-to-end latency vs. result["timestamp_ms"].
        archive_record = dict(result)
        archive_record["solve_ts_ms"] = int(time.time() * 1000)
        state.track_archive_buffer.append(archive_record)
        _record_solve_history(
            "published", s_in, result,
            solve_key=key,
            raw_lat=_raw_lat, raw_lon=_raw_lon,
            chi2_per_dof=s_in.get("chi2_per_dof") if isinstance(s_in, dict) else None,
            displacement_km=_disp_km,
            extra=_extra,
        )
    elif result is not None:
        # The LM ran but did not converge (success=False).  Previously this
        # path incremented nothing — staging showed hundreds of solves
        # vanishing with no observable reason.  trim_meta never applies here
        # (trimming only starts once a solve has already succeeded), but
        # consensus_meta can — the hypothesis stage ran before this solve
        # was even attempted.
        state.bump_counter("solver_failures")
        state.bump_counter("solver_fail_unconverged")
        _record_solve_history(
            "unconverged", s_in, result,
            extra={"consensus_meta": consensus_meta} if consensus_meta is not None else None,
        )
    return result


def _pool_solve_multinode(s_in, node_cfgs):
    """solve_multinode via the process pool (inline when no pool exists)."""
    from retina_geolocator.multinode_solver import solve_multinode
    return _pool_call(solve_multinode, s_in, node_cfgs)


def _solver_worker_iteration(timeout: float = 1.0, q=None) -> bool:
    """One queue-drain step; True if an item was taken (processed or failed).

    ``q`` defaults to state.solver_queue; tests pass their own Queue so a
    worker daemon leaked by an earlier test cannot race their items away.
    """
    if q is None:
        q = state.solver_queue
    try:
        item = q.get(timeout=timeout)
    except queue.Empty:
        return False
    try:
        _process_solver_item(item, _pool_solve_multinode)
    except Exception:
        # A worker thread must survive any single bad item: when the
        # trail-snapshot race killed both workers, the queue silently
        # filled and publishing stopped for hours with only the health
        # check noticing.
        state.bump_counter("solver_worker_errors")
        logging.exception("Solver worker: unhandled error for one item")
    return True


def _run_solver_worker():
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread."""
    while True:
        _solver_worker_iteration()


def start_solver_workers():
    """Start the solve process pool and N daemon threads draining the queue."""
    global _solver_pool
    from retina_geolocator.multinode_solver import solve_multinode
    with _solver_pool_lock:
        if _solver_pool is None and _POOL_ENABLED:
            try:
                _solver_pool = _make_solver_pool()
            except Exception:
                _solver_pool = None
                logging.exception(
                    "Solver process pool unavailable — solving inline on"
                    " worker threads"
                )
    if _solver_pool is not None:
        # Fire-and-forget prewarm: spin the children up and have them import
        # scipy now, not under the first association burst.  (<2 measurements
        # returns None before any solving.)
        _noop = {"initial_guess": {"lat": 0.0, "lon": 0.0}, "measurements": []}
        for _ in range(_N_SOLVER_WORKERS):
            _solver_pool.submit(solve_multinode, _noop, {})
    for i in range(_N_SOLVER_WORKERS):
        t = threading.Thread(
            target=_run_solver_worker, daemon=True, name=f"solver-{i}",
        )
        t.start()
    logging.info(
        "Started %d multinode solver worker(s) (process pool: %s)",
        _N_SOLVER_WORKERS,
        "on" if _solver_pool is not None else "off",
    )
