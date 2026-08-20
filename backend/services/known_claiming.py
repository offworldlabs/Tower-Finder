"""Known-target claiming (KNOWN_LANE_MODE): bind detections to known ADS-B
aircraft BEFORE the dark lane ever sees them.

Today an ADS-B tag is advisory — a tagged detection flows through the same
tracker and association gates a dark one does, so a lit airliner's echo can
still cross-pair with a dark tracklet into a phantom solve.  This stage runs
per frame, ahead of the tracker: detections that match a known transponder
are recorded in state.known_claims (the known lane's input), and in binding
mode they are removed from the frame the dark lane processes, so they cannot
cross-pair at all.  The dark pool is then dark by construction, not by
gate-luck.

Assignment is GLOBAL one-to-one (scipy linear_sum_assignment) rather than the
greedy pass associate_detections_to_adsb uses: two aircraft whose predicted
observations sit within one gate width of each other are exactly the case
this lane exists for, and greedy resolves that crossing by letting the best
single score steal the only aircraft the other detection could bind to —
leaving a real lit echo in the dark pool.  Hungarian minimises the summed
score, so a feasible complete matching always beats a partial one.

Gate/score shape follows associate_detections_to_adsb (per-axis gates, score
= d_res/d_gate + f_res/f_gate, same constants as defaults) with one addition:
the allowance grows with fix age.  Dead-reckoning error is ~linear in coast
time (a 10 m/s velocity error is ~450 m at the 45 s age cap, i.e. 1.5–3 µs of
delay — comparable to the base gate itself), so a fixed gate is simultaneously
too loose for a fresh fix and too tight for an old one.
"""

import logging
import math
from collections import deque

import numpy as np
from retina_analytics.association import (
    ADSB_SEED_DELAY_GATE_US,
    ADSB_SEED_DOPPLER_GATE_HZ,
    ADSB_SEED_MAX_DR_AGE_S,
    CLAIM_DELAY_GATE_US,
    CLAIM_DOPPLER_GATE_HZ,
    CLAIM_MAX_DR_AGE_S,
    claim_eligible,
    predict_observation,
)
from retina_analytics.constants import offset_latlon_m
from scipy.optimize import linear_sum_assignment

from config.constants import FT_TO_M
from core import state
from services.id_utils import normalize_hex_key

# Same base constants as the seeding path: the comparison is the identical
# "measurement vs dead-reckoned ADS-B fix" shape, so a different base gate
# here would just be a second opinion on the same physics.
KNOWN_CLAIM_DELAY_GATE_US = ADSB_SEED_DELAY_GATE_US
KNOWN_CLAIM_DOPPLER_GATE_HZ = ADSB_SEED_DOPPLER_GATE_HZ
KNOWN_CLAIM_MAX_FIX_AGE_S = ADSB_SEED_MAX_DR_AGE_S

# Sentinel cost for a gated-out (detection, aircraft) cell.  A large finite
# value rather than inf: linear_sum_assignment raises "cost matrix is
# infeasible" when a full matching cannot avoid inf, and an unclaimable
# detection is the normal case (clutter, dark targets), not an error.  Any
# real score is <= 2.0 by construction, so >= this means "not assigned".
_GATE_INFEASIBLE = 1.0e6

_logger = logging.getLogger(__name__)

# ── services.node_bias (trust slice) — optional at import time ────────────────
# The residual consumer is developed in a sibling slice and may not exist in
# this tree yet.  Resolved once, lazily: retrying the import per frame would
# put an exception on the hot path forever in trees where it never lands.
_node_bias_mod = None
_node_bias_unavailable = False


def _node_bias():
    global _node_bias_mod, _node_bias_unavailable
    if _node_bias_mod is None and not _node_bias_unavailable:
        try:
            from services import node_bias

            _node_bias_mod = node_bias
        except ImportError:
            _node_bias_unavailable = True
            _logger.info("services.node_bias not present — claim residuals unrecorded, no untrusted-hex filter")
    return _node_bias_mod


def _untrusted_hexes() -> frozenset:
    """Hexes whose transponder data has been judged to lie (trust slice).

    A lying transponder must never pull a detection out of the dark pool —
    binding to it would hide the very target it is lying about — so these are
    skipped as claim candidates on BOTH paths, node-tagged included: the
    node's correlation is trusted about *which echo* matches the broadcast,
    not about the broadcast being true.
    """
    nb = _node_bias()
    if nb is None:
        return frozenset()
    return frozenset(normalize_hex_key(h) for h in nb.get_untrusted_hexes())


def _reset_for_tests() -> None:
    """Forget the node_bias import verdict.  Tests only — lets a test inject
    a fake services.node_bias after an earlier test already cached the
    ImportError."""
    global _node_bias_mod, _node_bias_unavailable
    _node_bias_mod = None
    _node_bias_unavailable = False


def _gate_scale(age_s: float) -> float:
    """Gate allowance multiplier for a fix age: 1.0 fresh, 2.0 at the age cap.

    Linear because the dominant error it covers is linear: dead-reckoning
    drift is velocity error × coast time.  Doubling (not more) at the cap
    keeps a stale fix from claiming across a neighbouring aircraft's gate —
    past 2× the base gates the two failure modes trade places.
    """
    return 1.0 + min(abs(age_s), KNOWN_CLAIM_MAX_FIX_AGE_S) / KNOWN_CLAIM_MAX_FIX_AGE_S


