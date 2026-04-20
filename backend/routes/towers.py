"""Tower-finding, config, elevation, and health endpoints."""

import json
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from clients.fcc import fetch_fcc_broadcast_systems
from clients.maprad import fetch_broadcast_systems
from core.auth import require_admin
from services.alerting import send_alert
from services.tower_ranking import (
    _CONFIG_PATH,
    DEFAULT_LIMIT,
    DEFAULT_RADIUS_KM,
    parse_user_frequencies,
    process_and_rank,
    reload_config,
)

router = APIRouter()

API_KEY = os.getenv("MAPRAD_API_KEY", "")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_source(lat: float, lon: float) -> str:
    if -45 <= lat <= -10 and 112 <= lon <= 155:
        return "au"
    if 42 <= lat <= 84 and -141 <= lon <= -52:
        return "ca"
    if 24 <= lat < 49 and -125 <= lon <= -66:
        return "us"
    if 51 <= lat <= 72 and -180 <= lon <= -129:
        return "us"
    if 18 <= lat <= 23 and -161 <= lon <= -154:
        return "us"
    return "us"


async def _lookup_elevation(lat: float, lon: float) -> float | None:
    result = await _batch_lookup_elevations([(lat, lon)])
    return result.get((round(lat, 6), round(lon, 6)))


async def _batch_lookup_elevations(
    coords: list[tuple[float, float]],
) -> dict[tuple[float, float], float]:
    if not coords:
        return {}
    url = "https://api.open-meteo.com/v1/elevation"
    unique = list(dict.fromkeys((round(c[0], 6), round(c[1], 6)) for c in coords))
    lats = ",".join(str(c[0]) for c in unique)
    lons = ",".join(str(c[1]) for c in unique)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"latitude": lats, "longitude": lons})
            resp.raise_for_status()
            data = resp.json()
            elevations = data.get("elevation", [])
            result = {}
            for i, coord in enumerate(unique):
                if i < len(elevations) and elevations[i] is not None:
                    result[coord] = float(elevations[i])
            return result
    except Exception as exc:
        logging.warning("Batch elevation lookup failed: %s", exc)
        return {}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/towers")
async def find_towers(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    altitude: float = Query(0, ge=0),
    radius_km: int = Query(0, ge=0, le=300),
    limit: int = Query(0, ge=0, le=200),
    source: str = Query("auto"),
    frequencies: str = Query(""),
):
    source = source.lower()
    if source == "auto":
        source = _detect_source(lat, lon)
    if source not in ("us", "au", "ca"):
        raise HTTPException(status_code=400, detail="Invalid source. Use: us, au, ca, auto")

    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT
    user_freqs = parse_user_frequencies(frequencies)

    try:
        if source == "us":
            raw = await fetch_fcc_broadcast_systems(lat, lon, radius_km=effective_radius)
            if API_KEY:
                try:
                    maprad_raw = await fetch_broadcast_systems(
                        API_KEY, lat, lon, radius_km=effective_radius, source=source,
                    )
                    raw.extend(maprad_raw)
                except Exception:
                    logging.warning("Maprad supplement failed, using FCC data only")
        else:
            if not API_KEY:
                raise HTTPException(status_code=500, detail="MAPRAD_API_KEY not configured")
            raw = await fetch_broadcast_systems(
                API_KEY, lat, lon, radius_km=effective_radius, source=source,
            )
    except HTTPException:
        raise
    except Exception:
        logging.exception("Tower data fetch failed")
        raise HTTPException(status_code=502, detail="External service unavailable. Please try again.") from None

    resolved_altitude = altitude
    if altitude == 0:
        elev = await _lookup_elevation(lat, lon)
        if elev is not None:
            resolved_altitude = elev

    towers = process_and_rank(raw, lat, lon, limit=effective_limit, user_frequencies=user_freqs, radius_km=effective_radius)

    tower_coords = [(t["latitude"], t["longitude"]) for t in towers]
    elevations = await _batch_lookup_elevations(tower_coords)
    for t in towers:
        key = (round(t["latitude"], 6), round(t["longitude"], 6))
        elev = elevations.get(key)
        t["elevation_m"] = round(elev, 1) if elev is not None else None
        if elev is not None and t.get("antenna_height_m") is not None:
            t["altitude_m"] = round(elev + t["antenna_height_m"], 1)
        elif elev is not None:
            t["altitude_m"] = round(elev, 1)
        else:
            t["altitude_m"] = None

    return {
        "towers": towers,
        "query": {
            "latitude": lat, "longitude": lon,
            "altitude_m": resolved_altitude,
            "radius_km": effective_radius,
            "source": source,
            "user_frequencies_mhz": user_freqs,
        },
        "count": len(towers),
    }


