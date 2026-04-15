"""Test network dashboard, ground-truth validation endpoints."""

import math
import os
import time
from collections import deque
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Body, HTTPException, Depends, Header
from fastapi.responses import Response

from core import state
from core.auth import require_admin
from services.frame_processor import normalize_hex_key, resolve_ground_truth_hex, position_distance_km

router = APIRouter()

_RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")


def _verify_sim_key(x_api_key: str = Header(default="", alias="X-API-Key")):
    """Require X-API-Key for simulation data injection endpoints."""
    if _RADAR_API_KEY and x_api_key != _RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

# ── Task staleness detection ──────────────────────────────────────────────────
# Each task has an expected interval — if it hasn't reported success within
# 2× that interval, it's considered stale.
_TASK_EXPECTED_INTERVAL_S = {
    "frame_processor": 10,
    "analytics_refresh": 60,
    "aircraft_flush": 5,
    "archive_flush": 120,
    "reputation_evaluator": 120,
    "adsb_truth_fetcher": 300,
    "solver": 120,
}


def _get_stale_tasks() -> list[str]:
    """Return names of tasks that haven't reported success within their expected interval."""
    now = time.time()
    stale = []
    for task_name, expected_s in _TASK_EXPECTED_INTERVAL_S.items():
        last = state.task_last_success.get(task_name)
        if last is None:
            continue  # task hasn't started yet — not stale
        if (now - last) > expected_s * 2:
            stale.append(task_name)
    return stale


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
    now = time.time()

    total_nodes = len(state.connected_nodes)
    active_nodes = sum(1 for n in state.connected_nodes.values() if n.get("status") not in ("disconnected",))
    synthetic_nodes = sum(1 for n in state.connected_nodes.values() if n.get("is_synthetic"))

    total_tracks = sum(len(p.tracker.tracks) for p in state.node_pipelines.values()) if state.node_pipelines else 0
    total_tracks += len(_default_pipeline.tracker.tracks) if _default_pipeline and hasattr(_default_pipeline, 'tracker') else 0
    geolocated = sum(len(p.geolocated_tracks) for p in state.node_pipelines.values()) if state.node_pipelines else 0
    geolocated += len(_default_pipeline.geolocated_tracks) if _default_pipeline and hasattr(_default_pipeline, 'geolocated_tracks') else 0
    mn_tracks = len(state.multinode_tracks)
    adsb_tracks = len(state.adsb_aircraft)
    n_aircraft = len(state.latest_aircraft_json.get("aircraft", []))

    analytics_nodes = len(state.node_analytics.trust_scores)
    avg_trust = 0.0
    if state.node_analytics.trust_scores:
        scores = [ts.score for ts in state.node_analytics.trust_scores.values() if hasattr(ts, 'score')]
        avg_trust = sum(scores) / len(scores) if scores else 0

    blocked_nodes = sum(
        1 for r in state.node_analytics.reputations.values()
        if hasattr(r, 'reputation') and r.reputation < 0.1
    )
    n_overlaps = len(state.node_associator.overlap_zones) if hasattr(state.node_associator, 'overlap_zones') else 0
    ws_clients = len(state.ws_clients)
    ext_adsb = len(state.external_adsb_cache)

    return orjson.dumps({
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
            "chain_entries_total": sum(len(e) for e in state.chain_entries.values()),
            "iq_commitments_total": sum(len(c) for c in state.iq_commitments.values()),
            "nodes_with_chains": len(state.chain_entries),
        },
        "subsystem_health": {
            "tcp_server": "ok",
            "radar_pipeline": "ok" if _default_pipeline and hasattr(_default_pipeline, 'tracker') else "error",
            "node_analytics": "ok" if analytics_nodes > 0 or total_nodes == 0 else "waiting",
            "inter_node_association": "ok" if n_overlaps > 0 or active_nodes < 2 else "waiting",
            "data_archival": "ok",
            "websocket_broadcast": "ok",
            "aircraft_feed": "ok",
            "chain_of_custody": "ok" if len(state.node_identities) > 0 or total_nodes == 0 else "waiting",
        },
        "task_health": {
            "last_success": dict(state.task_last_success),
            "error_counts": dict(state.task_error_counts),
            "stale_tasks": _get_stale_tasks(),
        },
    })