def _tag_velocity(tag: dict) -> tuple[float, float]:
    """(vel_east, vel_north) m/s from a node adsb entry's gs (kt) / track (deg),
    the same conversion state._adsb_for_seeding applies to the cache."""
    gs_ms = (tag.get("gs", 0) or 0) * 0.514444
    trk = math.radians(tag.get("track", 0) or 0)
    return gs_ms * math.sin(trk), gs_ms * math.cos(trk)


def _dark_global_projections(geo, frame_ts_s: float) -> list[tuple[float, float]]:
    """(pred_delay_us, pred_doppler_hz) of every established dark global at
    this node, dead-reckoned to frame time — the contention reference set.

    Reuses the top-down claiming machinery verbatim (claim_eligible, the
    CLAIM_* gates' DR-age cap, the same projection) so "gates against a dark
    track" means exactly what it means in _claim_round: a claim is contested
    when ASSOC_CLAIM_MODE's claimer would ALSO have taken this detection.
    """
    out = []
    for g in state._global_tracks_for_claiming():
        if not claim_eligible(g):
            continue
        dt = frame_ts_s - g.get("timestamp_ms", 0) / 1000.0
        if not (0.0 <= dt <= CLAIM_MAX_DR_AGE_S):
            continue
        dr_lat, dr_lon = offset_latlon_m(
            g["lat"],
            g["lon"],
            east_m=g.get("vel_east", 0.0) * dt,
            north_m=g.get("vel_north", 0.0) * dt,
        )
        out.append(
            predict_observation(
                geo,
                dr_lat,
                dr_lon,
                g.get("alt_m", 0.0) / 1000.0,
                g.get("vel_east", 0.0),
                g.get("vel_north", 0.0),
                g.get("vel_up", 0.0),
            )
        )
    return out


def _is_contested(delay_us: float, doppler_hz: float, projections: list[tuple[float, float]]) -> bool:
    return any(
        abs(pd - delay_us) <= CLAIM_DELAY_GATE_US and abs(pf - doppler_hz) <= CLAIM_DOPPLER_GATE_HZ
        for pd, pf in projections
    )


