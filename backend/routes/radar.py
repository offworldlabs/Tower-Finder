"""Radar pipeline endpoints: receiver/aircraft JSON, detections, nodes, status."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config.constants import RATE_BUCKETS_MAX_IPS
from core import state
from core.auth import require_admin
from pipeline.passive_radar import PassiveRadarPipeline
from services.tcp_handler import is_synthetic_node

router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────────

class DetectionRequest(BaseModel):
    node_id: str = Field(default="http-node", max_length=128)
    frames: list[dict] | None = None
    # Allow extra fields — individual frames are dicts with variable keys
    model_config = {"extra": "allow"}


class BulkNodeEntry(BaseModel):
    node_id: str = Field(default="http-node", max_length=128)
    config: dict | None = None
    frames: list[dict] = Field(default_factory=list)


class BulkDetectionRequest(BaseModel):
    nodes: list[BulkNodeEntry] = Field(..., max_length=500)


class LoadFileRequest(BaseModel):
    path: str = Field(..., min_length=1)

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")
_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()
if not RADAR_API_KEY:
    logging.warning("RADAR_API_KEY is not set — detection/custody endpoints have no API key protection")


def _check_api_key_configured() -> None:
    """Called from lifespan startup — raises RuntimeError if key missing in production."""
    if not RADAR_API_KEY and _RETINA_ENV not in ("dev", "test"):
        raise RuntimeError(
            "RADAR_API_KEY is required in production. "
            "Set it in backend/.env to protect detection/custody endpoints."
        )
_RATE_LIMIT = int(os.getenv("RADAR_RATE_LIMIT", "60"))
_RATE_WINDOW = int(os.getenv("RADAR_RATE_WINDOW", "60"))
_TAR1090_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tar1090_data")
_ALLOWED_DETECTION_DIR = os.path.realpath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data", "archive")
)

# Module-level reference to default pipeline; set from main.py at startup
_default_pipeline: PassiveRadarPipeline | None = None


def init(pipeline: PassiveRadarPipeline):
    global _default_pipeline
    _default_pipeline = pipeline


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    bucket = state.rate_buckets[ip]
    recent = [t for t in bucket if now - t < _RATE_WINDOW]
    if recent:
        state.rate_buckets[ip] = recent
    else:
        # All timestamps expired — free the key; defaultdict recreates on next append
        del state.rate_buckets[ip]
    if len(recent) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    # Prevent unbounded memory growth from unique IPs — evict oldest half
    if len(state.rate_buckets) > RATE_BUCKETS_MAX_IPS:
        evict_count = len(state.rate_buckets) // 2
        for old_ip in list(state.rate_buckets)[:evict_count]:
            del state.rate_buckets[old_ip]
    state.rate_buckets[ip].append(now)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/radar/data/receiver.json")
async def tar1090_receiver():
    return _default_pipeline.generate_receiver_json()


@router.get("/api/radar/data/aircraft.json")
async def tar1090_aircraft():
    return Response(content=state.latest_aircraft_json_bytes, media_type="application/json")


@router.get("/api/radar/data/aircraft-live.json")
async def tar1090_aircraft_live():
    """Real-node-only aircraft data for map.retina.fm HTTP polling fallback."""
    return Response(content=state.latest_real_aircraft_json_bytes, media_type="application/json")


@router.post("/api/radar/detections")
async def ingest_detections(
    request: Request,
    body: DetectionRequest,
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

    node_id = body.node_id
    body_dict = body.model_dump(exclude_none=True)
    frames = body.frames if body.frames is not None else [body_dict]

    if node_id not in state.connected_nodes:
        with state.connected_nodes_lock:
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
        with state.connected_nodes_lock:
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
    body: BulkDetectionRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    registered = 0
    queued = 0
    for entry in body.nodes:
        node_id = entry.node_id
        frames = entry.frames
        entry_config = entry.config or {"node_id": node_id}

        if node_id not in state.connected_nodes:
            with state.connected_nodes_lock:
                state.connected_nodes[node_id] = {
                    "config_hash": "",
                    "config": entry_config,
                    "status": "active",
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "peer": "http-bulk",
                    "is_synthetic": is_synthetic_node(node_id),
                    "capabilities": {},
                }
            state.node_analytics.register_node(node_id, entry_config)
            registered += 1
        else:
            with state.connected_nodes_lock:
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
async def load_detection_file(body: LoadFileRequest, _admin=Depends(require_admin)):
    filepath = os.path.realpath(body.path)
    if not filepath.startswith(_ALLOWED_DETECTION_DIR + os.sep):
        raise HTTPException(status_code=400, detail="Path must be inside the coverage archive directory")
    if not os.path.isfile(filepath):
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
    return Response(content=state.latest_nodes_bytes, media_type="application/json")
