"""Node analytics and inter-node association endpoints."""

import os
import time
from collections import Counter

import orjson
from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import Response
from retina_analytics.trust import AdsReportEntry

from core import state

_RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")

router = APIRouter()


@router.get("/api/radar/analytics")
async def radar_analytics(real_only: bool = False):
    if real_only:
        return Response(content=state.latest_analytics_real_bytes, media_type="application/json")
    return Response(content=state.latest_analytics_bytes, media_type="application/json")


@router.get("/api/radar/analytics/{node_id}")
async def radar_node_analytics(node_id: str):
    summary = state.node_analytics.get_node_summary(node_id)
    if summary.keys() == {"node_id"}:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return summary


@router.post("/api/radar/analytics/adsb-report")
async def submit_adsb_report(
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if _RADAR_API_KEY and x_api_key != _RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    required = ["node_id", "predicted_delay", "measured_delay"]
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing: {missing}")

    entry = AdsReportEntry(
        timestamp_ms=body.get("timestamp_ms", 0),
        predicted_delay=body["predicted_delay"],
        predicted_doppler=body.get("predicted_doppler", 0),
        measured_delay=body["measured_delay"],
        measured_doppler=body.get("measured_doppler", 0),
        adsb_hex=body.get("adsb_hex", ""),
        adsb_lat=body.get("adsb_lat", 0),
        adsb_lon=body.get("adsb_lon", 0),
    )
    state.node_analytics.record_adsb_correlation(body["node_id"], entry)
    ts = state.node_analytics.trust_scores.get(body["node_id"])
    return {
        "status": "recorded",
        "trust_score": round(ts.score, 4) if ts else 0.0,
        "n_samples": len(ts.samples) if ts else 0,
    }


@router.get("/api/radar/association/overlaps")
async def association_overlaps():
    return Response(content=state.latest_overlaps_bytes, media_type="application/json")


@router.get("/api/radar/accuracy")
async def radar_accuracy():
    """Solver-vs-ADS-B accuracy stats (mean, median, P95, per-source breakdown)."""
    return Response(content=state.latest_accuracy_bytes, media_type="application/json")


@router.get("/api/radar/association/status")
async def association_status():
    return {
        "registered_nodes": len(state.node_associator.node_geometries),
        "overlap_zones": len(state.node_associator.overlap_zones),
        "pending_frames": list(state.node_associator._pending_frames.keys()),
        "overlaps": state.node_associator.get_overlap_summary(),
    }


@router.get("/api/radar/anomalies")
async def radar_anomalies():
    """Anomaly metrics: summary, breakdown by type, timeline, geographic clusters, recent events."""
    now = time.time()

    with state.anomaly_lock:
        log_snapshot = list(state.anomaly_log)
        active_hexes = set(state.anomaly_hexes)

    # --- Live anomaly types from aircraft.json ---
    live_aircraft = state.latest_aircraft_json.get("aircraft", [])
    live_type_counts: Counter = Counter()
    for ac in live_aircraft:
        if ac.get("is_anomalous"):
            for atype in ac.get("anomaly_types", []):
                live_type_counts[atype] += 1

    # --- Breakdown by type from log + live ---
    log_type_counts: Counter = Counter()
    for ev in log_snapshot:
        log_type_counts[ev.get("reason", "unknown")] += 1

    by_type = dict(log_type_counts + live_type_counts)

    # --- Unique hexes in log ---
    unique_hexes = {ev.get("hex") for ev in log_snapshot if ev.get("hex")}

    # --- Timeline: 1-hour buckets over last 24h ---
    bucket_size = 3600
    cutoff = now - 86400
    buckets: Counter = Counter()
    for ev in log_snapshot:
        ts = ev.get("ts", 0)
        if ts >= cutoff:
            b = int(ts // bucket_size) * bucket_size
            buckets[b] += 1

    # Fill empty buckets so the chart is continuous
    timeline = []
    if buckets:
        first = min(buckets)
        last = max(buckets)
    else:
        first = int(cutoff // bucket_size) * bucket_size
        last = int(now // bucket_size) * bucket_size
    b = first
    while b <= last:
        timeline.append({"ts": b, "count": buckets.get(b, 0)})
        b += bucket_size

    # --- Geographic clusters: 0.1° grid ---
    geo_grid: dict[tuple, list] = {}
    for ev in log_snapshot:
        lat = ev.get("lat")
        lon = ev.get("lon")
        if lat is None or lon is None:
            continue
        key = (round(lat, 1), round(lon, 1))
        geo_grid.setdefault(key, []).append(ev.get("reason", "unknown"))

    clusters = []
    for (glat, glon), reasons in geo_grid.items():
        dominant = Counter(reasons).most_common(1)[0][0] if reasons else "unknown"
        clusters.append({
            "lat": glat,
            "lon": glon,
            "count": len(reasons),
            "dominant_type": dominant,
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)

    # --- Most common anomaly type ---
    all_types = log_type_counts + live_type_counts
    most_common = all_types.most_common(1)[0][0] if all_types else None

    payload = {
        "summary": {
            "active_count": len(active_hexes),
            "total_events": len(log_snapshot),
            "unique_hexes": len(unique_hexes),
            "most_common_type": most_common,
        },
        "by_type": by_type,
        "timeline": timeline,
        "geographic_clusters": clusters[:50],
        "recent_events": log_snapshot,
    }
    return Response(
        content=orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY),
        media_type="application/json",
    )
