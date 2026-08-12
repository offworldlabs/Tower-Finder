"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import math
import os
import queue
import threading
import time
from collections import deque

from config.constants import N2_CONFIRM_CHI2_MAX, N2_TRACK_ASSOCIATION
from core import state

# Beam-coverage geometry, used to reject solver results whose position falls
# outside a contributing node's detection beam (ghost disambiguation at n=2).
# This module carried its own haversine, bearing and in-beam rule until those
# were consolidated into services.geo.
from services.calibration import record_adsb_calibration
from services.geo import haversine_km as _haversine_km
from services.geo import in_node_beam as _in_node_beam
from services.geo import offset_latlon_m

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

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
_SOLVER_RMS_DELAY_MAX_US = 3.0

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


def _sweep_altitudes(s_in: dict, node_cfgs: dict, solve_fn, layers_km: list[float], metric: str) -> dict | None:
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
                alt_km,
                metric,
                rms,
                best_rms,
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


# ── Multi-epoch EWMA position smoother (all N) ───────────────────────────────
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
# Dead-reckoning uses ADS-B ground-speed + track (always available in
# simulation; state.adsb_aircraft is populated by the frame processor for every
# ADS-B-tagged detection).  If ADS-B velocity or hex is missing the smoother
# returns the raw solver result unchanged.

# Per-hex rolling buffer: hex → deque of (lat, lon, timestamp_s)
_MN_POS_HISTORY: dict[str, deque] = {}
_MN_POS_HISTORY_LOCK = threading.Lock()
_MN_HISTORY_K = 3  # number of past frames to average (including current)
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


def _sweep_mn_history(now_s: float) -> None:
    """Drop hexes whose newest sample is stale.  Caller holds the lock."""
    global _mn_history_last_sweep
    if now_s - _mn_history_last_sweep < _MN_HISTORY_TTL_S / 10:
        return
    _mn_history_last_sweep = now_s
    for h in [h for h, dq in _MN_POS_HISTORY.items() if not dq or now_s - dq[-1][2] > _MN_HISTORY_TTL_S]:
        del _MN_POS_HISTORY[h]


