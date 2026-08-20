"""Known-lane solver: per-hex solves of identity-claimed detections.

The regular pipeline treats identity as an outcome — detections are paired
bottom-up by the delay grid, solved, and only then checked against ADS-B,
with a disagreeing solve rejected and discarded.  That makes known aircraft
the WORST-measured population: exactly the solves that would calibrate the
radar (truth is on file) are the ones the displacement gate deletes, so a
node whose measurements are drifting looks like a node that simply stopped
publishing.

This lane inverts that.  Slice A (the claiming stage) binds detections to
known ADS-B aircraft BEFORE dark association and publishes them into
``state.known_claims``; this module solves each claimed hex from those
detections directly.  Identity gives cross-node correspondence for free —
two claims on the same hex ARE the same aircraft — so the delay-grid pairing
machinery in retina_analytics.association is bypassed entirely, and every
known target with >= 2 claiming nodes produces a solve attempt and an
accuracy sample on EVERY pass, whatever the outcome.

FREE SOLVE INVARIANT: the ADS-B fix seeds the initial guess and pins the
altitude, and does NOTHING else.  The solution is never regularized,
weighted, or constrained toward the ADS-B position — the whole point of
this lane is that the residual solver-vs-ADS-B error measures the RADAR
(per-node bias, geometry, association quality), and any prior toward truth
would contaminate that measurement into a tautology.  The displacement gate
here CLASSIFIES (truth_match / ghost); unlike the regular pipeline's, it
never destroys the record or the accuracy sample.

Modes (state.KNOWN_LANE_MODE, owned by slice A; absent means "off"):
  off     — this module does nothing at all.  Same precedent as
            SOLVER_CONSENSUS_MODE / FOV_MODE: off is byte-identical to the
            behaviour that predates the feature.
  shadow  — solve + record outcomes, accuracy samples and counters, but
            never touch the live feed: a shadow run produces comparison
            data with zero behaviour change.
  binding — additionally publish truth_match solves into
            state.multinode_tracks under the hex's own key (mn-adsb-*),
            exactly as today's tagged solves are, superseding the regular
            pipeline's output for that hex.  Ghosts are recorded but never
            published: a displaced solve under a real hex is a wrong map
            marker, the same reason the regular displacement gate exists.

Neither ``state.known_claims`` nor ``state.KNOWN_LANE_MODE`` exists on this
branch — slice A owns core/state.py — so every access goes through getattr
with an inert default, and the counters below are registered onto the state
module at import.  This file must keep working unchanged whether slice A has
merged or not.
"""

import logging
import math
import threading
import time

from config.constants import FT_TO_M
from core import state
from services import track_filter
from services.geo import haversine_km, offset_latlon_m
from services.id_utils import normalize_hex_key

# Deliberate one-way dependency: this module reuses the solver worker's
# record store, gates, publication lock and smoother so known-lane records
# are queryable exactly like regular ones.  solver.py only ever imports this
# module lazily, inside _run_solver_worker (see the wiring there), so the
# import below cannot form a cycle.
from services.tasks import solver as solver_mod

# ── Counters ──────────────────────────────────────────────────────────────────
# Registered onto core.state at import rather than declared there: slice A
# owns core/state.py on this integration train, and a second declarer would
# be a guaranteed merge conflict.  hasattr-guarded so the day the counters DO
# move into state.py (integration step), this block becomes a no-op instead
# of re-zeroing them.  state.bump_counter works on them either way — it
# resolves names through the state module's globals(), which setattr feeds.
_COUNTERS = (
    "known_lane_attempts",
    "known_lane_truth_match",
    "known_lane_ghost",
    "known_lane_no_converge",
    "known_lane_published",
)
for _name in _COUNTERS:
    if not hasattr(state, _name):
        setattr(state, _name, 0)

# ── Claim selection windows ───────────────────────────────────────────────────
# Freshness: a claim older than this can no longer produce a visible result —
# same reasoning (and same constant) as the solver queue's staleness drop:
# multinode_tracks expiry is 60 s and a solve takes seconds, so solving a
# 45-s-old measurement set is CPU spent on an entry that dies on arrival.
_CLAIM_MAX_AGE_S = solver_mod._SOLVER_MAX_QUEUE_AGE_S

# Cross-node coincidence: the solver treats the measurement set as one epoch,
# so per-node claims are only combined when their timestamps agree to within
# this window.  At 300 m/s a 5 s spread is ~1.5 km of target motion — about
# the same order as the association grid step the regular lane's initial
# guesses tolerate — while frames arrive at ~1/s per node, so a live pair of
# claiming nodes is essentially never split by this gate.
_CLAIM_SPREAD_S = 5.0