@router.post("/api/test/validate")
async def validate_ground_truth(body: dict = Body(...)):
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
            dlat = (gt_lat - sa_lat) * 111.0
            dlon = (gt_lon - sa_lon) * 111.0 * math.cos(math.radians(gt_lat))
            dist_km = math.sqrt(dlat ** 2 + dlon ** 2)
            if dist_km < best_dist and dist_km < 50:
                best_dist = dist_km
                best_match = (i, sa)

        if best_match:
            idx, sa = best_match
            matched_server_indices.add(idx)
            sa_alt_m = sa.get("alt_baro", 0) * 0.3048 if sa.get("alt_baro") else 0
            alt_err_m = abs(gt_alt - sa_alt_m)
            matches.append({
                "truth_id": gt.get("id"),
                "server_hex": sa.get("hex"),
                "position_error_km": round(best_dist, 2),
                "altitude_error_m": round(alt_err_m, 0),
                "position_source": sa.get("position_source", "unknown"),
                "has_adsb": gt.get("has_adsb", False),
                "is_anomalous": gt.get("is_anomalous", False),
            })
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


@router.post("/api/test/ground-truth/push")
async def push_ground_truth_snapshot(body: dict = Body(...), _key=Depends(_verify_sim_key)):
    ts = body.get("ts_ms", int(time.time() * 1000)) / 1000.0
    aircraft_list = body.get("aircraft", [])
    if not isinstance(aircraft_list, list):
        raise HTTPException(status_code=400, detail="aircraft list required")

    for ac in aircraft_list:
        hex_code = normalize_hex_key(ac.get("hex") or ac.get("adsb_hex") or "")
        if not hex_code:
            continue
        lat = ac.get("lat", 0.0)
        lon = ac.get("lon", 0.0)
        alt_m = ac.get("alt_m") or ac.get("alt_km", 0) * 1000
        if not lat or not lon:
            continue
        if hex_code not in state.ground_truth_trails:
            state.ground_truth_trails[hex_code] = deque(maxlen=state.GROUND_TRUTH_MAX)
        trail = state.ground_truth_trails[hex_code]
        if trail:
            dlat = abs(trail[-1][0] - lat)
            dlon = abs(trail[-1][1] - lon)
            if dlat < 0.00005 and dlon < 0.00005:
                continue
        trail.append([round(lat, 6), round(lon, 6), round(alt_m, 0), round(ts, 1)])
        # Store/update metadata for this ground truth object
        state.ground_truth_meta[hex_code] = {
            "object_type": ac.get("object_type", "aircraft"),
            "is_anomalous": ac.get("is_anomalous", False),
            "speed_ms": ac.get("speed_ms", 0),
            "heading": ac.get("heading", 0),
        }
        # Flag anomalous objects and log events
        if ac.get("is_anomalous"):
            with state.anomaly_lock:
                if hex_code not in state.anomaly_hexes:
                    state.anomaly_hexes.add(hex_code)
                    event = {
                        "hex": hex_code,
                        "ts": round(ts, 1),
                        "lat": round(lat, 5),
                        "lon": round(lon, 5),
                        "reason": "anomalous_behavior",
                        "object_type": ac.get("object_type", "unknown"),
                        "flagged_at": datetime.now(timezone.utc).isoformat(),
                    }
                    state.anomaly_log.append(event)
                    if len(state.anomaly_log) > state.ANOMALY_LOG_MAX:
                        state.anomaly_log = state.anomaly_log[-state.ANOMALY_LOG_MAX:]
        else:
            with state.anomaly_lock:
                state.anomaly_hexes.discard(hex_code)

    return {"status": "ok", "received": len(aircraft_list), "tracked_hex": len(state.ground_truth_trails)}


