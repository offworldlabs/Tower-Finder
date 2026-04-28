"""Analytics, nodes, overlaps pre-computation — runs every 30 s."""

import asyncio
import concurrent.futures
import hashlib
import logging
import math
import time

import numpy as np
import orjson

from config.constants import YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM
from core import state
from services.id_utils import multinode_hex_from_key
from services.tasks._helpers import (
    _DELAY_MATCH_THRESHOLD_US,
    bistatic_delay_us,
    haversine_km,
)

_analytics_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="analytics-bg",
)

_RADAR3_NODE_ID = "radar3-retnode"


def _percentile(vals: list, pct: float) -> float:
    """Return the pct-th percentile of a list using numpy. Returns 0.0 for empty input."""
    if not vals:
        return 0.0
    return float(np.percentile(vals, pct))


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
        real_node_ids = {nid for nid, info in state.connected_nodes.items() if not info.get("is_synthetic", True)}
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
                "sample_rate": (info.get("config", {}).get("Fs") or info.get("config", {}).get("fs_hz")),
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

    # Per-node missed detection analysis
    try:
        _refresh_missed_detections(_nodes_snapshot)
    except Exception:
        logging.exception("_refresh_missed_detections failed")

    # Radar3 solver verification
    try:
        _refresh_radar3_verification()
    except Exception:
        logging.exception("_refresh_radar3_verification failed")

    # MLAT (multinode) solver verification
    try:
        _refresh_mlat_verification()
    except Exception:
        logging.exception("_refresh_mlat_verification failed")

    # Synthetic chain-of-custody entries for connected nodes that lack them
    _ensure_custody_data()
    # Evict PassiveRadarPipeline instances for long-disconnected nodes to free RAM
    _evict_stale_pipelines(_nodes_snapshot)


