"""Shared helpers for the aircraft feed: dedup, history, motion, identity.

Extracted from frame_processor.py (1553 lines, six responsibilities) — these
are the pure-ish helpers the feed builder, the track gates and the frame
processor all share.  They import only core state + geodesy, so every other
feed module can depend on them without cycles.
"""

import math
from collections import deque

from core import state
from services.geo import enu_km, haversine_km
from services.id_utils import normalize_hex_key

# Was a flat-earth approximation at 111.0 km/deg — 0.175% short of the
# spherical form used by every gate this feeds.
position_distance_km = haversine_km


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only.

    Currently a no-op: this module keeps no private mutable state (the
    arc-motion log lives on core.state).  Kept so conftest's autouse reset
    doesn't need to special-case the module.
    """


# ── Aircraft de-duplication ───────────────────────────────────────────────────
# Preference when several entries describe one aircraft. A multinode fix is
# constrained by >=2 receivers; a single-node ellipse arc is only an ambiguity
# locus, so it loses.
_DEDUP_SOURCE_RANK = {
    "multinode_solve": 0,
    "solver_adsb_seed": 1,
    "solver_single_node": 2,
    "single_node_ellipse_arc": 3,
}
# Deliberately tighter than the 6 km solver association gate. Observed duplicate
# spread is ~1 km, while real IFR separation is 5 nm (~9.3 km) laterally or
# 1000 ft vertically — so this collapses duplicates with margin to spare while
# staying well clear of merging two genuinely separate aircraft. Under-merging
# leaves a cosmetic double; over-merging would hide real traffic.
_DEDUP_PROXIMITY_KM = 3.0
_DEDUP_ALT_GATE_FT = 2000.0


def _looks_like_same_aircraft(a: dict, b: dict) -> bool:
    """Proximity + altitude test for entries with no shared identity."""
    for k in ("lat", "lon"):
        if not isinstance(a.get(k), (int, float)) or not isinstance(b.get(k), (int, float)):
            return False
    if position_distance_km(a["lat"], a["lon"], b["lat"], b["lon"]) > _DEDUP_PROXIMITY_KM:
        return False
    alt_a, alt_b = a.get("alt_baro"), b.get("alt_baro")
    if isinstance(alt_a, (int, float)) and isinstance(alt_b, (int, float)):
        if abs(alt_a - alt_b) > _DEDUP_ALT_GATE_FT:
            return False
    return True


def dedup_aircraft(aircraft: list[dict]) -> list[dict]:
    """Collapse entries that describe the same physical aircraft.

    Two entries can describe one aircraft and never share a `hex`: a multinode
    solve is named mn<sha>, a single-node arc by ICAO or pr<id>. The `seen_hex`
    set in the builder is exact string equality, so it can never merge them —
    which is why one target could render as several icons.

    Grouped by ground_truth_hex where available, else by proximity. The
    ground-truth path is simulation-only (resolve_ground_truth_hex reads
    state.ground_truth_trails), so the proximity fallback is what carries real
    hardware.
    """
    groups: list[list[dict]] = []
    by_truth: dict[str, int] = {}

    for ac in aircraft:
        gt = ac.get("ground_truth_hex")
        if gt:
            idx = by_truth.get(gt)
            if idx is None:
                by_truth[gt] = len(groups)
                groups.append([ac])
            else:
                groups[idx].append(ac)
            continue
        for g in groups:
            if _looks_like_same_aircraft(ac, g[0]):
                g.append(ac)
                break
        else:
            groups.append([ac])

    out: list[dict] = []
    for g in groups:
        if len(g) == 1:
            out.append(g[0])
            continue
        g.sort(key=lambda x: _DEDUP_SOURCE_RANK.get(x.get("position_source"), 99))
        winner = g[0]
        # Carry every contributing node onto the survivor so node filtering
        # (aircraft_flush.filter_payload_to_nodes) and the frontend's
        # node-highlight still see this aircraft under each node that saw it.
        nodes: list[str] = []
        for m in g:
            for nid in [m.get("node_id"), *(m.get("contributing_node_ids") or [])]:
                if nid and nid not in nodes:
                    nodes.append(nid)
        if nodes:
            winner["contributing_node_ids"] = nodes
        out.append(winner)

    return out


def append_track_history(hex_code: str, lat: float, lon: float, alt_ft: float, ts: float):
    """Append a position to the rolling track history for a hex."""
    if hex_code not in state.track_histories:
        state.track_histories[hex_code] = deque(maxlen=state.TRACK_HISTORY_MAX)
    hist = state.track_histories[hex_code]
    if hist:
        dlat = abs(hist[-1][0] - lat)
        dlon = abs(hist[-1][1] - lon)
        if dlat < 0.00005 and dlon < 0.00005:
            return
    hist.append([round(lat, 6), round(lon, 6), round(alt_ft, 0), round(ts, 1)])


# Minimum displacement (metres) before a new arc-motion sample is appended.
# Smaller than this counts as "same position" — sub-100 m drift on a stationary
# arc midpoint isn't a real velocity signal.
_ARC_MOTION_MIN_M = 100.0
# Number of distinct positions retained per track.  Long enough to span a few
# bistatic frames (~40 s apart) so velocity estimation can average over
# 60–120 s windows without holding state forever.
_ARC_MOTION_LOG_MAX = 6


def _record_arc_motion(ac_hex: str, lat: float, lon: float, ts: float) -> None:
    """Log distinct emitted positions for later velocity estimation.

    Only entries that have moved > _ARC_MOTION_MIN_M from the last logged
    position are appended — repeated emits at the same arc midpoint between
    detections would otherwise drown out real motion.
    """
    log = state.track_arc_motion.get(ac_hex)
    if log:
        plat, plon, _pts = log[-1]
        if haversine_km(lat, lon, plat, plon) * 1000.0 < _ARC_MOTION_MIN_M:
            return
    else:
        log = []
        state.track_arc_motion[ac_hex] = log
    log.append((lat, lon, ts))
    if len(log) > _ARC_MOTION_LOG_MAX:
        log.pop(0)


def _estimate_velocity_ms_from_motion(
    ac_hex: str, lat: float, lon: float, now: float,
) -> tuple[float, float] | None:
    """Return (vel_east_ms, vel_north_ms) inferred from arc-midpoint motion.

    Searches the per-track motion log for the oldest sample 15–120 s old
    that's > 200 m from the current position; uses that displacement to
    derive an averaged east/north velocity.  Returns None if no sample is
    recent-enough-but-old-enough or the displacement is too small to trust.

    No upper speed bound: supersonic targets are in scope, so a fast
    displacement is reported as-is rather than rejected as implausible.
    """
    log = state.track_arc_motion.get(ac_hex)
    if not log:
        return None
    for plat, plon, pts in log:
        dt = now - pts
        if dt < 15:
            continue  # too recent — noise dominates
        if dt > 120:
            continue  # too old — aircraft may have manoeuvred
        east_m, north_m = (c * 1000.0 for c in enu_km(plat, plon, lat, lon))
        dist_m = math.hypot(east_m, north_m)
        if dist_m < 200:
            continue  # essentially still — can't infer velocity
        return east_m / dt, north_m / dt
    return None


def _estimate_velocity_from_motion(
    ac_hex: str, lat: float, lon: float, now: float,
) -> tuple[float, float] | None:
    """Return (gs_knots, track_deg) inferred from arc-midpoint motion, or None.

    Thin wrapper over _estimate_velocity_ms_from_motion that converts to the
    knots/degrees form used by the emitted aircraft entry.
    """
    ms = _estimate_velocity_ms_from_motion(ac_hex, lat, lon, now)
    if ms is None:
        return None
    vel_e_ms, vel_n_ms = ms
    gs_kn = math.hypot(vel_e_ms, vel_n_ms) * 1.94384
    bearing_deg = math.degrees(math.atan2(vel_e_ms, vel_n_ms)) % 360.0
    return round(gs_kn, 1), round(bearing_deg, 1)


def resolve_ground_truth_hex(
    ac_hex: str, lat: float, lon: float, max_distance_km: float = 8.0,
) -> str | None:
    """Find the best ground-truth hex for a solved aircraft."""
    norm_hex = normalize_hex_key(ac_hex)
    if norm_hex and norm_hex in state.ground_truth_trails and state.ground_truth_trails[norm_hex]:
        return norm_hex

    best_hex = None
    best_distance = max_distance_km
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        last = trail[-1]
        dist = position_distance_km(lat, lon, last[0], last[1])
        if dist <= best_distance:
            best_distance = dist
            best_hex = gt_hex
    return best_hex