@router.post("/api/sim/adsb/push")
async def sim_push_adsb_positions(body: dict = Body(...), _key=Depends(_verify_sim_key)):
    """Simulator pushes live ADS-B positions every second directly into state.adsb_aircraft.

    This keeps each aircraft's position current at 1 Hz regardless of how many
    nodes happen to observe it in a given frame interval.
    """
    ts_ms = body.get("ts_ms", int(time.time() * 1000))
    aircraft_list = body.get("aircraft", [])
    if not isinstance(aircraft_list, list):
        raise HTTPException(status_code=400, detail="aircraft list required")

    updated = 0
    for ac in aircraft_list:
        hex_code = normalize_hex_key(ac.get("hex") or "")
        if not hex_code:
            continue
        lat = ac.get("lat")
        lon = ac.get("lon")
        if not lat or not lon:
            continue
        state.adsb_aircraft[hex_code] = {
            "hex": hex_code,
            "flight": ac.get("flight", ""),
            "lat": lat,
            "lon": lon,
            "alt_baro": ac.get("alt_baro", 0),
            "gs": ac.get("gs", 0),
            "track": ac.get("track", 0),
            "last_seen_ms": ts_ms,
        }
        updated += 1

    if updated:
        state.aircraft_dirty = True

    return {"status": "ok", "updated": updated}


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
        dlat = (sol_last[0] - gt_last[0]) * 111.0
        dlon = (sol_last[1] - gt_last[1]) * 111.0 * math.cos(math.radians(gt_last[0]))
        position_error_km = round(math.sqrt(dlat ** 2 + dlon ** 2), 3)

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
        content=orjson.dumps({
            "flagged_count": len(state.anomaly_hexes),
            "flagged_hexes": sorted(state.anomaly_hexes),
            "events": state.anomaly_log[-100:],
        }),
        media_type="application/json",
    )


# ── Simulation physics config ─────────────────────────────────────────────────

@router.get("/api/simulation/config")
async def get_simulation_config():
    """Return current simulation physics configuration plus live object-type counts."""
    counts: dict[str, int] = {"anomalous": 0, "drone": 0, "aircraft": 0, "total": 0}
    for meta in state.ground_truth_meta.values():
        counts["total"] += 1
        if meta.get("is_anomalous"):
            counts["anomalous"] += 1
        elif meta.get("object_type") == "drone":
            counts["drone"] += 1
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
    """
    allowed = {"frac_anomalous", "frac_drone", "frac_dark", "max_range_km",
               "min_aircraft", "max_aircraft"}
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
        gt_aircraft.append({
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
        })

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
                dlat = (sol[0] - gt_last[0]) * 111.0
                dlon = (sol[1] - gt_last[1]) * 111.0 * math.cos(math.radians(gt_last[0]))
                pos_errors.append(math.sqrt(dlat ** 2 + dlon ** 2))
            if len(pos_errors) >= 200:
                break

    detected_count = len(matched_gt_hexes)
    avg_err = round(sum(pos_errors) / len(pos_errors), 2) if pos_errors else None

    return Response(
        content=orjson.dumps({
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
        }),
        media_type="application/json",
    )


# ── Radar3 solver verification ────────────────────────────────────────────────

_RADAR3_NODE_ID = "radar3-retnode"


@router.get("/api/test/radar3/verification")
async def radar3_verification():
    """Return pre-computed radar3 solver-vs-ADS-B verification stats."""
    return Response(
        content=state.latest_radar3_verification_bytes,
        media_type="application/json",
    )


@router.get("/api/test/radar3/detection-range")
async def radar3_detection_range():
    """Return radar3 empirical detection range and furthest detections."""
    area = state.node_analytics.detection_areas.get(_RADAR3_NODE_ID)
    if not area:
        return Response(
            content=orjson.dumps({"error": "radar3 node not registered"}),
            media_type="application/json",
            status_code=404,
        )

    summary = area.summary()

    # Empirical coverage polygon
    ecov = state.node_analytics.empirical_coverages.get(_RADAR3_NODE_ID)
    polygon = None
    if ecov:
        polygon = ecov.to_polygon(
            beam_azimuth_deg=area.beam_azimuth_deg,
            beam_width_deg=area.beam_width_deg,
        )

    return Response(
        content=orjson.dumps({
            **summary,
            "empirical_coverage_polygon": polygon,
        }),
        media_type="application/json",
    )