# Pass cadence.  The pass itself is a cheap scan — a hex is only re-solved
# when a NEWER claim has arrived since its last attempt (see
# _last_attempt_ts_ms) — so this only bounds how often the scan runs, not
# how often solves happen.
_PASS_MIN_INTERVAL_S = 2.0

# Dict-level TTL for the per-hex dedup map, same shape/justification as
# solver.py's _MN_HISTORY_TTL_S: the map otherwise grows one entry per
# distinct hex for the process lifetime.  Swept opportunistically per pass.
_ATTEMPT_TTL_S = 600.0

# One pass at a time.  maybe_run_pass is called from every solver worker
# thread's loop; a try-lock (never blocking) means a second worker skips the
# pass instead of queueing behind it, and everything below the lock —
# _last_pass_ts, _last_attempt_ts_ms — is single-writer by construction.
_PASS_LOCK = threading.Lock()
_last_pass_ts = 0.0
_last_attempt_ts_ms: dict[str, int] = {}


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    global _last_pass_ts
    with _PASS_LOCK:
        _last_pass_ts = 0.0
        _last_attempt_ts_ms.clear()
    with state.counters_lock:
        for name in _COUNTERS:
            setattr(state, name, 0)


def _mode() -> str:
    """The known-lane mode, defensively.  Slice A owns the flag; an absent or
    unrecognised value is "off" (the ASSOC_CLAIM_MODE fallback precedent)."""
    mode = getattr(state, "KNOWN_LANE_MODE", "off")
    return mode if mode in ("off", "shadow", "binding") else "off"


