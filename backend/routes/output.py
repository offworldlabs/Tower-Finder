"""Public output APIs — solver aircraft and ground truth.

These endpoints expose the server's current aircraft state and ground-truth
data in a clean, documented JSON format suitable for external consumers.

Endpoints
---------
GET /api/v1/solver/aircraft       – All solver-positioned aircraft
GET /api/v1/ground-truth/aircraft – Ground truth trails + metadata
"""

import time
from collections import deque

import orjson
from fastapi import APIRouter
from fastapi.responses import Response

from core import state

router = APIRouter()


@router.get("/api/v1/solver/aircraft")
async def solver_aircraft():
    """Return all aircraft the solver is currently tracking.

    Each entry includes the solver's position estimate, position source
    (single-node ellipse, multi-node fix, ADS-B association), velocity,
    and detection metadata.

    Response shape::

        {
          "timestamp": <epoch seconds>,
          "count": <int>,
          "aircraft": [
            {
              "hex": "A1B2C3",
              "lat": 33.749,
              "lon": -84.388,
              "alt_baro": 35000,
              "gs": 420.3,
              "track": 180.5,
              "position_source": "adsb_associated" | "solver_single_node"
                                 | "multinode_lm" | "adsb_node_report"
                                 | "single_node_ellipse_arc",
              "multinode": true | false,
              "n_nodes": 2,
              "flight": "DAL123",
              "node_id": "node-abc",
              "target_class": "aircraft" | "drone" | null,
              "delay_us": 12.345,
              "doppler_hz": -45.67,
              "recent_positions": [{"lat":..,"lon":..,"alt":..,"t":..}, ...]
            }, ...
          ]
        }
    """
    data = state.latest_aircraft_json
    now = time.time()
    aircraft_out = []
    for ac in data.get("aircraft", []):
        aircraft_out.append({
            "hex": ac.get("hex"),
            "lat": ac.get("lat"),
            "lon": ac.get("lon"),
            "alt_baro": ac.get("alt_baro"),
            "gs": ac.get("gs"),
            "track": ac.get("track"),
            "position_source": ac.get("position_source"),
            "multinode": ac.get("multinode", False),
            "n_nodes": ac.get("n_nodes", 1),
            "flight": ac.get("flight"),
            "node_id": ac.get("node_id"),
            "target_class": ac.get("target_class"),
            "delay_us": ac.get("delay_us"),
            "doppler_hz": ac.get("doppler_hz"),
            "recent_positions": ac.get("recent_positions", []),
        })

    body = orjson.dumps({
        "timestamp": now,
        "count": len(aircraft_out),
        "aircraft": aircraft_out,
    })
    return Response(content=body, media_type="application/json")


@router.get("/api/v1/ground-truth/aircraft")
async def ground_truth_aircraft():
    """Return ground truth positions and metadata for all known aircraft.

    Ground truth is pushed by the fleet orchestrator (simulation) via
    ``POST /api/test/ground-truth/push``.  Each aircraft has a trail of
    recent positions and metadata (object type, anomaly flag).

    Response shape::

        {
          "timestamp": <epoch seconds>,
          "count": <int>,
          "aircraft": [
            {
              "hex": "SIM-001",
              "object_type": "aircraft" | "drone" | "dark",
              "is_anomalous": false,
              "trail": [
                {"lat": 33.75, "lon": -84.39, "alt_km": 10.5, "t": 1700000000.0},
                ...
              ]
            }, ...
          ]
        }
    """
    now = time.time()
    aircraft_out = []
    for hex_code, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        meta = state.ground_truth_meta.get(hex_code, {})
        aircraft_out.append({
            "hex": hex_code,
            "object_type": meta.get("object_type"),
            "is_anomalous": meta.get("is_anomalous", False),
            "trail": list(trail)[-30:],
        })

    body = orjson.dumps({
        "timestamp": now,
        "count": len(aircraft_out),
        "aircraft": aircraft_out,
    })
    return Response(content=body, media_type="application/json")