def claim_known_targets(node_id: str, frame: dict) -> set[int]:
    """Run the claiming stage for one frame; return the claimed detection
    indices.

    Records every claim in state.known_claims and bumps known_claims_made /
    known_claim_contentions regardless of mode — the caller decides whether
    the returned indices actually leave the dark pool (binding only).

    Two claim paths, in precedence order:
      1. Node-supplied frame["adsb"] entries become claims directly.  The
         node's own correlation is authoritative (existing invariant — the
         backend never overwrites a node-provided list), so it is not
         re-gated; the prediction is still computed so the record carries
         the residual the trust path needs.
      2. Remaining detections × fresh cached ADS-B states, global one-to-one
         via linear_sum_assignment under age-scaled gates.

    Claims nothing without a registered geometry: the registry contract
    requires the predicted observation, and there is nothing to predict
    with.  Fail toward dark, the same discipline every ADS-B doubt-case in
    this pipeline follows.
    """
    delays = frame.get("delay") or []
    dopplers = frame.get("doppler") or []
    if not delays:
        return set()
    geo = state.node_associator.node_geometries.get(node_id)
    if geo is None:
        return set()

    ts_ms = int(frame.get("timestamp", 0))
    frame_ts_s = ts_ms / 1000.0
    untrusted = _untrusted_hexes()

    # (det_idx, hexn, adsb_fix, pred_delay_us, pred_doppler_hz)
    claims: list[tuple[int, str, dict, float, float]] = []
    claimed_idx: set[int] = set()
    claimed_hexes: set[str] = set()

    # ── Path 1: node-supplied tags ────────────────────────────────────────────
    node_tags = frame.get("adsb")
    if node_tags:
        for i, tag in enumerate(node_tags):
            if i >= len(delays):
                break
            if not isinstance(tag, dict):
                continue
            hexn = normalize_hex_key(tag.get("hex") or tag.get("icao"))
            if not hexn or hexn in untrusted or hexn in claimed_hexes:
                continue
            lat, lon = tag.get("lat"), tag.get("lon")
            if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            ve, vn = _tag_velocity(tag)
            # The node correlated this fix against this frame, so the fix is
            # taken as current — no dead-reckoning, fix_ts_ms = frame time.
            pred_d, pred_f = predict_observation(
                geo, lat, lon, (tag.get("alt_baro", 0) or 0) * FT_TO_M / 1000.0, ve, vn
            )
            fix = {
                "lat": lat,
                "lon": lon,
                "alt_baro": tag.get("alt_baro"),
                "gs": tag.get("gs"),
                "track": tag.get("track"),
                "fix_ts_ms": ts_ms,
            }
            claims.append((i, hexn, fix, pred_d, pred_f))
            claimed_idx.add(i)
            claimed_hexes.add(hexn)

    # ── Path 2: assignment over untagged detections × fresh cached states ────
    free = [i for i in range(len(delays)) if i not in claimed_idx]
    if free:
        cands = []
        for hexn, st in state._adsb_for_seeding().items():
            if hexn in claimed_hexes or hexn in untrusted:
                continue
            age_s = frame_ts_s - st.get("timestamp_ms", 0) / 1000.0
            if abs(age_s) > KNOWN_CLAIM_MAX_FIX_AGE_S:
                continue
            dr_lat, dr_lon = offset_latlon_m(
                st["lat"],
                st["lon"],
                east_m=st.get("vel_east", 0.0) * age_s,
                north_m=st.get("vel_north", 0.0) * age_s,
            )
            pred_d, pred_f = predict_observation(
                geo,
                dr_lat,
                dr_lon,
                st.get("alt_m", 0.0) / 1000.0,
                st.get("vel_east", 0.0),
                st.get("vel_north", 0.0),
            )
            cands.append((hexn, st, pred_d, pred_f, _gate_scale(age_s)))

        if cands:
            cost = np.full((len(free), len(cands)), _GATE_INFEASIBLE)
            for c, (_hexn, _st, pred_d, pred_f, scale) in enumerate(cands):
                d_gate = KNOWN_CLAIM_DELAY_GATE_US * scale
                f_gate = KNOWN_CLAIM_DOPPLER_GATE_HZ * scale
                for r, i in enumerate(free):
                    d_res = abs(pred_d - float(delays[i]))
                    f_res = abs(pred_f - float(dopplers[i]))
                    if d_res > d_gate or f_res > f_gate:
                        continue
                    cost[r, c] = d_res / d_gate + f_res / f_gate
            rows, cols = linear_sum_assignment(cost)
            for r, c in zip(rows, cols):
                if cost[r, c] >= _GATE_INFEASIBLE:
                    continue
                i = free[r]
                hexn, st, pred_d, pred_f, _scale = cands[c]
                claims.append(
                    (
                        i,
                        hexn,
                        {
                            # REPORTED fix, not the dead-reckoned one — same
                            # rule as associate_detections_to_adsb, so a claim
                            # and a node tag for one aircraft carry the same
                            # position and downstream consumers need not know
                            # which path produced it.
                            "lat": st["lat"],
                            "lon": st["lon"],
                            "alt_baro": st.get("alt_baro"),
                            "gs": st.get("gs"),
                            "track": st.get("track"),
                            "fix_ts_ms": st.get("timestamp_ms", 0),
                        },
                        pred_d,
                        pred_f,
                    )
                )
                claimed_idx.add(i)

    if not claims:
        return set()

    # ── Contention, registry, counters, residual hook ─────────────────────────
    projections = _dark_global_projections(geo, frame_ts_s)
    nb = _node_bias()
    for i, hexn, fix, pred_d, pred_f in claims:
        d_meas = float(delays[i])
        f_meas = float(dopplers[i])
        # A detection both a known hex and an established dark track can
        # explain is a genuine ambiguity: claim it (identity evidence beats a
        # dark projection) but say so, and leave the dark track alone — if
        # the dark track is real, its other nodes keep feeding it; if it is
        # this aircraft's own ghost, it expires on its own.  Deleting it here
        # would let a claim silently erase a possibly-real dark target.
        contested = _is_contested(d_meas, f_meas, projections)
        dq = state.known_claims.get(hexn)
        if dq is None:
            dq = state.known_claims.setdefault(hexn, deque(maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX))
        dq.append(
            {
                "node_id": node_id,
                "delay_us": d_meas,
                "doppler_hz": f_meas,
                "pred_delay_us": float(pred_d),
                "pred_doppler_hz": float(pred_f),
                "ts_ms": ts_ms,
                "adsb_fix": fix,
                "contested": contested,
            }
        )
        state.bump_counter("known_claims_made")
        if contested:
            state.bump_counter("known_claim_contentions")
        if nb is not None:
            # Signed, measured minus predicted: the trust path estimates
            # per-node bias, and |residual| throws away the direction that
            # makes a bias a bias.
            nb.record_claim_residual(node_id, hexn, d_meas - pred_d, f_meas - pred_f, ts_ms)

    return claimed_idx


# Frame keys aligned by detection index.  snr and adsb may legitimately be
# absent; anything absent or non-list is passed through untouched.
_INDEXED_FRAME_KEYS = ("delay", "doppler", "snr", "adsb")


def strip_claimed_detections(frame: dict, claimed_idx: set[int]) -> dict:
    """Copy of `frame` with the claimed indices removed from every
    index-aligned list.

    A copy, never in-place: the original frame still feeds the archive and
    the ADS-B cache extraction downstream, and both must see what the node
    actually sent — binding changes which lane processes a detection, not
    the record of its existence.
    """
    n = len(frame.get("delay") or [])
    keep = [i for i in range(n) if i not in claimed_idx]
    out = dict(frame)
    for key in _INDEXED_FRAME_KEYS:
        v = frame.get(key)
        if isinstance(v, list):
            out[key] = [v[i] for i in keep if i < len(v)]
    return out
