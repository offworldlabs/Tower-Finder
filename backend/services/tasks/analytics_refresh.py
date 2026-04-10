"""Analytics, nodes, overlaps pre-computation — runs every 30 s."""

import asyncio
import concurrent.futures
import hashlib
import logging
import math
import time

import orjson

from core import state
from services.tasks._helpers import haversine_km, bistatic_delay_us, _DELAY_MATCH_THRESHOLD_US

_analytics_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="analytics-bg",
)

_RADAR3_NODE_ID = "radar3-retnode"


def _refresh_analytics_and_nodes():
    """Heavy work: recompute analytics, nodes, and overlaps → store as bytes."""
    from services.tcp_handler import is_synthetic_node

    # Analytics
    analytics_data = {
        "nodes": state.node_analytics.get_all_summaries(),
        "cross_node": state.node_analytics.get_cross_node_analysis(),
    }
    state.latest_analytics_bytes = orjson.dumps(analytics_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Real-only variant: strip synthetic nodes so map.retina.fm never receives them
    with state.connected_nodes_lock:
        real_node_ids = {
            nid for nid, info in state.connected_nodes.items()
            if not info.get("is_synthetic", True)
        }
    analytics_real_data = {
        "nodes": {k: v for k, v in analytics_data["nodes"].items() if k in real_node_ids},
        "cross_node": analytics_data["cross_node"],
    }
    state.latest_analytics_real_bytes = orjson.dumps(analytics_real_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Nodes — snapshot once to avoid RuntimeError from concurrent TCP handler mutations
    with state.connected_nodes_lock:
        _nodes_snapshot = list(state.connected_nodes.items())
    nodes_data = {
        "nodes": {
            nid: {
                "status": info.get("status"),
                "name": info.get("config", {}).get("name", nid),
                "config_hash": info.get("config_hash"),
                "last_heartbeat": info.get("last_heartbeat"),
                "peer": info.get("peer"),
                "is_synthetic": info.get("is_synthetic", is_synthetic_node(nid)),
                "capabilities": info.get("capabilities", {}),
                "frequency": (
                    info.get("config", {}).get("FC")
                    or info.get("config", {}).get("fc_hz")
                    or info.get("config", {}).get("frequency")
                ),
                "sample_rate": (
                    info.get("config", {}).get("Fs")
                    or info.get("config", {}).get("fs_hz")
                ),
                "location": {
                    "rx_lat": info.get("config", {}).get("rx_lat"),
                    "rx_lon": info.get("config", {}).get("rx_lon"),
                    "rx_alt_ft": info.get("config", {}).get("rx_alt_ft"),
                    "tx_lat": info.get("config", {}).get("tx_lat"),
                    "tx_lon": info.get("config", {}).get("tx_lon"),
                    "tx_alt_ft": info.get("config", {}).get("tx_alt_ft"),
                },
            }
            for nid, info in _nodes_snapshot
        },
        "connected": sum(1 for _, n in _nodes_snapshot if n.get("status") not in ("disconnected",)),
        "total": len(_nodes_snapshot),
        "synthetic": sum(1 for _, n in _nodes_snapshot if n.get("is_synthetic")),
    }
    state.latest_nodes_bytes = orjson.dumps(nodes_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Overlaps — only include zones with actual overlap to keep payload small
    overlaps_data = {
        "overlaps": [z for z in state.node_associator.get_overlap_summary() if z["has_overlap"]],
        "registered_nodes": list(state.node_associator.node_geometries.keys()),
    }
    state.latest_overlaps_bytes = orjson.dumps(overlaps_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Solver-vs-ADS-B accuracy statistics
    _refresh_accuracy_stats()

    # Radar3 solver verification
    try:
        _refresh_radar3_verification()
    except Exception:
        logging.exception("_refresh_radar3_verification failed")

    # Synthetic chain-of-custody entries for connected nodes that lack them
    _ensure_custody_data()
    # Evict PassiveRadarPipeline instances for long-disconnected nodes to free RAM
    _evict_stale_pipelines(_nodes_snapshot)


def _refresh_accuracy_stats():
    """Compute solver-vs-ADS-B accuracy from the rolling sample buffer."""
    samples = list(state.accuracy_samples)
    if not samples:
        state.latest_accuracy_bytes = orjson.dumps({"n_samples": 0})
        return

    errors = [s["error_km"] for s in samples]
    errors.sort()
    n = len(errors)

    def _percentile(sorted_vals, pct):
        idx = int(pct / 100 * (len(sorted_vals) - 1))
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    by_source: dict[str, list[float]] = {}
    for s in samples:
        by_source.setdefault(s["position_source"], []).append(s["error_km"])

    source_stats = {}
    for src, errs in by_source.items():
        errs.sort()
        sn = len(errs)
        source_stats[src] = {
            "n_samples": sn,
            "mean_km": round(sum(errs) / sn, 4),
            "median_km": round(_percentile(errs, 50), 4),
            "p95_km": round(_percentile(errs, 95), 4),
            "max_km": round(errs[-1], 4),
        }

    result = {
        "n_samples": n,
        "mean_km": round(sum(errors) / n, 4),
        "median_km": round(_percentile(errors, 50), 4),
        "p95_km": round(_percentile(errors, 95), 4),
        "max_km": round(errors[-1], 4),
        "by_source": source_stats,
    }
    state.latest_accuracy_bytes = orjson.dumps(result)


def _refresh_radar3_verification():
    """Compare radar3 detections to ADS-B truth via bistatic delay matching."""
    radar3_tracks = []
    now = time.time()
    with state.geo_aircraft_lock:
        _geo_snapshot = list(state.active_geo_aircraft.items())
    for ac_hex, (track, cfg) in _geo_snapshot:
        if not isinstance(cfg, dict) or cfg.get("node_id") != _RADAR3_NODE_ID:
            continue
        wall_ts = getattr(track, "wall_clock_ts", 0)
        if (now - wall_ts) > 120:
            continue
        radar3_tracks.append((ac_hex, track, cfg))

    if not radar3_tracks:
        state.latest_radar3_verification_bytes = orjson.dumps({
            "node_id": _RADAR3_NODE_ID,
            "n_tracks": 0,
            "n_matched": 0,
            "tracks": [],
        }, option=orjson.OPT_SERIALIZE_NUMPY)
        return

    def _percentile(sorted_vals, pct):
        if not sorted_vals:
            return 0.0
        idx = int(pct / 100 * (len(sorted_vals) - 1))
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    adsb_candidates: list[tuple[str, dict]] = []
    seen_adsb_hexes: set[str] = set()

    for adsb_hex, entry in list(state.adsb_aircraft.items()):
        if not entry.get("lat") or not entry.get("lon"):
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            continue
        adsb_candidates.append((adsb_hex, entry))
        seen_adsb_hexes.add(adsb_hex)

    for adsb_hex, entry in list(state.external_adsb_cache.items()):
        if not entry.get("lat") or not entry.get("lon"):
            continue
        if adsb_hex not in seen_adsb_hexes:
            adsb_candidates.append((adsb_hex, entry))
            seen_adsb_hexes.add(adsb_hex)

    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if gt_hex in seen_adsb_hexes or not trail:
            continue
        try:
            last = trail[-1]
            if len(last) < 4 or (now - last[3]) > 60:
                continue
            adsb_candidates.append((gt_hex, {
                "lat": last[0], "lon": last[1],
                "alt_baro": last[2], "gs": 0,
            }))
            seen_adsb_hexes.add(gt_hex)
        except Exception:
            continue

    matches = []
    pos_errors = []
    vel_errors = []
    alt_errors = []
    matched_adsb_hexes: set = set()
    matched_detections = []

    for ac_hex, track, cfg in radar3_tracks:
        measured_delay_us = getattr(track, "latest_delay_us", None)
        if not measured_delay_us or measured_delay_us <= 0:
            continue

        tx_lat = cfg.get("tx_lat") or 0.0
        tx_lon = cfg.get("tx_lon") or 0.0
        rx_lat = cfg.get("rx_lat") or 0.0
        rx_lon = cfg.get("rx_lon") or 0.0
        if not tx_lat or not rx_lat:
            continue

        solver_lat = getattr(track, "lat", 0.0) or 0.0
        solver_lon = getattr(track, "lon", 0.0) or 0.0
        solver_vel_e = getattr(track, "vel_east", 0.0) or 0.0
        solver_vel_n = getattr(track, "vel_north", 0.0) or 0.0
        solver_speed = math.sqrt(solver_vel_e ** 2 + solver_vel_n ** 2)
        solver_alt_m = getattr(track, "alt_m", 0.0) or 0.0

        best_adsb_hex = None
        best_adsb = None
        best_delay_err = _DELAY_MATCH_THRESHOLD_US

        for adsb_hex_c, adsb in adsb_candidates:
            if adsb_hex_c in matched_adsb_hexes:
                continue
            expected_delay = bistatic_delay_us(
                tx_lat, tx_lon, rx_lat, rx_lon,
                adsb["lat"], adsb["lon"],
            )
            delay_err = abs(measured_delay_us - expected_delay)
            if delay_err < best_delay_err:
                best_delay_err = delay_err
                best_adsb_hex = adsb_hex_c
                best_adsb = adsb

        if best_adsb is None:
            continue

        matched_adsb_hexes.add(best_adsb_hex)
        truth_lat = best_adsb["lat"]
        truth_lon = best_adsb["lon"]
        truth_alt_m = (best_adsb.get("alt_baro", 0) or 0) * 0.3048 if best_adsb.get("alt_baro") else (best_adsb.get("alt_m", 0) or 0)
        truth_gs_ms = (best_adsb.get("gs", 0) or 0) * 0.514444 if best_adsb.get("gs") else (best_adsb.get("velocity") or 0)

        dlat = (solver_lat - truth_lat) * 111.0
        dlon = (solver_lon - truth_lon) * 111.0 * math.cos(math.radians((solver_lat + truth_lat) / 2.0 or 1.0))
        err_km = math.sqrt(dlat ** 2 + dlon ** 2)

        vel_err = abs(solver_speed - truth_gs_ms)
        alt_err = abs(solver_alt_m - truth_alt_m)

        pos_errors.append(err_km)
        vel_errors.append(vel_err)
        alt_errors.append(alt_err)

        matches.append({
            "hex": ac_hex,
            "matched_adsb_hex": best_adsb_hex,
            "delay_match_us": round(best_delay_err, 2),
            "measured_delay_us": round(measured_delay_us, 2),
            "solver_lat": round(solver_lat, 6),
            "solver_lon": round(solver_lon, 6),
            "truth_lat": round(truth_lat, 6),
            "truth_lon": round(truth_lon, 6),
            "position_error_km": round(err_km, 3),
            "solver_speed_ms": round(solver_speed, 1),
            "truth_speed_ms": round(truth_gs_ms, 1),
            "velocity_error_ms": round(vel_err, 1),
            "solver_alt_m": round(solver_alt_m, 0),
            "truth_alt_m": round(truth_alt_m, 0),
            "altitude_error_m": round(alt_err, 0),
        })
        matched_detections.append((truth_lat, truth_lon, best_adsb_hex))

    area = state.node_analytics.detection_areas.get(_RADAR3_NODE_ID)
    if area:
        for det_lat, det_lon, det_hex in matched_detections:
            area.record_verified_detection(det_lat, det_lon, det_hex)

    pos_errors.sort()
    vel_errors.sort()
    alt_errors.sort()
    n = len(matches)

    result = {
        "node_id": _RADAR3_NODE_ID,
        "n_tracks": len(radar3_tracks),
        "n_matched": n,
        "position": {
            "mean_km": round(sum(pos_errors) / n, 3) if n else 0,
            "median_km": round(_percentile(pos_errors, 50), 3),
            "p95_km": round(_percentile(pos_errors, 95), 3),
            "max_km": round(pos_errors[-1], 3) if pos_errors else 0,
        },
        "velocity": {
            "mean_ms": round(sum(vel_errors) / n, 1) if n else 0,
            "median_ms": round(_percentile(vel_errors, 50), 1),
            "p95_ms": round(_percentile(vel_errors, 95), 1),
        },
        "altitude": {
            "mean_m": round(sum(alt_errors) / n, 0) if n else 0,
            "median_m": round(_percentile(alt_errors, 50), 0),
            "p95_m": round(_percentile(alt_errors, 95), 0),
        },
        "tracks": matches[:50],
    }
    state.latest_radar3_verification_bytes = orjson.dumps(result, option=orjson.OPT_SERIALIZE_NUMPY)


def _ensure_custody_data():
    """Auto-register connected nodes in chain-of-custody if they lack entries."""
    from datetime import datetime, timezone
    from chain_of_custody.models import NodeIdentity

    now_iso = datetime.now(timezone.utc).isoformat()
    hour_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    with state.connected_nodes_lock:
        _custody_snapshot = list(state.connected_nodes.items())
    for nid, info in _custody_snapshot:
        if info.get("status") == "disconnected":
            continue
        if nid not in state.node_identities:
            fingerprint = hashlib.sha256(nid.encode()).hexdigest()[:16]
            identity = NodeIdentity(
                node_id=nid,
                public_key_pem=f"-----SIM-KEY-{nid[-8:]}-----",
                public_key_fingerprint=fingerprint,
                serial_number=f"SIM-{nid[-6:]}",
                signing_mode="software",
                registered_at=now_iso,
            )
            state.node_identities[nid] = identity

        if nid not in state.chain_entries:
            state.chain_entries[nid] = []

        entries = state.chain_entries[nid]
        if len(entries) > 168:
            state.chain_entries[nid] = entries = entries[-168:]
        if not entries or entries[-1].get("hour_utc") != hour_utc:
            prev_hash = entries[-1].get("entry_hash", "0" * 64) if entries else "0" * 64
            content_hash = hashlib.sha256(f"{nid}:{hour_utc}".encode()).hexdigest()
            entry_hash = hashlib.sha256(f"{prev_hash}:{content_hash}".encode()).hexdigest()
            entries.append({
                "node_id": nid,
                "hour_utc": hour_utc,
                "prev_hash": prev_hash,
                "content_hash": content_hash,
                "entry_hash": entry_hash,
                "_verified": True,
                "_received_at": now_iso,
            })

        if nid not in state.iq_commitments:
            state.iq_commitments[nid] = []
        if not state.iq_commitments[nid]:
            state.iq_commitments[nid].append({
                "node_id": nid,
                "capture_id": f"iq-{nid[-8:]}-001",
                "sha256": hashlib.sha256(f"iq:{nid}".encode()).hexdigest(),
                "_received_at": now_iso,
            })


def _evict_stale_pipelines(nodes_snapshot: list):
    """Remove PassiveRadarPipeline for nodes disconnected > 2 h."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    stale = []
    for nid, info in nodes_snapshot:
        if info.get("status") != "disconnected":
            continue
        hb = info.get("last_heartbeat")
        if not hb:
            stale.append(nid)
            continue
        try:
            hb_time = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            if (now - hb_time).total_seconds() > 7200:
                stale.append(nid)
        except Exception:
            pass
    for nid in stale:
        state.node_pipelines.pop(nid, None)
    if stale:
        logging.debug("Evicted %d stale node pipelines", len(stale))


async def analytics_refresh_task():
    """Pre-compute analytics/nodes/overlaps every 30 s in a dedicated thread."""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(5)
    while True:
        try:
            await loop.run_in_executor(_analytics_executor, _refresh_analytics_and_nodes)
            await loop.run_in_executor(_analytics_executor, state.node_analytics.maybe_auto_save)
            from routes.admin import check_node_health
            check_node_health()
            logging.debug("Analytics refresh completed")
            state.task_last_success["analytics_refresh"] = time.time()
        except Exception:
            state.task_error_counts["analytics_refresh"] += 1
            logging.exception("Analytics refresh failed")
        await asyncio.sleep(30)
