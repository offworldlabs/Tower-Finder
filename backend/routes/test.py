"""Test network dashboard, ground-truth validation endpoints."""

import logging
import math
import os
import time
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import Response

from core import state
from core.task_registry import get_stale_tasks
from core.users import require_admin
from services.frame_processor import resolve_ground_truth_hex
from services.geo import haversine_km
from services.id_utils import normalize_hex_key
from services.tasks import solver as solver_mod

router = APIRouter()
logger = logging.getLogger(__name__)

_RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")
_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()

# In production the simulation-injection endpoints would otherwise fail-open
# (any caller can push fake aircraft / ground truth) if RADAR_API_KEY is left
# unset. Refuse to start rather than silently expose them.
if not _RADAR_API_KEY and _RETINA_ENV == "production":
    raise RuntimeError(
        "RADAR_API_KEY environment variable is required when "
        "RETINA_ENV=production. /api/test/ground-truth/push and "
        "/api/sim/adsb/push would otherwise accept unauthenticated callers."
    )
if not _RADAR_API_KEY:
    logger.warning(
        "RADAR_API_KEY is not set (RETINA_ENV=%r) — /api/test/ground-truth/push "
        "and /api/sim/adsb/push accept ANY caller without authentication.",
        _RETINA_ENV or "unset",
    )


def _verify_sim_key(x_api_key: str = Header(default="", alias="X-API-Key")):
    """Require X-API-Key for simulation data injection endpoints.

    Production fails fast at import time when no key is configured, so by the
    time we reach this function in prod a key always exists and we always
    enforce it. Outside production the check is opt-in for dev convenience.
    """
    if _RADAR_API_KEY and x_api_key != _RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# Was a byte-identical copy of routes/admin.py's; the rule now lives beside the
# interval table it reads.
_get_stale_tasks = get_stale_tasks


# Module-level reference set from main.py at startup
_default_pipeline = None


def init(pipeline):
    global _default_pipeline
    _default_pipeline = pipeline


@router.get("/api/test/dashboard")
async def test_network_dashboard():
    body = _build_dashboard_data()
    return Response(content=body, media_type="application/json")