def _num(v, fallback=0.0) -> float:
    """Float coercion for ADS-B fields; alt_baro can be the string "ground"."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(fallback)


def _select_claims(dq, now_ms: int) -> dict[str, dict]:
    """Newest usable claim per node from one hex's deque, or {} if fewer than
    two nodes survive.

    Skips stale claims (see _CLAIM_MAX_AGE_S), contested ones (a contested
    claim is one the claiming stage could not bind to a single identity, and
    a wrong-identity measurement is exactly the contamination whose residual
    this lane exists to measure cleanly), and anything malformed — the deque
    is slice A's, written concurrently, and this reader must survive any
    single bad entry.  Nodes whose newest claim trails the newest overall by
    more than _CLAIM_SPREAD_S are dropped rather than failing the whole hex.
    """
    best: dict[str, dict] = {}
    for c in list(dq):
        if not isinstance(c, dict):
            continue
        try:
            ts_ms = int(c["ts_ms"])
            node_id = c["node_id"]
            float(c["delay_us"])
            float(c["doppler_hz"])
        except (KeyError, TypeError, ValueError):
            continue
        if not node_id or now_ms - ts_ms > _CLAIM_MAX_AGE_S * 1000.0:
            continue
        if c.get("contested"):
            continue
        cur = best.get(node_id)
        if cur is None or ts_ms > int(cur["ts_ms"]):
            best[node_id] = c
    if len(best) < 2:
        return {}
    newest_ts = max(int(c["ts_ms"]) for c in best.values())
    best = {nid: c for nid, c in best.items() if newest_ts - int(c["ts_ms"]) <= _CLAIM_SPREAD_S * 1000.0}
    return best if len(best) >= 2 else {}


def _build_solver_input(hexn: str, claims: dict[str, dict]) -> dict | None:
    """Shape one hex's selected claims into a solver input.

    Mirrors the adsb_inputs shape from retina_analytics.association's
    _adsb_seed_round (the current tagged path), minus the track/cv plumbing
    this lane has no use for: identity supplied the correspondence, so there
    is no pairing to confirm.

    FREE SOLVE (see the module docstring): the ADS-B fix contributes the
    initial guess (dead-reckoned to the newest claim's epoch), the pinned
    altitude, and the velocity seed — the same three things the tagged path
    already feeds solve_multinode — and nothing downstream ever pulls the
    solution back toward it.
    """
    newest = max(claims.values(), key=lambda c: int(c["ts_ms"]))
    newest_ts_ms = int(newest["ts_ms"])
    fix = newest.get("adsb_fix")
    if not isinstance(fix, dict):
        return None
    lat, lon = fix.get("lat"), fix.get("lon")
    if lat is None or lon is None:
        return None

    gs_ms = _num(fix.get("gs")) * 0.514444  # knots → m/s
    trk_rad = math.radians(_num(fix.get("track")))
    vel_east = gs_ms * math.sin(trk_rad)
    vel_north = gs_ms * math.cos(trk_rad)
    dt_s = (newest_ts_ms - int(_num(fix.get("fix_ts_ms"), newest_ts_ms))) / 1000.0
    guess_lat, guess_lon = offset_latlon_m(
        float(lat),
        float(lon),
        east_m=vel_east * dt_s,
        north_m=vel_north * dt_s,
    )

    return {
        "initial_guess": {
            "lat": guess_lat,
            "lon": guess_lon,
            "alt_km": _num(fix.get("alt_baro")) * FT_TO_M / 1000.0,
        },
        "initial_velocity": {
            "vel_east_ms": vel_east,
            "vel_north_ms": vel_north,
        },
        "measurements": [
            {
                "node_id": nid,
                "delay_us": float(c["delay_us"]),
                "doppler_hz": float(c["doppler_hz"]),
                "snr": _num(c.get("snr")),
            }
            for nid, c in sorted(claims.items())
        ],
        "n_nodes": len(claims),
        "timestamp_ms": newest_ts_ms,
        "adsb_hex": hexn,
        "known_lane": True,
    }


def _record_accuracy(hexn: str, err_km: float, label: str, n_nodes: int, ts_s: float) -> None:
    """Append one known-lane sample to the rolling accuracy store.

    Same store and base shape as track_gates._record_accuracy_sample, so
    _refresh_accuracy_stats bins it by position_source with no changes —
    known_lane_truth_match vs known_lane_ghost is the headline comparison —
    plus label/n_nodes as their own fields so later analysis can bin by
    GDOP proxy without parsing them back out of the source string.  Recorded
    on EVERY converged outcome, ghost included: a ghost's error is exactly
    the datum the regular pipeline's displacement gate used to delete.
    """
    state.accuracy_samples.append(
        {
            "hex": hexn,
            "error_km": round(err_km, 4),
            "position_source": f"known_lane_{label}",
            "label": label,
            "n_nodes": n_nodes,
            "ts": round(ts_s, 1),
        }
    )


def _publish(hexn: str, s_in: dict, result: dict) -> str:
    """Publish one truth_match solve into the live feed, binding mode only.

    Deliberately the same steps, same lock, and same key rule as
    _process_solver_item's publish block, via the primitives it exports:
    multinode_key_decision keys the entry mn-adsb-<hex> (its unconditional
    ADS-B branch), so the known-lane entry replaces — supersedes — whatever
    the regular pipeline last wrote for this aircraft, and the smoother's
    history is shared with it by construction (the key IS the smoother key).
    """
    result["adsb_hex"] = hexn
    # No source tracks: claims are detection-level, so the anomaly collector
    # finds nothing and stamps clean flags; the latch below still carries any
    # flag a regular-pipeline solve raised on this hex earlier.
    solver_mod._collect_track_anomalies(s_in, result)
    result["source_track_ids"] = []
    result["solver_vel_east"] = result.get("vel_east")
    result["solver_vel_north"] = result.get("vel_north")
    result["vel_source"] = "solve"
    # Same trust rule as the regular publish path: raw-solve velocity is
    # under/exactly-determined at n<=3, and a pinned vz means the misfit
    # leaked into the horizontal components.
    result["vel_untrusted"] = bool(result.get("vz_saturated")) or int(result.get("n_nodes") or 0) <= 3

    with solver_mod._MN_TRACKS_LOCK:
        key, _how = solver_mod.multinode_key_decision(state.multinode_tracks, result, hexn, None)
        smoothed = track_filter.smooth_solve(result, key, hexn, ewma_fn=solver_mod._ewma_smooth_track)
        prev = state.multinode_tracks.get(key)
        if prev:
            # Latch, exactly as the regular path does: a tracker flag raised
            # on an earlier solve holds for the track's lifetime.
            smoothed["is_anomalous"] = bool(smoothed.get("is_anomalous")) or bool(prev.get("is_anomalous"))
            smoothed["anomaly_types"] = sorted(
                set(smoothed.get("anomaly_types", [])) | set(prev.get("anomaly_types", []))
            )
        smoothed["solve_count"] = (prev.get("solve_count", 0) if prev else 0) + 1
        state.multinode_tracks[key] = smoothed

    archive_record = dict(smoothed)
    archive_record["solve_ts_ms"] = int(time.time() * 1000)
    state.track_archive_buffer.append(archive_record)
    return key


def _attempt(hexn: str, s_in: dict, node_cfgs: dict, solve_fn, mode: str) -> None:
    """One known-lane solve attempt: solve, classify, record — unconditionally.

    Every attempt leaves a record in state.mlat_solve_history (known_* rather
    than published/rejected_* outcomes, so the regular funnel's numbers stay
    clean in shadow AND binding), and every CONVERGED attempt leaves an
    accuracy sample.  Classification is the regular displacement gate's own
    threshold applied to truth-at-solve-epoch — which is the initial guess by
    construction (the fix was dead-reckoned to exactly this epoch), so the
    record's displacement_km and the accuracy error are the same number.
    """
    state.bump_counter("known_lane_attempts")
    try:
        # Single solve at the pinned ADS-B altitude — no layer sweep.  The
        # sweep exists to DISCOVER an unknown altitude; here identity already
        # supplies it, and letting the solver shop across layers for the
        # lowest residual would let a biased node pick a flattering altitude
        # instead of showing up in the residual this lane measures.
        result = solve_fn(s_in, node_cfgs)
    except Exception:
        logging.exception("Known-lane solve failed for %s", hexn)
        result = None

    if not result or not result.get("success"):
        state.bump_counter("known_lane_no_converge")
        solver_mod._record_solve_history(
            "known_no_converge",
            s_in,
            result if isinstance(result, dict) else None,
            extra={"known_lane": True, "label": "no_converge", "published": False},
        )
        return

    ig = s_in["initial_guess"]
    raw_lat, raw_lon = float(result["lat"]), float(result["lon"])
    err_km = haversine_km(float(ig["lat"]), float(ig["lon"]), raw_lat, raw_lon)
    label = "truth_match" if err_km <= solver_mod._MAX_DISPLACEMENT_KM else "ghost"
    state.bump_counter(f"known_lane_{label}")
    n_nodes = int(result.get("n_nodes") or s_in.get("n_nodes") or 0)
    _record_accuracy(hexn, err_km, label, n_nodes, s_in["timestamp_ms"] / 1000.0)

    published = mode == "binding" and label == "truth_match"
    solve_key = _publish(hexn, s_in, result) if published else None
    if published:
        state.bump_counter("known_lane_published")

    solver_mod._record_solve_history(
        f"known_{label}",
        s_in,
        result,
        solve_key=solve_key,
        raw_lat=raw_lat,
        raw_lon=raw_lon,
        displacement_km=err_km,
        extra={"known_lane": True, "label": label, "published": published},
    )


def run_known_lane_pass(solve_fn, node_cfgs: dict | None = None) -> int:
    """One full pass over state.known_claims; returns the attempt count.

    A hex is attempted when it has fresh, uncontested claims from >= 2
    distinct nodes at compatible timestamps (see _select_claims) AND a claim
    newer than its last attempt — so re-running the pass against an unchanged
    registry is free, and calling this more often than claims arrive costs a
    scan, never a solve.  Defensive throughout: the registry belongs to
    slice A and may be absent, empty, or mid-write.
    """
    mode = _mode()
    if mode == "off":
        return 0
    claims_by_hex = getattr(state, "known_claims", None)
    if not claims_by_hex:
        return 0

    now_ms = int(time.time() * 1000)
    attempts = 0
    for raw_hex, dq in list(claims_by_hex.items()):
        hexn = normalize_hex_key(raw_hex)
        if not hexn:
            continue
        claims = _select_claims(dq, now_ms)
        if not claims:
            continue
        newest_ts = max(int(c["ts_ms"]) for c in claims.values())
        if _last_attempt_ts_ms.get(hexn, -1) >= newest_ts:
            continue
        s_in = _build_solver_input(hexn, claims)
        if s_in is None:
            continue
        if node_cfgs is None:
            # Deferred to first need: pulling the config snapshot costs a
            # lock, and most passes find nothing new to solve.
            from services.frame_processor import get_node_configs

            node_cfgs = get_node_configs()
        # Stamped before the solve, not after: a solve that raises must not
        # be retried every pass against the same claims forever.
        _last_attempt_ts_ms[hexn] = newest_ts
        _attempt(hexn, s_in, node_cfgs, solve_fn, mode)
        attempts += 1

    # Opportunistic TTL sweep of the dedup map (see _ATTEMPT_TTL_S).
    cutoff_ms = now_ms - _ATTEMPT_TTL_S * 1000.0
    for h in [h for h, ts in _last_attempt_ts_ms.items() if ts < cutoff_ms]:
        del _last_attempt_ts_ms[h]
    return attempts


def maybe_run_pass(solve_fn) -> None:
    """Interval- and mode-gated pass entry point for the solver worker loop.

    Must never raise: it runs on the solver worker threads, and an escaped
    exception here would kill queue draining exactly the way the 2026-08-08
    trail-race outage did.  The try-lock means concurrent workers skip
    rather than queue, and the interval check lives under the same lock so
    two workers cannot both pass it in the same window.
    """
    global _last_pass_ts
    try:
        if _mode() == "off":
            return
        if not _PASS_LOCK.acquire(blocking=False):
            return
        try:
            now = time.time()
            if now - _last_pass_ts < _PASS_MIN_INTERVAL_S:
                return
            _last_pass_ts = now
            run_known_lane_pass(solve_fn)
        finally:
            _PASS_LOCK.release()
    except Exception:
        logging.exception("Known-lane pass failed")