def _ewma_smooth_track(result: dict, adsb_hex: str | None) -> dict:
    """Apply dead-reckoned multi-epoch averaging to a multinode solver result.

    Dead-reckon previous solve positions to the current solve timestamp using
    ADS-B ground-speed and track, then return the simple mean of the
    dead-reckoned history and the current solve.  Thread-safe via lock.

    N-agnostic — applied to every solve with a known ADS-B hex regardless of
    n_nodes.  Returns the original result dict if hex is unknown, ADS-B
    velocity is unavailable, or there is no prior history yet.
    """
    if not adsb_hex:
        return result

    r_lat = result["lat"]
    r_lon = result["lon"]
    r_ts = result.get("timestamp_ms", 0) / 1000.0

    # Retrieve ADS-B velocity for dead-reckoning.
    adsb = state.adsb_aircraft.get(adsb_hex)
    if not adsb:
        with _MN_POS_HISTORY_LOCK:
            _sweep_mn_history(r_ts)
            _MN_POS_HISTORY.setdefault(adsb_hex, deque(maxlen=_MN_HISTORY_K)).append((r_lat, r_lon, r_ts))
        return result

    gs_knots = float(adsb.get("gs", 0) or 0)
    track_deg = float(adsb.get("track", 0) or 0)
    gs_kms = gs_knots * 0.514444 / 1000.0  # knots → km/s
    # Geographic track: 0° = North, 90° = East.
    vel_north_kms = gs_kms * math.cos(math.radians(track_deg))
    vel_east_kms = gs_kms * math.sin(math.radians(track_deg))

    with _MN_POS_HISTORY_LOCK:
        _sweep_mn_history(r_ts)
        hist = _MN_POS_HISTORY.setdefault(adsb_hex, deque(maxlen=_MN_HISTORY_K))

        # Dead-reckon each past position forward to the current solve time
        # and collect valid points (not too stale, not too far after dr).
        positions: list[tuple[float, float]] = [(r_lat, r_lon)]

        for prev_lat, prev_lon, prev_ts in hist:
            dt = r_ts - prev_ts
            if dt <= 0 or dt > _MN_DR_MAX_AGE_S:
                continue  # skip future or stale entries
            dr_lat, dr_lon = offset_latlon_m(
                prev_lat,
                prev_lon,
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
        "EWMA: hex=%s K=%d raw=(%.4f,%.4f) → smooth=(%.4f,%.4f)",
        adsb_hex,
        len(positions),
        r_lat,
        r_lon,
        avg_lat,
        avg_lon,
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


def _multinode_track_key(result: dict, adsb_hex: str | None) -> str:
    """Return a stable per-aircraft key for state.multinode_tracks.

    Call under _MN_TRACKS_LOCK — it scans state.multinode_tracks and the caller
    writes back into it.

    ADS-B-tagged solves key on the transponder hex, the same identity
    _ewma_smooth_track already uses to accumulate cross-solve history, so the
    smoother and the track store finally agree.

    Dark targets have no such identity and are associated to the nearest recent
    dark track, dead-reckoned forward to this solve's timestamp.  This is the
    path real (non-simulated) hardware depends on: ground_truth_hex is not
    usable here because it only exists for simulated traffic.
    """
    if adsb_hex:
        return f"mn-adsb-{adsb_hex}"

    lat, lon = result["lat"], result["lon"]
    ts_s = result.get("timestamp_ms", 0) / 1000.0
    best_key, best_dist = None, _MN_ASSOC_MAX_DIST_KM

    for key, prev in state.multinode_tracks.items():
        # Only dark tracks are claimable; an untagged solve must never steal the
        # identity of an ADS-B-tagged aircraft that happens to be nearby.
        if not key.startswith("mn-dark-"):
            continue
        dt = ts_s - prev.get("timestamp_ms", 0) / 1000.0
        if not (0.0 <= dt <= _MN_ASSOC_MAX_AGE_S):
            continue
        p_lat, p_lon = prev.get("lat"), prev.get("lon")
        if p_lat is None or p_lon is None:
            continue
        # Dead-reckon the existing track forward before measuring, so a fast
        # target is not rejected purely for having moved since its last solve.
        p_lat, p_lon = offset_latlon_m(
            p_lat,
            p_lon,
            east_m=prev.get("vel_east", 0.0) * dt,
            north_m=prev.get("vel_north", 0.0) * dt,
        )
        d = _haversine_km(lat, lon, p_lat, p_lon)
        if d < best_dist:
            best_key, best_dist = key, d

    if best_key is not None:
        return best_key
    # No claimant — a genuinely new target.  This key only needs to be unique at
    # birth; every later solve associates to it above, so it stays stable.
    return f"mn-dark-{result.get('timestamp_ms', 0)}-{lat:.3f}-{lon:.3f}"


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
_TRACK_CLAIMS: dict[str, tuple[float, float]] = {}  # track_id → (chi2, claimed_at)
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
        return True  # detection-level input: nothing to arbitrate
    # Note this claims only the first pair of a cluster: format_track_pairs_for_solver
    # truncates track_pair_ids to [:1], so a cluster spanning three tracks leaves
    # the third unclaimed.  s_in["track_ids"] carries the full set if that is
    # ever worth closing — see --claim-policy all-tracks.
    track_ids = [tid for pair in pairs for tid in pair]
    with _TRACK_CLAIMS_LOCK:
        return claim_decision(_TRACK_CLAIMS, track_ids, chi2, time.time(), _TRACK_CLAIM_TTL_S)


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


def _resolve_n2_chi2(s_in: dict, node_cfgs) -> float | None:
    """chi2/dof for an n=2 pairing, fitting here if association deferred it.

    The constant-velocity fit is an ~86 ms LM solve.  Association runs inside
    the frame worker, so doing it there is frame latency: measured on staging
    at 92% frame-queue depth with the processor 21 s behind a 6 frame/s feed.
    This worker already has its own threads and a queue with a staleness drop,
    which is exactly the place for it — so association hands over the epochs
    and the fit happens on this side.

    Association may still fit inline (the offline bench does, having no queue),
    in which case chi2_per_dof arrives already set and this is a no-op.
    """
    chi2 = s_in.get("chi2_per_dof")
    if chi2 is not None:
        return chi2
    epochs = s_in.get("cv_epochs")
    if not epochs or not isinstance(node_cfgs, dict):
        return None
    try:
        from retina_geolocator.multinode_solver import fit_constant_velocity

        fit = fit_constant_velocity(
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
    # Cache it so a retry of the same input does not refit.
    s_in["chi2_per_dof"] = fit["chi2_per_dof"]
    s_in["n_epochs"] = fit["n_epochs"]
    return fit["chi2_per_dof"]


# Public alias: the offline bench resolves chi2 through the same code path the
# worker uses, rather than carrying a copy of the fit plumbing.
resolve_n2_chi2 = _resolve_n2_chi2


def _process_solver_item(item: tuple, solve_fn) -> dict | None:
    """Process a single solver queue entry. Returns the solver result (or None).

    Extracted from the worker loop so the success/failure/latency bookkeeping
    can be unit-tested without spinning up daemon threads.
    """
    s_in, node_cfgs = item[0], item[1]
    enqueued_at: float | None = item[2] if len(item) > 2 else None
    # Discard items that have been waiting too long in the queue.  By the time
    # they are solved, the result's timestamp_ms will be > 60 s old and the
    # entry will be immediately pruned from multinode_tracks — wasting CPU.
    age_s = time.time() - enqueued_at if enqueued_at is not None else 0.0
    if enqueued_at is not None and age_s > _SOLVER_MAX_QUEUE_AGE_S:
        logging.debug(
            "Solver: dropping stale item (age=%.1fs > %.1fs, n_nodes=%d)",
            age_s,
            _SOLVER_MAX_QUEUE_AGE_S,
            s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0,
        )
        return None
    n_nodes = s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0
    try:
        if "initial_guess" not in s_in:
            result = solve_fn(s_in, node_cfgs)
        elif n_nodes >= 3:
            result = _solve_best_altitude(s_in, node_cfgs, solve_fn)
        else:
            result = _solve_best_altitude_n2(s_in, node_cfgs, solve_fn)
    except Exception:
        state.task_error_counts["solver"] += 1
        state.bump_counter("solver_failures")
        logging.exception("Multinode solver failed")
        result = None
    if result and result.get("success"):
        rms_delay = result.get("rms_delay", 0) or 0
        if rms_delay > _SOLVER_RMS_DELAY_MAX_US:
            logging.debug(
                "Solver result rejected: rms_delay=%.1f µs > %.1f µs threshold (n_nodes=%d, lat=%.3f, lon=%.3f)",
                rms_delay,
                _SOLVER_RMS_DELAY_MAX_US,
                result.get("n_nodes", 0),
                result.get("lat", 0),
                result.get("lon", 0),
            )
            state.bump_counter("solver_failures")
            return result
        rms_doppler = result.get("rms_doppler", 0) or 0
        if rms_doppler > _SOLVER_RMS_DOPPLER_MAX_HZ:
            logging.debug(
                "Solver result rejected: rms_doppler=%.1f Hz > %.1f Hz threshold "
                "(n_nodes=%d, lat=%.3f, lon=%.3f) — physically unrealisable Doppler",
                rms_doppler,
                _SOLVER_RMS_DOPPLER_MAX_HZ,
                result.get("n_nodes", 0),
                result.get("lat", 0),
                result.get("lon", 0),
            )
            state.bump_counter("solver_failures")
            return result
        # Reject solutions outside the beam coverage of contributing nodes.
        # For n=2 the solver has two geometric solutions (two bistatic ellipse
        # intersections); the ghost intersection typically falls outside one of
        # the node beams.  This check rejects it without needing Doppler data.
        # Skipped when node_cfgs lacks beam info (cfg is None) — safe fallback.
        contributing_ids = result.get("contributing_node_ids", [])
        if contributing_ids and isinstance(node_cfgs, dict):
            for nid in contributing_ids:
                cfg = node_cfgs.get(nid)
                if cfg and not _in_node_beam(result["lat"], result["lon"], cfg):
                    logging.debug(
                        "Solver result rejected: outside beam of node %s "
                        "(lat=%.3f lon=%.3f beam_az=%.0f beam_w=%.0f range_km=%.0f)",
                        nid,
                        result["lat"],
                        result["lon"],
                        float(cfg.get("beam_azimuth_deg") or 0),
                        float(cfg.get("beam_width_deg") or 41),
                        float(cfg.get("max_range_km") or 50),
                    )
                    state.bump_counter("solver_failures")
                    return None
        # Reject if the solution drifted more than _MAX_DISPLACEMENT_KM from
        # the ADS-B initial_guess. For N=2 this catches mirror-point ghosts
        # (false bistatic ellipse intersection 15-50 km away). For N≥3 it
        # catches solves where the inter-node associator bound a wrong frame
        # and the LM converged on a non-target position — production stats
        # showed those were the dominant source of the per-N inversion in
        # /api/test/mlat-accuracy.
        if "initial_guess" in s_in:
            _ig = s_in["initial_guess"]
            _ig_lat = _ig.get("lat")
            _ig_lon = _ig.get("lon")
            if _ig_lat and _ig_lon:
                _disp_km = _haversine_km(
                    float(_ig_lat),
                    float(_ig_lon),
                    result["lat"],
                    result["lon"],
                )
                if _disp_km > _MAX_DISPLACEMENT_KM:
                    logging.debug(
                        "n=%d result rejected: %.1f km from initial_guess "
                        "(lat=%.3f lon=%.3f) — likely mirror or wrong-frame "
                        "convergence",
                        n_nodes,
                        _disp_km,
                        result["lat"],
                        result["lon"],
                    )
                    state.bump_counter("solver_failures")
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
                    "n=2 solve withheld: chi2/dof=%s (limit %.1f, span %s epochs) — lat=%.3f lon=%.3f",
                    "unfitted" if _chi2 is None else f"{_chi2:.2f}",
                    _N2_CONFIRM_CHI2_MAX,
                    s_in.get("n_epochs"),
                    result.get("lat", 0),
                    result.get("lon", 0),
                )
                state.bump_counter("n2_unconfirmed")
                return result
            if not _claim_track_pair(s_in, _chi2):
                # A better-fitting pairing already owns one of these two
                # single-node tracks.  One track is one aircraft, so the two are
                # mutually exclusive hypotheses — and the chi2 gate alone cannot
                # separate them when both clear it, which is exactly the case
                # this catches.  Measured offline, competition of this kind is
                # worth ~9 points of n=2 ghost rate at no cost in real tracks.
                state.bump_counter("n2_unconfirmed")
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
                logging.warning("Solver latency high: %.1fs for %d-node candidate", latency, s_in.get("n_nodes", 0))
                from services.alerting import send_alert

                send_alert(
                    "solver_latency_high",
                    f"Solver latency {latency:.1f}s — pipeline may be falling behind",
                    {"latency_s": round(latency, 1), "n_nodes": s_in.get("n_nodes", 0)},
                )
        state.task_last_success["solver"] = time.time()
        # For every solve with a known ADS-B hex, apply dead-reckoned multi-epoch
        # EWMA smoothing to reduce single-frame measurement noise (reduces mean
        # position error by ~√K where K is the number of history frames used).
        # Originally n_nodes == 2 only — production data showed the gate caused
        # an apparent N=2 < N=3 inversion in /api/test/mlat-accuracy because
        # only N=2 benefited from the noise reduction.
        _adsb_hex = s_in.get("adsb_hex") if isinstance(s_in, dict) else None
        result = _ewma_smooth_track(result, _adsb_hex)
        # Propagate the input ADS-B hex into the result so verification can match
        # the solve back to the *aircraft that produced the measurements* rather
        # than guessing by proximity. Critical for spoofed targets: their solver
        # converges near the frozen ADS-B init position, far from real position;
        # the proximity matcher would otherwise bind them to whatever innocent
        # aircraft happens to be near the spoof point and tag the result with
        # the wrong hex's is_anomalous=False — pollutting the normal_only stats.
        if _adsb_hex:
            result["adsb_hex"] = _adsb_hex
        # Empirical coverage characterises where a node can see, so it is built
        # only from positions known independently of the solve.  This loop used
        # to record result["lat"]/["lon"] for every multinode solve — measured
        # blind, 55-85% of n=2 tracks are ghosts a median 20+ km from any
        # aircraft, and those were shaping the polygon.  Worse, that polygon is
        # on its way to constraining association, so a phantom would have
        # widened the very region that produced it.
        #
        # The ADS-B fix is the honest input, and contributing_node_ids remains
        # the right attribution: one calibration point per node that actually
        # saw the target.  A dark solve now records nothing.
        _cal = state.adsb_aircraft.get(_adsb_hex) if _adsb_hex else None
        if _cal:
            record_adsb_calibration(
                result.get("contributing_node_ids", []),
                _cal.get("lat"),
                _cal.get("lon"),
                age_s=time.time() - _cal.get("last_seen_ms", 0) / 1000.0,
            )
        with _MN_TRACKS_LOCK:
            key = _multinode_track_key(result, _adsb_hex)
            state.multinode_tracks[key] = result
        # Append a snapshot to the track-archive buffer for Parquet persistence.
        # solve_ts_ms records when the solve completed (server wallclock) so
        # analysts can measure end-to-end latency vs. result["timestamp_ms"].
        archive_record = dict(result)
        archive_record["solve_ts_ms"] = int(time.time() * 1000)
        state.track_archive_buffer.append(archive_record)
    return result


def _run_solver_worker():
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread."""
    from retina_geolocator.multinode_solver import solve_multinode

    while True:
        try:
            item = state.solver_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        _process_solver_item(item, solve_multinode)


def start_solver_workers():
    """Start N daemon threads that continuously drain the solver queue."""
    for i in range(_N_SOLVER_WORKERS):
        t = threading.Thread(
            target=_run_solver_worker,
            daemon=True,
            name=f"solver-{i}",
        )
        t.start()
    logging.info("Started %d multinode solver worker(s)", _N_SOLVER_WORKERS)
