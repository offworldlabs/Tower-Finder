"""Simulation ingest endpoints — the write path from the fleet orchestrator.

Split out of routes/test.py: these two POSTs are primary DATA-INGEST paths
(they write state.adsb_aircraft and state.ground_truth_trails directly), not
diagnostics.  main.py mounts this router only under SYNTHETIC_FLEET_ENABLED=1,
so a deployment that runs no fleet carries no API-key-but-otherwise-open ingest
surface it never uses.
"""

import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException

from core import state

# The API-key rule (and its production fail-fast) lives beside the other
# consumers in routes.test.
from routes.test import _verify_sim_key
from services.geo import valid_latlon
from services.id_utils import normalize_hex_key

router = APIRouter()


def synthetic_fleet_enabled(env: Mapping[str, str]) -> bool:
    """Whether this deployment asked for the simulation subsystem.

    The gate on mounting the router below, and the whole of it. It used to be
    that any environment not named `production` got these routes, so a
    deployment carried a write path because of what it was called rather than
    because anyone chose it. Naming the behaviour lets a fleetless deployment
    close the path by dropping one line, whatever its name.

    Parameterised on `env` rather than reading os.environ so the rule is
    testable without importing main, which decides the mount once at import.
    """
    return env.get("SYNTHETIC_FLEET_ENABLED", "") == "1"


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
        moved = True
        if trail:
            dlat = abs(trail[-1][0] - lat)
            dlon = abs(trail[-1][1] - lon)
            moved = not (dlat < 0.00005 and dlon < 0.00005)
        if moved:
            trail.append([round(lat, 6), round(lon, 6), round(alt_m, 0), round(ts, 1)])
        else:
            # Sub-5.5 m movement: don't append a duplicate point, but DO
            # refresh the liveness timestamp and fall through to the meta /
            # anomaly update.  The old `continue` here starved slow or
            # hovering objects: their last trail timestamp aged past the
            # GT_DISPLAY_STALE_S GC while they were still being pushed every
            # 2 s, so the dot blinked out until they cleared 5.5 m — and
            # anomaly transitions during a hover were silently dropped.
            trail[-1][3] = round(ts, 1)
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