def _build_dashboard_data() -> bytes:
    # Snapshot mutable dicts to avoid RuntimeError from concurrent mutation
    with state.connected_nodes_lock:
        _cn_snapshot = list(state.connected_nodes.values())
    _pipelines_snapshot = list(state.node_pipelines.values())

    total_nodes = len(_cn_snapshot)
    active_nodes = sum(1 for n in _cn_snapshot if n.get("status") not in ("disconnected",))
    synthetic_nodes = sum(1 for n in _cn_snapshot if n.get("is_synthetic"))

    total_tracks = sum(len(p.tracker.tracks) for p in _pipelines_snapshot) if _pipelines_snapshot else 0
    total_tracks += (
        len(_default_pipeline.tracker.tracks) if _default_pipeline and hasattr(_default_pipeline, "tracker") else 0
    )
    geolocated = sum(len(p.geolocated_tracks) for p in _pipelines_snapshot) if _pipelines_snapshot else 0
    geolocated += (
        len(_default_pipeline.geolocated_tracks)
        if _default_pipeline and hasattr(_default_pipeline, "geolocated_tracks")
        else 0
    )
    mn_tracks = len(state.multinode_tracks)
    adsb_tracks = len(state.adsb_aircraft)
    n_aircraft = len(state.latest_aircraft_json.get("aircraft", []))

    analytics_nodes = len(state.node_analytics.trust_scores)
    avg_trust = 0.0
    if state.node_analytics.trust_scores:
        scores = [ts.score for ts in list(state.node_analytics.trust_scores.values()) if hasattr(ts, "score")]
        avg_trust = sum(scores) / len(scores) if scores else 0

    blocked_nodes = sum(
        1 for r in list(state.node_analytics.reputations.values()) if hasattr(r, "reputation") and r.reputation < 0.1
    )
    n_overlaps = len(state.node_associator.overlap_zones) if hasattr(state.node_associator, "overlap_zones") else 0
    ws_clients = len(state.ws_clients)
    ext_adsb = len(state.external_adsb_cache)

    return orjson.dumps(
        {
            "status": "running",
            "environment": os.getenv("RETINA_ENV", "production"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes": {
                "total": total_nodes,
                "active": active_nodes,
                "synthetic": synthetic_nodes,
                "real": total_nodes - synthetic_nodes,
            },
            "pipeline": {
                "active_tracks": total_tracks,
                "geolocated_tracks": geolocated,
                "multinode_tracks": mn_tracks,
                "adsb_aircraft": adsb_tracks,
                "node_pipelines": len(state.node_pipelines),
                "aircraft_on_map": n_aircraft,
            },
            "analytics": {
                "nodes_with_analytics": analytics_nodes,
                "average_trust_score": round(avg_trust, 4),
                "blocked_nodes": blocked_nodes,
            },
            "association": {"overlap_zones": n_overlaps},
            "streaming": {
                "websocket_clients": ws_clients,
                "external_adsb_cached": ext_adsb,
            },
            "server_health": {
                "frame_queue_depth": state.frame_queue.qsize(),
                "frame_queue_max": state.frame_queue.maxsize,
                "frames_dropped": state.frames_dropped,
                "frame_queue_utilization_pct": round(
                    state.frame_queue.qsize() / max(state.frame_queue.maxsize, 1) * 100, 1
                ),
            },
            "chain_of_custody": {
                "registered_keys": len(state.node_identities),
                "chain_entries_total": sum(len(e) for e in list(state.chain_entries.values())),
                "iq_commitments_total": sum(len(c) for c in list(state.iq_commitments.values())),
                "nodes_with_chains": len(state.chain_entries),
            },
            "subsystem_health": {
                "tcp_server": "ok",
                "radar_pipeline": "ok" if _default_pipeline and hasattr(_default_pipeline, "tracker") else "error",
                "node_analytics": "ok" if analytics_nodes > 0 or total_nodes == 0 else "waiting",
                "inter_node_association": "ok" if n_overlaps > 0 or active_nodes < 2 else "waiting",
                "data_archival": "ok",
                "websocket_broadcast": "ok",
                "aircraft_feed": "ok",
                "chain_of_custody": "ok" if len(state.node_identities) > 0 or total_nodes == 0 else "waiting",
            },
            "solver": {
                "successes": state.solver_successes,
                "failures": state.solver_failures,
                # n=2 solves that succeeded but were withheld from the map
                # because their track pairing did not clear the chi2 gate (or
                # was outbid for a shared single-node track).  Distinct from
                # failures: the solve worked, it just did not earn publication.
                # Only observable here — the per-solve reason is logged at DEBUG,
                # which staging does not emit.
                "n2_unconfirmed": state.n2_unconfirmed,
                # Per-reason breakdown of solver_failures (same aggregate as
                # above): which gate is eating the solves.
                "failures_by_reason": {
                    "exception": state.solver_fail_exception,
                    "unconverged": state.solver_fail_unconverged,
                    "rms_delay": state.solver_fail_rms_delay,
                    "rms_doppler": state.solver_fail_rms_doppler,
                    "beam": state.solver_fail_beam,
                    "displacement": state.solver_fail_displacement,
                },
                # Overlap grids rebuilt because a node's observed coverage
                # tightened, and how many nodes triggered it.  Zero against
                # populated polygons means the prior is not reaching the grids.
                "coverage_rebuilds": state.coverage_rebuilds,
                "coverage_rebuild_nodes": state.coverage_rebuild_nodes,
                "queue_drops": state.solver_queue_drops,
                # Items discarded unsolved after aging out in the queue.  The
                # queue-full and too-slow failure modes are distinct: drops
                # here with queue_drops at 0 means the drain rate collapsed,
                # not the queue size.
                "stale_drops": state.solver_stale_drops,
                # Multinode entries replaced because a later solve consumed
                # the same source tracks under a new key (fragmented re-solve).
                "mn_superseded": state.mn_superseded,
                # Solves published after node-trimming recovered them from
                # the rms_delay gate at n>=4.  See solver.py's
                # _trim_and_resolve.
                "solver_trimmed": state.solver_trimmed,
                # Consensus hypothesis stage (solver.py's _consensus_select).
                # mode is off/shadow/active (SOLVER_CONSENSUS_MODE); the
                # counters are cumulative since boot regardless of mode —
                # shadow/selected/filtered only move once mode isn't "off".
                "consensus": {
                    "mode": solver_mod._CONSENSUS_MODE,
                    "selected": state.solver_consensus_selected,
                    "filtered": state.solver_consensus_filtered,
                    "fallback": state.solver_consensus_fallback,
                    "shadow": state.solver_consensus_shadow,
                },
                # Teleporting emits (mis-association noise).  Debug counter
                # only — jumps no longer mark tracks anomalous.
                "position_jump_events": state.position_jump_events,
                "last_latency_s": round(state.solver_last_latency_s, 3),
                "avg_latency_s": round(state.solver_total_latency_s / max(state.solver_total_solved, 1), 3),
            },
            "mlat_verification": _mlat_verification_summary(),
            "task_health": {
                "last_success": dict(state.task_last_success),
                "error_counts": dict(state.task_error_counts),
                "stale_tasks": _get_stale_tasks(),
            },
        }
    )


