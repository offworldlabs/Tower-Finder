"""Radar pipeline endpoints: receiver/aircraft JSON, detections, nodes, status."""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request, Header

from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from services.tcp_handler import is_synthetic_node

router = APIRouter()

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")
_RATE_LIMIT = int(os.getenv("RADAR_RATE_LIMIT", "60"))
_RATE_WINDOW = int(os.getenv("RADAR_RATE_WINDOW", "60"))
_TAR1090_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tar1090_data")

# Module-level reference to default pipeline; set from main.py at startup
_default_pipeline: PassiveRadarPipeline | None = None


def init(pipeline: PassiveRadarPipeline):
    global _default_pipeline
    _default_pipeline = pipeline


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    bucket = state.rate_buckets[ip]
    state.rate_buckets[ip] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(state.rate_buckets[ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    state.rate_buckets[ip].append(now)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/radar/data/receiver.json")
async def tar1090_receiver():
    return _default_pipeline.generate_receiver_json()


@router.get("/api/radar/data/aircraft.json")
async def tar1090_aircraft():
    return state.latest_aircraft_json


@router.post("/api/radar/detections")
async def ingest_detections(
    request: Request,
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    if not (RADAR_API_KEY and x_api_key == RADAR_API_KEY):
        client_ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        _check_rate_limit(client_ip)

    node_id = body.get("node_id", "http-node")
    frames = body.get("frames", [body]) if "frames" in body else [body]

    if node_id not in state.connected_nodes:
        state.connected_nodes[node_id] = {
            "config_hash": "",
            "config": {"node_id": node_id},
            "status": "active",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "peer": "http",
            "is_synthetic": is_synthetic_node(node_id),
            "capabilities": {},
        }
        state.node_analytics.register_node(node_id, {"node_id": node_id})
        state.node_associator.register_node(node_id, {"node_id": node_id})
    else:
        state.connected_nodes[node_id]["status"] = "active"
        state.connected_nodes[node_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

    processed = 0
    for frame in frames:
        if "timestamp" not in frame:
            continue
        frame["_node_id"] = node_id
        try:
            state.frame_queue.put_nowait((node_id, frame))
            processed += 1
        except asyncio.QueueFull:
            logging.warning("Frame queue full, dropping frame from %s", node_id)

    return {
        "status": "ok",
        "frames_queued": processed,
        "tracks": len(state.latest_aircraft_json.get("aircraft", [])),
    }


@router.post("/api/radar/detections/bulk")
async def ingest_detections_bulk(
    request: Request,
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    nodes_list = body.get("nodes", [])
    if not isinstance(nodes_list, list):
        raise HTTPException(status_code=400, detail="'nodes' must be an array")

    registered = 0
    queued = 0
    for entry in nodes_list:
        node_id = entry.get("node_id", "http-node")
        frames = entry.get("frames", [])

        if node_id not in state.connected_nodes:
            state.connected_nodes[node_id] = {
                "config_hash": "",
                "config": entry.get("config", {"node_id": node_id}),
                "status": "active",
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "peer": "http-bulk",
                "is_synthetic": is_synthetic_node(node_id),
                "capabilities": {},
            }
            state.node_analytics.register_node(node_id, entry.get("config", {"node_id": node_id}))
            registered += 1
        else:
            state.connected_nodes[node_id]["status"] = "active"
            state.connected_nodes[node_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

        for frame in frames:
            if "timestamp" not in frame:
                continue
            frame["_node_id"] = node_id
            try:
                state.frame_queue.put_nowait((node_id, frame))
                queued += 1
            except asyncio.QueueFull:
                break

    return {"status": "ok", "nodes_registered": registered, "frames_queued": queued}


@router.post("/api/radar/load-file")
async def load_detection_file(body: dict = Body(...)):
    filepath = body.get("path", "")
    if not filepath or not os.path.isfile(filepath):
        raise HTTPException(status_code=400, detail="File not found")
    if not filepath.endswith(".detection"):
        raise HTTPException(status_code=400, detail="Only .detection files accepted")

    tracks = _default_pipeline.process_file(filepath)
    aircraft_data = _default_pipeline.generate_aircraft_json()
    with open(os.path.join(_TAR1090_DATA_DIR, "aircraft.json"), "w") as f:
        json.dump(aircraft_data, f)

    return {"status": "ok", "tracks": len(tracks), "aircraft": aircraft_data["aircraft"]}


@router.get("/api/radar/status")
async def radar_status():
    return {
        "node_id": _default_pipeline.node_id,
        "total_tracks": len(_default_pipeline.tracker.tracks),
        "geolocated_tracks": len(_default_pipeline.geolocated_tracks),
        "multinode_tracks": len(state.multinode_tracks),
        "track_events": len(_default_pipeline.event_writer.get_events()),
        "external_adsb_cached": len(state.external_adsb_cache),
        "config": {
            "rx_lat": _default_pipeline.config["rx_lat"],
            "rx_lon": _default_pipeline.config["rx_lon"],
            "tx_lat": _default_pipeline.config["tx_lat"],
            "tx_lon": _default_pipeline.config["tx_lon"],
            "FC": _default_pipeline.config["FC"],
            "Fs": _default_pipeline.config["Fs"],
        },
    }


@router.get("/api/radar/nodes")
async def radar_nodes():
    return {
        "nodes": {
            nid: {
                "status": info.get("status"),
                "config_hash": info.get("config_hash"),
                "last_heartbeat": info.get("last_heartbeat"),
                "peer": info.get("peer"),
                "is_synthetic": info.get("is_synthetic", is_synthetic_node(nid)),
                "capabilities": info.get("capabilities", {}),
            }
            for nid, info in state.connected_nodes.items()
        },
        "connected": sum(1 for n in state.connected_nodes.values() if n.get("status") not in ("disconnected",)),
        "total": len(state.connected_nodes),
        "synthetic": sum(1 for n in state.connected_nodes.values() if n.get("is_synthetic")),
    }
