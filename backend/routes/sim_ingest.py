"""Simulation ingest endpoints — the write path from the fleet orchestrator.

Split out of routes/test.py: these two POSTs are primary DATA-INGEST paths
(they write state.adsb_aircraft and state.ground_truth_trails directly), not
diagnostics.  main.py mounts this router only when RETINA_ENV != production
or SIM_INGEST_ENABLED=1 — production previously carried an
API-key-but-otherwise-open ingest surface it never uses.
"""

import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from core import state

# The API-key rule (and its production fail-fast) lives beside the other
# consumers in routes.test.
from routes.test import _verify_sim_key
from services.geo import valid_latlon
from services.id_utils import normalize_hex_key

router = APIRouter()


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
        lat = ac.get("lat")
        lon = ac.get("lon")
        alt_m = ac.get("alt_m") or ac.get("alt_km", 0) * 1000
        if not valid_latlon(lat, lon):
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
            "has_adsb": ac.get("has_adsb", False),
            "adsb_callsign": ac.get("adsb_callsign"),
            "anomaly_event": ac.get("anomaly_event"),
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
                        state.anomaly_log = state.anomaly_log[-state.ANOMALY_LOG_MAX :]
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
        if not valid_latlon(lat, lon):
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