@router.get("/api/health")
async def health():
    import resource
    import shutil
    import sys
    import time

    from core import state

    issues = []

    # Check frame queue saturation (>90% = unhealthy)
    q_pct = state.frame_queue.qsize() / max(state.frame_queue.maxsize, 1)
    if q_pct > 0.9:
        issues.append(f"frame_queue_saturated ({q_pct:.0%})")

    # Check critical task staleness
    now = time.time()
    critical_tasks = {"frame_processor": 20, "analytics_refresh": 120, "aircraft_flush": 15}
    for task, max_age_s in critical_tasks.items():
        last = state.task_last_success.get(task)
        if last is not None and (now - last) > max_age_s:
            issues.append(f"stale_task:{task}")

    # Check solver queue drops (growing = solver can't keep up)
    if state.solver_queue_drops > 0:
        issues.append(f"solver_queue_drops:{state.solver_queue_drops}")

    # Check disk space (<500 MB free = critical)
    try:
        disk = shutil.disk_usage(state.COVERAGE_STORAGE_DIR)
        free_mb = disk.free / (1024 * 1024)
        if free_mb < 500:
            issues.append(f"disk_low:{free_mb:.0f}MB")
    except Exception:
        pass

    # Check process memory (>3 GB on a 4 GB droplet = danger)
    try:
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        rss_mb = rusage.ru_maxrss / 1024 if sys.platform == "linux" else rusage.ru_maxrss / (1024 * 1024)
        if rss_mb > 3000:
            issues.append(f"memory_high:{rss_mb:.0f}MB")
    except Exception:
        pass

    # Solver queue backpressure (>50% = early warning before drops start)
    solver_q_pct = state.solver_queue.qsize() / max(state.solver_queue.maxsize, 1)
    if solver_q_pct > 0.5:
        issues.append(f"solver_queue_high:{solver_q_pct:.0%}")

    # Solver end-to-end latency (>30s = solver threads are badly overloaded)
    if state.solver_last_latency_s > 30:
        issues.append(f"solver_latency_high:{state.solver_last_latency_s:.0f}s")

    # Node dropout (active nodes <80% of peak — fleet disconnect or TCP issue)
    _node_dropout_threshold = float(os.getenv("NODE_DROPOUT_THRESHOLD", "0.8"))
    with state.connected_nodes_lock:
        active_nodes = sum(
            1 for n in state.connected_nodes.values() if n.get("status") != "disconnected"
        )
    if (
        state.peak_connected_nodes > 10
        and active_nodes < state.peak_connected_nodes * _node_dropout_threshold
    ):
        issues.append(f"node_dropout:{active_nodes}/{state.peak_connected_nodes}")

    # Zero tracks (0 aircraft after warmup = pipeline failure)
    if (
        state.frames_processed > 500
        and len(state.adsb_aircraft) == 0
        and len(state.multinode_tracks) == 0
    ):
        issues.append("no_active_tracks")

    # Anomaly flood (>50% of aircraft anomalous = tracker misfiring, real alerts buried)
    _total_aircraft = len(state.adsb_aircraft)
    if _total_aircraft > 10 and len(state.anomaly_hexes) / _total_aircraft > 0.5:
        issues.append(f"anomaly_flood:{len(state.anomaly_hexes)}/{_total_aircraft}")

    # Solver accuracy degradation (mean position error >10 km = output untrustworthy)
    try:
        import orjson
        if state.latest_accuracy_bytes and state.latest_accuracy_bytes != b"{}":
            _acc = orjson.loads(state.latest_accuracy_bytes)
            if _acc.get("n_samples", 0) > 20 and _acc.get("mean_km", 0) > 10:
                issues.append(f"solver_accuracy_degraded:{_acc['mean_km']:.1f}km")
    except Exception:
        pass

    # Fleet-wide miss rate (avg >70% = sensor network effectively blind)
    try:
        if state.latest_missed_detections:
            _rates = [
                v["miss_rate"]
                for v in state.latest_missed_detections.values()
                if v.get("in_range", 0) > 0
            ]
            if _rates and sum(_rates) / len(_rates) > 0.7:
                _avg = sum(_rates) / len(_rates)
                issues.append(f"high_miss_rate:{_avg:.0%}")
    except Exception:
        pass

    if issues:
        logging.warning("Health check degraded: %s", ", ".join(issues))
        send_alert("health_degraded", f"Health check degraded: {', '.join(issues)}", {"issues": issues})
        return {"status": "degraded"}
    return {"status": "ok"}


@router.get("/api/config")
async def get_config():
    with open(_CONFIG_PATH) as f:
        return json.load(f)


@router.put("/api/config")
async def update_config(body: dict, _admin=Depends(require_admin)):
    # Sanity check: config should be a reasonable size
    raw = json.dumps(body)
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Config too large (max 1 MB)")
    with open(_CONFIG_PATH, "w") as f:
        f.write(json.dumps(body, indent=2))
    reload_config()
    return {"status": "updated"}


@router.get("/api/elevation")
async def get_elevation(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    elev = await _lookup_elevation(lat, lon)
    if elev is None:
        raise HTTPException(status_code=502, detail="Elevation lookup failed")
    return {"latitude": lat, "longitude": lon, "elevation_m": elev}