# ── Missed detection analysis ─────────────────────────────────────────────────


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing from (lat1, lon1) to (lat2, lon2) in degrees [0, 360)."""
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    dlonr = math.radians(lon2 - lon1)
    y = math.sin(dlonr) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlonr)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _aircraft_in_beam(
    ac_lat: float,
    ac_lon: float,
    rx_lat: float,
    rx_lon: float,
    beam_azimuth_deg: float,
    beam_width_deg: float,
    max_range_km: float,
) -> bool:
    """Return True if an aircraft at (ac_lat, ac_lon) is inside the node beam."""
    dist_km = haversine_km(rx_lat, rx_lon, ac_lat, ac_lon)
    if dist_km > max_range_km:
        return False
    bearing = _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon)
    # Angular difference, wrapped to [-180, 180]
    diff = (bearing - beam_azimuth_deg + 180) % 360 - 180
    return abs(diff) <= beam_width_deg / 2.0


def _refresh_missed_detections(nodes_snapshot: list):
    """Compare ADS-B ground truth against each node's beam geometry.

    For each active node, count how many ADS-B aircraft are within its
    detection zone but were NOT detected by the node's tracker.
    Results stored in ``state.latest_missed_detections``.
    """
    now = time.time()
    # Snapshot current ADS-B positions (both from node frames and external)
    adsb_snapshot: list[tuple[str, float, float]] = []
    seen_hexes: set[str] = set()  # lowercase, for O(1) dedup against external cache
    for hex_code, entry in list(state.adsb_aircraft.items()):
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 120:
            continue
        adsb_snapshot.append((hex_code, lat, lon))
        seen_hexes.add(hex_code.lower())

    for hex_code, entry in list(state.external_adsb_cache.items()):
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            continue
        # Avoid duplicates — O(1) set lookup, case-insensitive
        if hex_code.lower() in seen_hexes:
            continue
        adsb_snapshot.append((hex_code, lat, lon))
        seen_hexes.add(hex_code.lower())

    result: dict[str, dict] = {}

    for nid, info in nodes_snapshot:
        if info.get("status") == "disconnected":
            continue
        cfg = info.get("config", {})
        rx_lat = cfg.get("rx_lat")
        rx_lon = cfg.get("rx_lon")
        tx_lat = cfg.get("tx_lat")
        tx_lon = cfg.get("tx_lon")
        if not all((rx_lat, rx_lon, tx_lat, tx_lon)):
            continue

        beam_width = float(cfg.get("beam_width_deg") or YAGI_BEAM_WIDTH_DEG)
        max_range = float(cfg.get("max_range_km") or YAGI_MAX_RANGE_KM)
        beam_azimuth = cfg.get("beam_azimuth_deg")
        if beam_azimuth is None:
            # Default: Yagi sits perpendicular to the RX→TX baseline, so the
            # boresight is rotated +90° from the direct RX→TX bearing.
            # Nodes with a different antenna orientation should set beam_azimuth_deg
            # explicitly in their config to avoid incorrect missed-detection counts.
            beam_azimuth = (_bearing_deg(rx_lat, rx_lon, tx_lat, tx_lon) + 90.0) % 360.0

        # Aircraft within this node's beam
        in_range: list[str] = []
        for hex_code, ac_lat, ac_lon in adsb_snapshot:
            if _aircraft_in_beam(ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range):
                in_range.append(hex_code)

        if not in_range:
            result[nid] = {
                "in_range": 0,
                "detected": 0,
                "missed": 0,
                "miss_rate": 0.0,
                "missed_aircraft": [],
            }
            continue

        # Which of these did the node actually detect?
        # Check the node's pipeline tracker for ADS-B hex associations.
        pipeline = state.node_pipelines.get(nid)
        detected_hexes: set[str] = set()
        if pipeline:
            for track in pipeline.tracker.tracks:
                hex_val = getattr(track, "adsb_hex", None)
                if hex_val:
                    detected_hexes.add(hex_val.lower())

        in_range_set = set(h.lower() for h in in_range)
        detected_in_range = in_range_set & detected_hexes
        missed = in_range_set - detected_hexes

        # Build details for missed aircraft (limit to 20 for payload size)
        # Pre-build a hex→(lat,lon) dict for O(1) lookup instead of O(n²) scan.
        adsb_by_hex = {h.lower(): (lat, lon) for h, lat, lon in adsb_snapshot}
        missed_details = []
        for hex_code in list(missed)[:20]:
            if hex_code in adsb_by_hex:
                lat, lon = adsb_by_hex[hex_code]
                dist = haversine_km(rx_lat, rx_lon, lat, lon)
                missed_details.append(
                    {
                        "hex": hex_code,
                        "lat": round(lat, 5),
                        "lon": round(lon, 5),
                        "dist_km": round(dist, 1),
                    }
                )

        n_in_range = len(in_range_set)
        n_detected = len(detected_in_range)
        n_missed = len(missed)

        result[nid] = {
            "in_range": n_in_range,
            "detected": n_detected,
            "missed": n_missed,
            "miss_rate": round(n_missed / n_in_range, 3) if n_in_range > 0 else 0.0,
            "missed_aircraft": missed_details,
        }

    state.latest_missed_detections = result


def _refresh_accuracy_stats():
    """Compute solver-vs-ADS-B accuracy from the rolling sample buffer."""
    samples = list(state.accuracy_samples)
    if not samples:
        state.latest_accuracy_bytes = orjson.dumps({"n_samples": 0})
        return

    errors = [s["error_km"] for s in samples]
    errors.sort()
    n = len(errors)

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
        state.latest_radar3_verification_bytes = orjson.dumps(
            {
                "node_id": _RADAR3_NODE_ID,
                "n_tracks": 0,
                "n_matched": 0,
                "tracks": [],
            },
            option=orjson.OPT_SERIALIZE_NUMPY,
        )
        return

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
            adsb_candidates.append(
                (
                    gt_hex,
                    {
                        "lat": last[0],
                        "lon": last[1],
                        "alt_baro": last[2],
                        "gs": 0,
                    },
                )
            )
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
        solver_speed = math.sqrt(solver_vel_e**2 + solver_vel_n**2)
        solver_alt_m = getattr(track, "alt_m", 0.0) or 0.0

        best_adsb_hex = None
        best_adsb = None
        best_delay_err = _DELAY_MATCH_THRESHOLD_US

        for adsb_hex_c, adsb in adsb_candidates:
            if adsb_hex_c in matched_adsb_hexes:
                continue
            expected_delay = bistatic_delay_us(
                tx_lat,
                tx_lon,
                rx_lat,
                rx_lon,
                adsb["lat"],
                adsb["lon"],
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
        truth_alt_m = (
            (best_adsb.get("alt_baro", 0) or 0) * 0.3048
            if best_adsb.get("alt_baro")
            else (best_adsb.get("alt_m", 0) or 0)
        )
        truth_gs_ms = (
            (best_adsb.get("gs", 0) or 0) * 0.514444 if best_adsb.get("gs") else (best_adsb.get("velocity") or 0)
        )

        dlat = (solver_lat - truth_lat) * 111.0
        dlon = (solver_lon - truth_lon) * 111.0 * math.cos(math.radians((solver_lat + truth_lat) / 2.0 or 1.0))
        err_km = math.sqrt(dlat**2 + dlon**2)

        vel_err = abs(solver_speed - truth_gs_ms)
        alt_err = abs(solver_alt_m - truth_alt_m)

        pos_errors.append(err_km)
        vel_errors.append(vel_err)
        alt_errors.append(alt_err)

        matches.append(
            {
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
            }
        )
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


# ── MLAT (multinode) solver verification ─────────────────────────────────────

# Maximum age of a multinode solve result to include in verification (seconds).
_MLAT_SOLVE_MAX_AGE_S = 120
# Maximum distance between a solve result and a ground-truth point to count
# as a match.  12 km handles 2-node solves with marginal geometry.
_MLAT_MATCH_THRESHOLD_KM = 12.0
# Maximum altitude difference (metres) between solver and truth for a valid
# match.  With ADS-B altitude injection the solver altitude is exact (< 50 m
# error); a candidate truth aircraft whose altitude differs by more than this
# gate is a different aircraft that happens to be within the position window.
# 3000 m = 10 000 ft accommodates the full 2 km altitude-layer gap plus noise.
_MLAT_ALT_GATE_M = 3000.0
# Solve results within this radius are considered solver cycles for the same
# aircraft.  Only one representative per cluster enters the matching loop so
# that a single aircraft with multiple solver cycles does not inflate n_solves.
_MLAT_CLUSTER_KM = 12.0


def _refresh_mlat_accuracy_stats() -> None:
    """Compute rolling MLAT solver accuracy from the mlat_samples deque.

    Mirrors _refresh_accuracy_stats() for single-node solves, but broken
    down by node count (2-node vs 3-node etc.) instead of position_source.
    Written to state.latest_mlat_accuracy_bytes, served by /api/test/mlat-accuracy.
    """
    samples = list(state.mlat_samples)
    if not samples:
        state.latest_mlat_accuracy_bytes = orjson.dumps({"n_samples": 0})
        return

    errors = [s["error_km"] for s in samples]
    errors.sort()
    n = len(errors)

    by_nodes: dict[int, list[float]] = {}
    for s in samples:
        by_nodes.setdefault(int(s["n_nodes"]), []).append(s["error_km"])

    node_stats = {}
    for nc, errs in sorted(by_nodes.items()):
        sorted_errs = sorted(errs)
        sn = len(sorted_errs)
        node_stats[str(nc)] = {
            "n_samples": sn,
            "mean_km": round(sum(sorted_errs) / sn, 4),
            "median_km": round(_percentile(sorted_errs, 50), 4),
            "p95_km": round(_percentile(sorted_errs, 95), 4),
            "max_km": round(sorted_errs[-1], 4),
        }

    state.latest_mlat_accuracy_bytes = orjson.dumps(
        {
            "n_samples": n,
            "mean_km": round(sum(errors) / n, 4),
            "median_km": round(_percentile(errors, 50), 4),
            "p95_km": round(_percentile(errors, 95), 4),
            "max_km": round(errors[-1], 4),
            "by_node_count": node_stats,
        }
    )


def _refresh_mlat_verification():
    """Compare multinode solve results to ground-truth trails pushed by the fleet orchestrator.

    Matching is proximity-based (no adsb_hex in the solver result): for each
    fresh multinode solve we find the closest ground-truth trail point and
    record the lateral, altitude, and speed errors.

    Results are written to state.latest_mlat_verification_bytes and exposed
    via GET /api/test/mlat-verification.
    """
    now = time.time()

    # --- Build truth sources --------------------------------------------------
    n_truth_gt = 0
    n_truth_live_adsb = 0
    n_truth_external = 0

    # Ground-truth trails: kept as a {hex → (trail_list, meta)} snapshot for
    # time-matched lookup in the matching loop.  Aircraft positions are pushed
    # every 2 s and stored in a deque of up to 120 points (240 s of history).
    # Matching uses the trail point whose timestamp is closest to the solver's
    # timestamp_ms so that aircraft movement between capture and verification
    # time does not inflate position errors.
    gt_trails_snapshot: dict[str, tuple[list, dict]] = {}
    seen_gt_hexes: set[str] = set()
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        last = trail[-1]
        # Only include trails whose most-recent point is fresh (orchestrator
        # still running for this aircraft).
        if len(last) < 4 or (now - last[3]) > 60:
            continue
        gt_trails_snapshot[gt_hex] = (list(trail), state.ground_truth_meta.get(gt_hex, {}))
        seen_gt_hexes.add(gt_hex)
        n_truth_gt += 1

    # Fallback pools (current snapshot only — no trail history):
    # truth_pool: list of (hex, lat, lon, alt_m, speed_ms, object_type, is_anomalous)
    adsb_truth_pool: list[tuple] = []
    seen_truth_hexes: set[str] = set(seen_gt_hexes)

    # Fallback 1: live ADS-B entries not already covered by ground-truth trails.
    # Solve results are kept up to _MLAT_SOLVE_MAX_AGE_S = 120 s because solves
    # are infrequent (one per 40 s frame interval) — a 60 s window would discard
    # most valid results before a second truth point arrives.
    for adsb_hex, entry in list(state.adsb_aircraft.items()):
        if adsb_hex in seen_truth_hexes:
            continue
        if entry.get("lat") is None or entry.get("lon") is None:
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            continue
        gs_ms = (entry.get("gs", 0) or 0) * 0.514444
        alt_m = (entry.get("alt_baro", 0) or 0) * 0.3048
        adsb_truth_pool.append(
            (
                adsb_hex,
                entry["lat"],
                entry["lon"],
                float(alt_m),
                float(gs_ms),
                "aircraft",
                False,
            )
        )
        seen_truth_hexes.add(adsb_hex)
        n_truth_live_adsb += 1

    # Fallback 2: OpenSky / external ADS-B snapshot — same pattern as
    # _refresh_radar3_verification().  Useful when the live ADS-B injector
    # is in its rate-limit backoff window (up to 300 s).
    for adsb_hex, entry in list(state.external_adsb_cache.items()):
        if not entry.get("lat") or not entry.get("lon"):
            continue
        if adsb_hex not in seen_truth_hexes:
            gs_ms = (entry.get("gs", 0) or 0) * 0.514444
            alt_m = (entry.get("alt_baro", 0) or 0) * 0.3048
            adsb_truth_pool.append(
                (
                    adsb_hex,
                    entry["lat"],
                    entry["lon"],
                    float(alt_m),
                    float(gs_ms),
                    "aircraft",
                    False,
                )
            )
            seen_truth_hexes.add(adsb_hex)
            n_truth_external += 1

    # --- Walk multinode solve results -----------------------------------------
    # Count fresh solves BEFORE the truth-pool check so n_solves is always honest
    # even when we have no truth data to match against.
    mn_snapshot = list(state.multinode_tracks.items())
    fresh_solves = []
    for key, r in mn_snapshot:
        ts_ms = r.get("timestamp_ms", 0)
        age_s = now - ts_ms / 1000.0
        if age_s > _MLAT_SOLVE_MAX_AGE_S or age_s < 0:
            continue
        if not r.get("lat") or not r.get("lon"):
            continue
        fresh_solves.append((key, r))

    n_solver_cycles = len(fresh_solves)

    if not gt_trails_snapshot and not adsb_truth_pool:
        state.latest_mlat_verification_bytes = orjson.dumps(
            {
                "n_solves": n_solver_cycles,
                "n_solver_cycles": n_solver_cycles,
                "n_unique_aircraft": n_solver_cycles,
                "n_matched": 0,
                "match_rate_pct": 0.0,
                "match_threshold_km": _MLAT_MATCH_THRESHOLD_KM,
                "n_truth_candidates": 0,
                "truth_sources": {"ground_truth": 0, "live_adsb": 0, "external_adsb": 0},
                "position": {"mean_km": 0, "median_km": 0, "p95_km": 0, "max_km": 0},
                "velocity": {"mean_ms": 0, "median_ms": 0, "p95_ms": 0},
                "altitude": {"mean_m": 0, "median_m": 0, "p95_m": 0},
                "by_node_count": {},
                "tracks": [],
                "unmatched": {"n": n_solver_cycles, "nearest_truth": {"mean_km": None, "median_km": None, "p95_km": None}, "tracks": []},
            },
            option=orjson.OPT_SERIALIZE_NUMPY,
        )
        return

    # Greedy best-match assignment: pre-sort fresh_solves by distance to nearest
    # truth so the globally-closest (solver, truth) pair is always matched first.
    # Without this sort, when two solver cycles from different node pairs both
    # resolve near the same aircraft (e.g. one at 3 km error, one at 10 km), the
    # dict-insertion-order winner claims the truth even if it is the worse result,
    # leaving the better result unmatched and recording the inflated error.
    def _min_truth_dist_km(kv: tuple) -> float:
        _r = kv[1]
        _slat = float(_r.get("lat", 0))
        _slon = float(_r.get("lon", 0))
        _sts = _r.get("timestamp_ms", 0) / 1000.0
        _best = float(_MLAT_MATCH_THRESHOLD_KM)
        for _gt_hex, (_trail, _meta) in gt_trails_snapshot.items():
            _cl = min(_trail, key=lambda p: abs(p[3] - _sts))
            if abs(_cl[3] - _sts) > _MLAT_SOLVE_MAX_AGE_S + 30:
                continue
            _best = min(_best, haversine_km(_slat, _slon, _cl[0], _cl[1]))
        for _te in adsb_truth_pool:
            _best = min(_best, haversine_km(_slat, _slon, _te[1], _te[2]))
        return _best

    fresh_solves.sort(key=_min_truth_dist_km)

    # Greedy assignment after best-first sort: each truth hex is now claimed by
    # the closest solver result, preventing worse duplicates from displacing it.
    matches: list[dict] = []
    unmatched: list[dict] = []
    unmatched_nearest_km: list[float] = []
    pos_errors: list[float] = []
    vel_errors: list[float] = []
    alt_errors: list[float] = []
    matched_truth_hexes: set[str] = set()
    by_node_count: dict[int, list[float]] = {}

    # Each fresh solve is an individual candidate; n_unique_aircraft is computed
    # after matching by counting: matched aircraft + unmatched solves whose
    # nearest truth hex is not already claimed by a matched solve.
    for key, r in fresh_solves:
        solver_lat = float(r["lat"])
        solver_lon = float(r["lon"])
        solver_alt_m = float(r.get("alt_m", 0) or 0)
        solver_vel_e = float(r.get("vel_east", 0) or 0)
        solver_vel_n = float(r.get("vel_north", 0) or 0)
        solver_speed_ms = math.sqrt(solver_vel_e**2 + solver_vel_n**2)
        n_nodes = int(r.get("n_nodes", 0))
        solver_ts = r.get("timestamp_ms", 0) / 1000.0

        best_truth: tuple | None = None
        best_dist_km = _MLAT_MATCH_THRESHOLD_KM

        # 1. Ground-truth trails: find the trail point whose timestamp is
        #    closest to the solver's capture time so aircraft movement between
        #    frame capture and now does not inflate the position error.
        for gt_hex, (trail, meta) in gt_trails_snapshot.items():
            if gt_hex in matched_truth_hexes:
                continue
            closest = min(trail, key=lambda p: abs(p[3] - solver_ts))
            # Skip if the closest point is too far in time (trail too sparse)
            if abs(closest[3] - solver_ts) > _MLAT_SOLVE_MAX_AGE_S + 30:
                continue
            # Altitude gate: skip if truth altitude known and differs too much.
            # Prevents matching solver result to a different nearby aircraft.
            t_alt_m = float(closest[2])
            if (solver_alt_m > 100 and t_alt_m > 100
                    and abs(solver_alt_m - t_alt_m) > _MLAT_ALT_GATE_M):
                continue
            dist_km = haversine_km(solver_lat, solver_lon, closest[0], closest[1])
            if dist_km < best_dist_km:
                best_dist_km = dist_km
                speed_ms = float(meta.get("speed_ms", 0) or 0)
                best_truth = (
                    gt_hex,
                    closest[0],
                    closest[1],
                    t_alt_m,
                    speed_ms,
                    meta.get("object_type", "aircraft"),
                    bool(meta.get("is_anomalous", False)),
                )

        # 2. ADS-B fallback (current position — no trail history).
        if best_truth is None:
            for truth_entry in adsb_truth_pool:
                truth_hex, t_lat, t_lon, t_alt, t_speed, t_type, t_anom = truth_entry
                if truth_hex in matched_truth_hexes:
                    continue
                # Altitude gate (same logic as ground-truth loop above).
                if (solver_alt_m > 100 and t_alt > 100
                        and abs(solver_alt_m - t_alt) > _MLAT_ALT_GATE_M):
                    continue
                dist_km = haversine_km(solver_lat, solver_lon, t_lat, t_lon)
                if dist_km < best_dist_km:
                    best_dist_km = dist_km
                    best_truth = truth_entry

        if best_truth is None:
            # Diagnostic pass: find nearest truth at any distance (no threshold).
            # Also record nearest_hex so we can determine post-loop whether this
            # unmatched solve is a duplicate cycle for an already-matched aircraft
            # or a genuinely new aircraft position.
            nearest_km = float("inf")
            nearest_hex: str | None = None
            for gt_hex2, (trail2, _meta2) in gt_trails_snapshot.items():
                closest2 = min(trail2, key=lambda p: abs(p[3] - solver_ts))
                if abs(closest2[3] - solver_ts) > _MLAT_SOLVE_MAX_AGE_S + 30:
                    continue
                d = haversine_km(solver_lat, solver_lon, closest2[0], closest2[1])
                if d < nearest_km:
                    nearest_km = d
                    nearest_hex = gt_hex2
            for truth_entry2 in adsb_truth_pool:
                t_hex2, t_lat2, t_lon2 = truth_entry2[0], truth_entry2[1], truth_entry2[2]
                d = haversine_km(solver_lat, solver_lon, t_lat2, t_lon2)
                if d < nearest_km:
                    nearest_km = d
                    nearest_hex = t_hex2
            nearest_km_val = round(nearest_km, 1) if nearest_km < float("inf") else None
            if nearest_km_val is not None:
                unmatched_nearest_km.append(nearest_km)
            unmatched.append(
                {
                    "solve_key": key,
                    "solver_lat": round(solver_lat, 6),
                    "solver_lon": round(solver_lon, 6),
                    "n_nodes": n_nodes,
                    "nearest_truth_km": nearest_km_val,
                    "nearest_truth_hex": nearest_hex,
                    "rms_delay": round(float(r.get("rms_delay", 0) or 0), 3),
                    "timestamp_ms": int(r.get("timestamp_ms", 0)),
                }
            )
            continue

        truth_hex, t_lat, t_lon, t_alt, t_speed, t_type, t_anom = best_truth
        matched_truth_hexes.add(truth_hex)

        pos_err = best_dist_km
        vel_err = abs(solver_speed_ms - t_speed)
        alt_err = abs(solver_alt_m - t_alt)

        pos_errors.append(pos_err)
        vel_errors.append(vel_err)
        alt_errors.append(alt_err)
        by_node_count.setdefault(n_nodes, []).append(pos_err)

        matches.append(
            {
                "solve_key": key,
                "solver_hex": multinode_hex_from_key(key),
                "solver_lat": round(solver_lat, 6),
                "solver_lon": round(solver_lon, 6),
                "truth_lat": round(t_lat, 6),
                "truth_lon": round(t_lon, 6),
                "truth_hex": truth_hex,
                "position_error_km": round(pos_err, 3),
                "solver_alt_m": round(solver_alt_m, 0),
                "truth_alt_m": round(t_alt, 0),
                "altitude_error_m": round(alt_err, 0),
                "solver_speed_ms": round(solver_speed_ms, 1),
                "truth_speed_ms": round(t_speed, 1),
                "velocity_error_ms": round(vel_err, 1),
                "n_nodes": n_nodes,
                "rms_delay": round(float(r.get("rms_delay", 0) or 0), 3),
                "rms_doppler": round(float(r.get("rms_doppler", 0) or 0), 2),
                "object_type": t_type,
                "is_anomalous": t_anom,
                "timestamp_ms": int(r.get("timestamp_ms", 0)),
            }
        )

    n_solves = n_solver_cycles
    n_matched = len(matches)

    # n_unique_aircraft: matched aircraft + unmatched solves whose nearest truth
    # is NOT already claimed (i.e. not a duplicate cycle for an already-matched
    # aircraft).  Two unmatched solves pointing at the same unclaimed truth hex
    # count as one aircraft.
    unmatched_unclaimed_hexes: set[str] = set()
    n_unmatched_no_nearby_truth = 0
    for u in unmatched:
        u_hex = u.get("nearest_truth_hex")
        if u_hex is None:
            n_unmatched_no_nearby_truth += 1
        elif u_hex not in matched_truth_hexes:
            unmatched_unclaimed_hexes.add(u_hex)
    n_unique_aircraft = n_matched + len(unmatched_unclaimed_hexes) + n_unmatched_no_nearby_truth
    pos_errors.sort()
    vel_errors.sort()
    alt_errors.sort()

    # Feed rolling sample buffer for trend monitoring (one sample per matched track)
    ts_now_ms = int(now * 1000)
    for m in matches:
        state.mlat_samples.append(
            {
                "hex": m["truth_hex"],
                "error_km": m["position_error_km"],
                "n_nodes": m["n_nodes"],
                "ts": ts_now_ms,
            }
        )

    _refresh_mlat_accuracy_stats()

    by_node_count_out = {
        str(k): {
            "n": len(errs),
            "mean_km": round(sum(errs) / len(errs), 3),
            "median_km": round(_percentile(sorted(errs), 50), 3),
        }
        for k, errs in sorted(by_node_count.items())
    }

    result = {
        "n_solves": n_solves,
        "n_solver_cycles": n_solver_cycles,
        "n_unique_aircraft": n_unique_aircraft,
        "n_matched": n_matched,
        "match_rate_pct": round(100.0 * n_matched / n_unique_aircraft, 1) if n_unique_aircraft else 0.0,
        "match_threshold_km": _MLAT_MATCH_THRESHOLD_KM,
        "n_truth_candidates": n_truth_gt + n_truth_live_adsb + n_truth_external,
        "truth_sources": {
            "ground_truth": n_truth_gt,
            "live_adsb": n_truth_live_adsb,
            "external_adsb": n_truth_external,
        },
        "position": {
            "mean_km": round(sum(pos_errors) / n_matched, 3) if n_matched else 0,
            "median_km": round(_percentile(pos_errors, 50), 3),
            "p95_km": round(_percentile(pos_errors, 95), 3),
            "max_km": round(pos_errors[-1], 3) if pos_errors else 0,
        },
        "velocity": {
            "mean_ms": round(sum(vel_errors) / n_matched, 1) if n_matched else 0,
            "median_ms": round(_percentile(vel_errors, 50), 1),
            "p95_ms": round(_percentile(vel_errors, 95), 1),
        },
        "altitude": {
            "mean_m": round(sum(alt_errors) / n_matched, 0) if n_matched else 0,
            "median_m": round(_percentile(alt_errors, 50), 0),
            "p95_m": round(_percentile(alt_errors, 95), 0),
        },
        "by_node_count": by_node_count_out,
        "tracks": matches[:100],
        "unmatched": {
            "n": len(unmatched),
            "nearest_truth": {
                "mean_km": round(sum(unmatched_nearest_km) / len(unmatched_nearest_km), 1)
                if unmatched_nearest_km
                else None,
                "median_km": round(_percentile(sorted(unmatched_nearest_km), 50), 1)
                if unmatched_nearest_km
                else None,
                "p95_km": round(_percentile(sorted(unmatched_nearest_km), 95), 1)
                if unmatched_nearest_km
                else None,
            },
            "tracks": sorted(unmatched, key=lambda x: x.get("nearest_truth_km") or 999)[:50],
        },
    }
    state.latest_mlat_verification_bytes = orjson.dumps(result, option=orjson.OPT_SERIALIZE_NUMPY)


def _ensure_custody_data():
    """Auto-register connected nodes in chain-of-custody if they lack entries."""
    from datetime import datetime, timezone

    from retina_custody.models import NodeIdentity

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
            entries.append(
                {
                    "node_id": nid,
                    "hour_utc": hour_utc,
                    "prev_hash": prev_hash,
                    "content_hash": content_hash,
                    "entry_hash": entry_hash,
                    "_verified": True,
                    "_received_at": now_iso,
                }
            )

        if nid not in state.iq_commitments:
            state.iq_commitments[nid] = []
        if not state.iq_commitments[nid]:
            state.iq_commitments[nid].append(
                {
                    "node_id": nid,
                    "capture_id": f"iq-{nid[-8:]}-001",
                    "sha256": hashlib.sha256(f"iq:{nid}".encode()).hexdigest(),
                    "_received_at": now_iso,
                }
            )


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