@router.post("/api/test/validate")
async def validate_ground_truth(body: dict = Body(...), _key=Depends(_verify_sim_key)):
    # _verify_sim_key: this was the one POST here with no auth dependency at
    # all — an unauthenticated compute endpoint over the full aircraft list.
    truth_list = body.get("ground_truth", [])
    if not truth_list:
        raise HTTPException(status_code=400, detail="ground_truth list required")

    server_aircraft = state.latest_aircraft_json.get("aircraft", [])
    matches = []
    unmatched_truth = []
    matched_server_indices: set[int] = set()

    for gt in truth_list:
        gt_lat = gt.get("lat", 0)
        gt_lon = gt.get("lon", 0)
        gt_alt = gt.get("alt_km", 0) * 1000

        best_match = None
        best_dist = float("inf")
        for i, sa in enumerate(server_aircraft):
            if i in matched_server_indices:
                continue
            sa_lat, sa_lon = sa.get("lat", 0), sa.get("lon", 0)
            if sa_lat == 0 and sa_lon == 0:
                continue
            dist_km = haversine_km(gt_lat, gt_lon, sa_lat, sa_lon)
            if dist_km < best_dist and dist_km < 50:
                best_dist = dist_km
                best_match = (i, sa)

        if best_match:
            idx, sa = best_match
            matched_server_indices.add(idx)
            sa_alt_m = sa.get("alt_baro", 0) * 0.3048 if sa.get("alt_baro") else 0
            alt_err_m = abs(gt_alt - sa_alt_m)
            matches.append(
                {
                    "truth_id": gt.get("id"),
                    "server_hex": sa.get("hex"),
                    "position_error_km": round(best_dist, 2),
                    "altitude_error_m": round(alt_err_m, 0),
                    "position_source": sa.get("position_source", "unknown"),
                    "has_adsb": gt.get("has_adsb", False),
                    "is_anomalous": gt.get("is_anomalous", False),
                }
            )
        else:
            unmatched_truth.append(gt.get("id", "unknown"))

    false_tracks = len(server_aircraft) - len(matched_server_indices)

    if matches:
        pos_errors = [m["position_error_km"] for m in matches]
        alt_errors = [m["altitude_error_m"] for m in matches]
        avg_pos_err = sum(pos_errors) / len(pos_errors)
        avg_alt_err = sum(alt_errors) / len(alt_errors)
        max_pos_err = max(pos_errors)
        accuracy_pct = len(matches) / len(truth_list) * 100
        sorted_pos = sorted(pos_errors)
        p50_pos = sorted_pos[len(sorted_pos) // 2]
        p95_pos = sorted_pos[int(len(sorted_pos) * 0.95)]
        sorted_alt = sorted(alt_errors)
        p50_alt = sorted_alt[len(sorted_alt) // 2]
        p95_alt = sorted_alt[int(len(sorted_alt) * 0.95)]
    else:
        avg_pos_err = avg_alt_err = max_pos_err = 0
        p50_pos = p95_pos = p50_alt = p95_alt = 0
        accuracy_pct = 0

    # Per-position_source breakdown
    by_source: dict[str, list[float]] = {}
    for m in matches:
        src = m.get("position_source", "unknown")
        by_source.setdefault(src, []).append(m["position_error_km"])
    source_breakdown = {}
    for src, errs in by_source.items():
        errs.sort()
        sn = len(errs)
        source_breakdown[src] = {
            "count": sn,
            "mean_km": round(sum(errs) / sn, 2),
            "median_km": round(errs[sn // 2], 2),
            "p95_km": round(errs[int(sn * 0.95)], 2),
        }

    return {
        "validation": {
            "truth_aircraft": len(truth_list),
            "server_aircraft": len(server_aircraft),
            "matched": len(matches),
            "unmatched_truth": len(unmatched_truth),
            "false_tracks": false_tracks,
            "detection_rate_pct": round(accuracy_pct, 1),
        },
        "accuracy": {
            "avg_position_error_km": round(avg_pos_err, 2),
            "median_position_error_km": round(p50_pos, 2),
            "p95_position_error_km": round(p95_pos, 2),
            "max_position_error_km": round(max_pos_err, 2),
            "avg_altitude_error_m": round(avg_alt_err, 0),
            "median_altitude_error_m": round(p50_alt, 0),
            "p95_altitude_error_m": round(p95_alt, 0),
        },
        "by_source": source_breakdown,
        "matches": matches[:50],
        "unmatched_ids": unmatched_truth[:20],
    }



@router.get("/api/test/ground-truth/{hex_code}")
async def get_ground_truth_trail(hex_code: str):
    norm_hex = normalize_hex_key(hex_code)
    solved_trail = list(state.track_histories.get(hex_code, [])) or list(state.track_histories.get(norm_hex, []))
    matched_hex = norm_hex
    gt_trail = list(state.ground_truth_trails.get(matched_hex, []))
    if not gt_trail and solved_trail:
        last = solved_trail[-1]
        fallback_hex = resolve_ground_truth_hex(norm_hex, last[0], last[1])
        if fallback_hex:
            matched_hex = fallback_hex
            gt_trail = list(state.ground_truth_trails.get(fallback_hex, []))

    if not gt_trail and not solved_trail:
        raise HTTPException(status_code=404, detail=f"No trail data for {hex_code}")

    position_error_km = None
    if gt_trail and solved_trail:
        gt_last = gt_trail[-1]
        sol_last = solved_trail[-1]
        position_error_km = round(
            haversine_km(sol_last[0], sol_last[1], gt_last[0], gt_last[1]), 3)

    return {
        "hex": hex_code,
        "ground_truth_hex": matched_hex,
        "ground_truth_trail": gt_trail,
        "solved_trail": solved_trail,
        "position_error_km": position_error_km,
        "ground_truth_points": len(gt_trail),
        "solved_points": len(solved_trail),
    }


@router.get("/api/test/anomalies")
async def get_anomaly_log():
    """Return the anomaly event log and currently flagged hex codes."""
    return Response(
        content=orjson.dumps(
            {
                "flagged_count": len(state.anomaly_hexes),
                "flagged_hexes": sorted(state.anomaly_hexes),
                "events": state.anomaly_log[-100:],
            }
        ),
        media_type="application/json",
    )


# ── Simulation physics config ─────────────────────────────────────────────────


@router.get("/api/simulation/config")
async def get_simulation_config():
    """Return current simulation physics configuration plus live object-type counts."""
    counts: dict[str, int] = {"anomalous": 0, "drone": 0, "aircraft": 0, "dark": 0, "total": 0}
    for meta in list(state.ground_truth_meta.values()):
        counts["total"] += 1
        if meta.get("is_anomalous"):
            counts["anomalous"] += 1
        elif meta.get("object_type") == "drone":
            counts["drone"] += 1
        elif not meta.get("has_adsb"):
            # Dark: an aircraft flying without a transponder.  Counted apart
            # from "aircraft" so the frac_dark knob is verifiable in one call.
            counts["dark"] += 1
        else:
            counts["aircraft"] += 1
    return Response(
        content=orjson.dumps({**state.simulation_config, "ground_truth_counts": counts}),
        media_type="application/json",
    )


@router.put("/api/simulation/config")
async def put_simulation_config(body: dict = Body(...), _admin=Depends(require_admin)):
    """Update simulation physics fractions.

    Accepted keys: frac_anomalous, frac_drone, frac_dark (0.0–1.0 each).
    Sum of the three must not exceed 1.0 — the remainder is commercial aircraft.
    Optional: max_range_km (10–400), min_aircraft (1–500), max_aircraft (1–500).

    Also accepted — fleet scene keys, deliberately NO defaults (state.py's
    only-if-set pattern: a fresh backend never ships these, so the fleet
    container falls back to its own env; applying one is an orchestrator
    self-restart, see fleet-entrypoint.sh / retina_simulation.orchestrator):
    n_nodes (int, 4–100), dual_fraction (0.0–1.0).
    """
    allowed = {
        "frac_anomalous", "frac_drone", "frac_dark", "max_range_km", "min_aircraft", "max_aircraft",
        "n_nodes", "dual_fraction",
    }
    updated = {}
    for k in allowed:
        if k in body:
            v = body[k]
            if k.startswith("frac_"):
                if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                    raise HTTPException(400, detail=f"{k} must be 0.0–1.0")
            elif k in ("max_range_km",):
                if not isinstance(v, (int, float)) or not (10 <= v <= 400):
                    raise HTTPException(400, detail=f"{k} must be 10–400")
            elif k in ("min_aircraft", "max_aircraft"):
                if not isinstance(v, int) or not (1 <= v <= 500):
                    raise HTTPException(400, detail=f"{k} must be int 1–500")
            elif k == "n_nodes":
                # bool is an int subclass in Python — True/False must not
                # sneak through as 1/0.
                if not isinstance(v, int) or isinstance(v, bool) or not (4 <= v <= 100):
                    raise HTTPException(400, detail=f"{k} must be int 4–100")
            elif k == "dual_fraction":
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 <= v <= 1.0):
                    raise HTTPException(400, detail=f"{k} must be 0.0–1.0")
            updated[k] = v

    total_frac = (
        updated.get("frac_anomalous", state.simulation_config["frac_anomalous"])
        + updated.get("frac_drone", state.simulation_config["frac_drone"])
        + updated.get("frac_dark", state.simulation_config["frac_dark"])
    )
    if total_frac > 1.0:
        raise HTTPException(400, detail="Sum of frac_anomalous + frac_drone + frac_dark must be ≤ 1.0")

    state.simulation_config.update(updated)
    state.simulation_config["_updated_at"] = time.time()
    return Response(
        content=orjson.dumps({"ok": True, "config": state.simulation_config}),
        media_type="application/json",
    )


@router.get("/api/simulation/ground-truth")
async def get_simulation_ground_truth():
    """Return current ground truth aircraft positions (last known fix, max 30 s old)
    plus a lightweight solver-performance summary computed from server state.
    """
    now = time.time()
    gt_aircraft = []
    for hx, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        trail_list = list(trail)
        lat, lon, alt_m, ts = trail_list[-1]
        if now - ts > 30:
            continue
        # Derive heading/speed from last 2 trail points for frontend dead-reckoning
        gs_knots = 0.0
        track_deg = 0.0
        if len(trail_list) >= 2:
            p1, p2 = trail_list[-2], trail_list[-1]
            dt = p2[3] - p1[3]
            if dt > 0.1:
                dlat_m = (p2[0] - p1[0]) * 111_320
                dlon_m = (p2[1] - p1[1]) * 111_320 * math.cos(math.radians(p1[0] or 1e-9))
                dist_m = math.hypot(dlat_m, dlon_m)
                gs_knots = round(dist_m / dt * 1.94384, 1)
                track_deg = round(math.degrees(math.atan2(dlon_m, dlat_m)) % 360, 1)
        meta = state.ground_truth_meta.get(hx, {})
        gt_aircraft.append(
            {
                "hex": hx,
                "lat": lat,
                "lon": lon,
                "alt_m": alt_m,
                "gs": gs_knots,
                "track": track_deg,
                "speed_ms": meta.get("speed_ms", 0),
                "heading": meta.get("heading", 0),
                "ts": round(ts, 3),
                "object_type": meta.get("object_type", "aircraft"),
                "is_anomalous": meta.get("is_anomalous", False),
            }
        )

    # ── solver performance ────────────────────────────────────────────────────
    gt_hex_set = {a["hex"] for a in gt_aircraft}
    gt_total = len(gt_hex_set)

    # Latest aircraft solved by the pipeline (what the map shows)
    solved_aircraft = state.latest_aircraft_json.get("aircraft", [])

    # Build solved-hex lookup (direct hex match + ground_truth_hex link)
    solved_by_hex: dict[str, list] = {}
    for ac in solved_aircraft:
        hx = ac.get("hex", "")
        if hx and "lat" in ac and "lon" in ac:
            solved_by_hex[hx] = [ac["lat"], ac["lon"]]
        gt_hx = ac.get("ground_truth_hex")
        if gt_hx and gt_hx not in solved_by_hex and "lat" in ac and "lon" in ac:
            solved_by_hex[gt_hx] = [ac["lat"], ac["lon"]]

    # Count unique GT objects that have at least one matching solved position
    # (by direct hex match or ground_truth_hex proximity link).
    # This avoids double-counting: multiple per-node tracks for the same
    # physical aircraft, or ADS-B + solver entries for the same target.
    matched_gt_hexes: set[str] = set()
    pos_errors: list[float] = []
    for hx in gt_hex_set:
        if hx in solved_by_hex:
            matched_gt_hexes.add(hx)
            trail = state.ground_truth_trails.get(hx)
            if trail:
                gt_last = list(trail)[-1]
                sol = solved_by_hex[hx]
                pos_errors.append(
                    haversine_km(sol[0], sol[1], gt_last[0], gt_last[1]))
            if len(pos_errors) >= 200:
                break

    detected_count = len(matched_gt_hexes)
    avg_err = round(sum(pos_errors) / len(pos_errors), 2) if pos_errors else None

    return Response(
        content=orjson.dumps(
            {
                "aircraft": gt_aircraft,
                "total": gt_total,
                "performance": {
                    "gt_total": gt_total,
                    "detected": detected_count,
                    "detection_rate_pct": round(detected_count / gt_total * 100, 1) if gt_total else 0.0,
                    "avg_position_error_km": avg_err,
                    "multinode_tracks": len(state.multinode_tracks),
                    "tracked_with_error": len(pos_errors),
                },
            }
        ),
        media_type="application/json",
    )


def _mlat_verification_summary() -> dict:
    """Return a lightweight summary of the latest MLAT verification for the dashboard."""
    try:
        data = orjson.loads(state.latest_mlat_verification_bytes)
        return {
            "n_solves": data.get("n_solves", 0),
            "n_matched": data.get("n_matched", 0),
            "match_rate_pct": data.get("match_rate_pct", 0.0),
            "position_mean_km": data.get("position", {}).get("mean_km", 0),
            "position_p95_km": data.get("position", {}).get("p95_km", 0),
            "altitude_mean_m": data.get("altitude", {}).get("mean_m", 0),
        }
    except Exception:
        return {}


# ── Per-node solver verification ──────────────────────────────────────────────

_RADAR3_NODE_ID = "radar3-retnode"


@router.get("/api/test/node/{node_id}/verification")
async def node_verification(node_id: str):
    """Return pre-computed solver-vs-ADS-B verification stats for one node."""
    return Response(
        content=state.latest_node_verification_bytes.get(node_id, b"{}"),
        media_type="application/json",
    )


@router.get("/api/test/radar3/verification")
async def radar3_verification():
    """Back-compat alias for the radar3 node's verification stats."""
    return await node_verification(_RADAR3_NODE_ID)


@router.get("/api/test/mlat-verification")
async def mlat_verification():
    """Return pre-computed multinode (MLAT) solver-vs-ground-truth verification stats."""
    return Response(
        content=state.latest_mlat_verification_bytes,
        media_type="application/json",
    )


@router.get("/api/test/mlat-history")
async def mlat_history(
    hex: str | None = None, all: int = 0, minutes: float = 30.0,
):
    """Per-solve MLAT history from the last ~30 minutes.

    ``?hex=mn...`` returns the published solves behind one map marker
    (matched on the solver-minted mn<sha256[:10]> hex), newest first, plus
    ``rejects_nearby``: gate-rejected solves within 10 km of the marker's
    newest published position — the signal for "the gates starved this track
    and the display held a stale point".  ``?all=1`` dumps the whole window
    for scripted debugging.  Records are written by the solver worker
    (services.tasks.solver._record_solve_history).
    """
    minutes = max(0.0, min(minutes, 35.0))
    cutoff_ms = int((time.time() - minutes * 60.0) * 1000)
    records = [
        r for r in list(state.mlat_solve_history) if r["ts_ms"] >= cutoff_ms
    ]
    records.reverse()  # newest first

    if all:
        payload = {
            "window_minutes": minutes,
            "n_records": len(records),
            "records": records[:1000],
        }
        return Response(content=orjson.dumps(payload), media_type="application/json")

    norm = (hex or "").strip().lower()
    if not norm:
        return Response(
            content=orjson.dumps({"error": "pass ?hex=mn... or ?all=1"}),
            media_type="application/json",
            status_code=400,
        )
    solves = [r for r in records if r.get("solver_hex") == norm]
    rejects_nearby: list[dict] = []
    if solves:
        ref = solves[0]
        ref_lat, ref_lon = ref.get("raw_lat"), ref.get("raw_lon")
        if ref_lat is not None and ref_lon is not None:
            rejects_nearby = [
                r
                for r in records
                if r.get("outcome") != "published"
                and r.get("raw_lat") is not None
                and haversine_km(ref_lat, ref_lon, r["raw_lat"], r["raw_lon"]) <= 10.0
            ]
    payload = {
        "hex": norm,
        "window_minutes": minutes,
        "n_solves": len(solves),
        "solves": solves[:500],
        "rejects_nearby": {
            "n": len(rejects_nearby),
            "by_outcome": {
                o: sum(1 for r in rejects_nearby if r["outcome"] == o)
                for o in sorted({r["outcome"] for r in rejects_nearby})
            },
            "records": rejects_nearby[:200],
        },
    }
    return Response(content=orjson.dumps(payload), media_type="application/json")


# ── Solver Report (full funnel/error/ghost/consensus picture) ─────────────────

# Both tunable from staging observation, not derived from anything physical.
_GHOST_GATE_KM = 5.0
_ERR_GT_GATE_KM = 15.0
# How stale a ground-truth trail point (or ADS-B fix) may be and still count
# as "the aircraft was there" for ghost detection.
_GHOST_GT_MAX_AGE_S = 90.0
_ADSB_FRESH_S = 60.0


def _solver_window_stats(minutes: float) -> dict:
    """Full solver picture: publication funnel, position error, ghost/false-track
    precision, consensus counters — the data behind the Solver Report panel.

    Funnel, reject-reason, and position-error stats are windowed over the
    last ``minutes`` of ``state.mlat_solve_history`` (idiom shared with
    mlat-history). Ghost detection and consensus/counters read *current* live
    state (multinode_tracks / ground_truth_trails / adsb_aircraft) rather
    than the window — a live track is either a ghost right now or it isn't.

    Ghost definition: a currently-live state.multinode_tracks entry that is
    (1) not ADS-B-associated (no adsb_hex on the result and its key doesn't
    start with mn-adsb-, solver.py:615), (2) more than _GHOST_GATE_KM from
    the time-nearest (<= _GHOST_GT_MAX_AGE_S old) point of every ground-truth
    trail, AND (3) more than _GHOST_GATE_KM from every adsb_aircraft entry
    fresh within _ADSB_FRESH_S. Both distance gates are required because
    staging injects real adsb.lol traffic alongside the simulated fleet — "no
    GT match" alone would mislabel every real-traffic track as a ghost.
    """
    cutoff_ms = int((time.time() - minutes * 60.0) * 1000)
    records = [r for r in list(state.mlat_solve_history) if r["ts_ms"] >= cutoff_ms]

    attempts = len(records)
    n2 = n3plus = 0
    reject_total = 0
    by_reason: dict[str, int] = {}
    pos_errors: list[float] = []
    for r in records:
        outcome = r.get("outcome")
        if outcome == "published":
            n_nodes = r.get("n_nodes") or 0
            if n_nodes == 2:
                n2 += 1
            elif n_nodes >= 3:
                n3plus += 1
            err = r.get("gt_error_km")
            if err is not None and err <= _ERR_GT_GATE_KM:
                pos_errors.append(err)
        else:
            reject_total += 1
            reason = outcome[len("rejected_"):] if outcome.startswith("rejected_") else outcome
            by_reason[reason] = by_reason.get(reason, 0) + 1

    pos_errors.sort()
    n_err = len(pos_errors)
    median_err = pos_errors[n_err // 2] if n_err else None
    p90_err = pos_errors[int(0.9 * (n_err - 1))] if n_err else None

    # ── ghosts ──────────────────────────────────────────────────────────────
    now = time.time()
    now_ms = now * 1000.0
    gt_trails = [(hx, list(trail)) for hx, trail in state.ground_truth_trails.items()]
    fresh_adsb = [
        a for a in state.adsb_aircraft.values()
        if a.get("last_seen_ms") is not None and now_ms - a["last_seen_ms"] <= _ADSB_FRESH_S * 1000.0
    ]

    live_tracks = 0
    adsb_associated = 0
    gt_matched = 0
    ghost_tracks = 0
    for key, rec in state.multinode_tracks.items():
        live_tracks += 1
        if rec.get("adsb_hex") or key.startswith("mn-adsb-"):
            adsb_associated += 1
            continue
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None:
            continue

        matched = False
        for _hx, trail in gt_trails:
            if not trail:
                continue
            pt = min(trail, key=lambda p: abs(p[3] - now))
            if abs(pt[3] - now) > _GHOST_GT_MAX_AGE_S:
                continue
            if haversine_km(lat, lon, pt[0], pt[1]) <= _GHOST_GATE_KM:
                matched = True
                break
        if matched:
            gt_matched += 1
            continue

        near_adsb = any(
            haversine_km(lat, lon, a["lat"], a["lon"]) <= _GHOST_GATE_KM
            for a in fresh_adsb
        )
        if not near_adsb:
            ghost_tracks += 1

    precision_pct = (
        round((live_tracks - ghost_tracks) / live_tracks * 100, 1) if live_tracks else 0.0
    )

    return {
        "window_minutes": minutes,
        "attempts": attempts,
        "published": {"total": n2 + n3plus, "n2": n2, "n3plus": n3plus},
        "rejects": {"total": reject_total, "by_reason": by_reason},
        "position_error_km": {"median": median_err, "p90": p90_err, "n": n_err},
        "ghosts": {
            "live_tracks": live_tracks,
            "adsb_associated": adsb_associated,
            "gt_matched": gt_matched,
            "ghost_tracks": ghost_tracks,
            "precision_pct": precision_pct,
        },
        "consensus": {
            "mode": solver_mod._CONSENSUS_MODE,
            "selected": state.solver_consensus_selected,
            "filtered": state.solver_consensus_filtered,
            "fallback": state.solver_consensus_fallback,
            "shadow": state.solver_consensus_shadow,
        },
        "counters": {
            "successes": state.solver_successes,
            "failures": state.solver_failures,
            "n2_unconfirmed": state.n2_unconfirmed,
            "solver_trimmed": state.solver_trimmed,
            "stale_drops": state.solver_stale_drops,
            "queue_drops": state.solver_queue_drops,
        },
    }


@router.get("/api/test/solver-stats")
async def solver_stats(minutes: float = 10.0):
    """Full solver picture for the Solver Report panel: publication funnel
    (n=2 vs n>=3), reject-reason breakdown, position-error percentiles,
    ghost/false-track precision, and consensus/since-boot counters.

    See ``_solver_window_stats`` for the ghost definition and gate constants.
    """
    minutes = max(1.0, min(minutes, 35.0))
    payload = _solver_window_stats(minutes)
    return Response(content=orjson.dumps(payload), media_type="application/json")


@router.get("/api/test/mlat-accuracy")
async def mlat_accuracy():
    """Rolling MLAT solver accuracy stats aggregated from the last 5 000 matched tracks.

    Mirrors GET /api/radar/accuracy (single-node) but broken down by node count
    instead of position_source.  Updates every 30 s alongside the main verification
    refresh and is useful for detecting long-term accuracy degradation.
    """
    return Response(
        content=state.latest_mlat_accuracy_bytes,
        media_type="application/json",
    )


@router.get("/api/test/node/{node_id}/detection-range")
async def node_detection_range(node_id: str):
    """Return one node's empirical detection range and furthest detections."""
    area = state.node_analytics.detection_areas.get(node_id)
    if not area:
        return Response(
            content=orjson.dumps({"error": f"node {node_id} not registered"}),
            media_type="application/json",
            status_code=404,
        )

    summary = area.summary()

    # Empirical coverage polygon
    ecov = state.node_analytics.empirical_coverages.get(node_id)
    polygon = None
    if ecov:
        polygon = ecov.to_polygon(
            beam_azimuth_deg=area.beam_azimuth_deg,
            beam_width_deg=area.beam_width_deg,
        )

    return Response(
        content=orjson.dumps(
            {
                **summary,
                "empirical_coverage_polygon": polygon,
            }
        ),
        media_type="application/json",
    )


@router.get("/api/test/radar3/detection-range")
async def radar3_detection_range():
    """Back-compat alias for the radar3 node's detection range."""
    return await node_detection_range(_RADAR3_NODE_ID)
