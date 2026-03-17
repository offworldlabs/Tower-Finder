import asyncio
import json
import os
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from collections import defaultdict

import httpx
from fastapi import FastAPI, Query, HTTPException, Body, WebSocket, WebSocketDisconnect, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

from maprad_client import fetch_broadcast_systems
from fcc_client import fetch_fcc_broadcast_systems
from calculations import (
    process_and_rank, reload_config, _CONFIG_PATH,
    DEFAULT_RADIUS_KM, DEFAULT_LIMIT, parse_user_frequencies,
)
from passive_radar import PassiveRadarPipeline, DEFAULT_NODE_CONFIG
from node_analytics import NodeAnalyticsManager, AdsReportEntry
from inter_node_association import InterNodeAssociator
from retina_geolocator.multinode_solver import solve_multinode
from storage import archive_detections, list_archived_files, read_archived_file

load_dotenv()
logging.basicConfig(level=logging.INFO)

TCP_PORT = int(os.getenv("RADAR_TCP_PORT", "3012"))

# ── Connected node state tracking ─────────────────────────────────────────────
_connected_nodes: dict[str, dict] = {}  # node_id → {config_hash, config, status, last_heartbeat, peer, is_synthetic, capabilities}
_COVERAGE_STORAGE_DIR = os.path.join(os.path.dirname(__file__), "coverage_data")
_node_analytics = NodeAnalyticsManager(storage_dir=_COVERAGE_STORAGE_DIR)
_node_associator = InterNodeAssociator()
_multinode_tracks: dict[str, dict] = {}  # key → solver result for multi-node geolocations

# External ADS-B truth source (OpenSky Network)
# Cached positions: {icao_hex: {lat, lon, alt_m, timestamp}}
_external_adsb_cache: dict[str, dict] = {}

# ── WebSocket broadcast infrastructure ────────────────────────────────────────
_ws_clients: set[WebSocket] = set()
_latest_aircraft_json: dict = {"now": 0, "aircraft": [], "messages": 0}


async def _fetch_external_adsb():
    """Fetch aircraft positions from OpenSky Network as independent truth source.

    Queries for aircraft in the bounding box covering all connected nodes,
    then cross-references with node-reported ADS-B data to validate trust.
    """
    global _external_adsb_cache

    # Compute bounding box from all connected nodes
    active_nodes = [
        info for info in _connected_nodes.values()
        if info.get("status") != "disconnected" and info.get("config")
    ]
    if not active_nodes:
        return

    lats = [n["config"].get("rx_lat", 0) for n in active_nodes]
    lons = [n["config"].get("rx_lon", 0) for n in active_nodes]
    if not lats or all(l == 0 for l in lats):
        return

    # Expand bounding box by 1° (~111 km) around the node cluster
    lamin = min(lats) - 1.0
    lamax = max(lats) + 1.0
    lomin = min(lons) - 1.0
    lomax = max(lons) + 1.0

    url = "https://opensky-network.org/api/states/all"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params={
                "lamin": lamin, "lamax": lamax,
                "lomin": lomin, "lomax": lomax,
            })
            if resp.status_code != 200:
                return
            data = resp.json()
    except Exception:
        return

    states = data.get("states", [])
    if not states:
        return

    now_cache = {}
    for s in states:
        # OpenSky state vector fields:
        # [0]=icao24, [5]=lon, [6]=lat, [7]=baro_alt, [13]=velocity, [10]=heading
        icao = s[0] if s[0] else None
        lon_val = s[5]
        lat_val = s[6]
        alt_val = s[7]  # meters (barometric)
        if icao and lat_val is not None and lon_val is not None:
            now_cache[icao] = {
                "lat": lat_val,
                "lon": lon_val,
                "alt_m": alt_val or 0,
                "velocity": s[9] if len(s) > 9 else None,
                "heading": s[10] if len(s) > 10 else None,
            }

    _external_adsb_cache = now_cache
    logging.debug("External ADS-B: cached %d aircraft positions", len(now_cache))

    # Cross-validate any node-reported ADS-B correlations against external truth
    _cross_validate_adsb_reports()


def _cross_validate_adsb_reports():
    """Compare node-reported ADS-B data against external OpenSky positions.

    If a node claims to see aircraft X at position P, but OpenSky says
    aircraft X is actually at position Q (far from P), the node's trust
    score is penalised.
    """
    import math
    if not _external_adsb_cache:
        return

    for node_id, ts_state in _node_analytics.trust_scores.items():
        if not ts_state.samples:
            continue
        # Check the most recent samples
        for sample in ts_state.samples[-10:]:
            if not sample.adsb_hex:
                continue
            ext = _external_adsb_cache.get(sample.adsb_hex.lower())
            if ext is None:
                continue
            # Compare reported position vs external truth
            dlat = sample.adsb_lat - ext["lat"]
            dlon = sample.adsb_lon - ext["lon"]
            dist_km = math.sqrt(dlat ** 2 + dlon ** 2) * 111.0
            if dist_km > 10.0:
                # Node-reported ADS-B position diverges from external truth
                rep = _node_analytics.reputations.get(node_id)
                if rep:
                    rep.apply_penalty(
                        0.1,
                        f"ADS-B position mismatch: {sample.adsb_hex} "
                        f"reported {dist_km:.1f}km from external truth"
                    )
                    logging.warning(
                        "Node %s ADS-B mismatch for %s: %.1f km off",
                        node_id, sample.adsb_hex, dist_km,
                    )


def _get_node_configs() -> dict[str, dict]:
    """Collect config dicts for all connected nodes (for the multi-node solver)."""
    configs = {}
    for nid, info in _connected_nodes.items():
        cfg = info.get("config")
        if cfg:
            configs[nid] = cfg
    return configs

RETINA_PROTOCOL_VERSION = "1.0"
SERVER_CAPABILITIES = {
    "config_request": True,
    "adsb_report": True,
    "association": True,
    "analytics": True,
    "coverage_map": True,
}


def _is_synthetic_node(node_id: str) -> bool:
    """Detect synthetic nodes by their 'synth-' ID prefix."""
    return node_id.startswith("synth-")


async def _send_msg(writer: asyncio.StreamWriter, msg: dict):
    """Send a newline-delimited JSON message to a node."""
    writer.write(json.dumps(msg).encode("utf-8") + b"\n")
    await writer.drain()


async def _handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single TCP connection from a synthetic or real radar node.

    Implements the RETINA node protocol:
      1. Node sends HELLO → server validates version
      2. Node sends CONFIG → server stores config, replies CONFIG_ACK
      3. Steady state: node sends HEARTBEAT + DETECTION messages
      4. Server sends CONFIG_REQUEST if heartbeat config hash mismatches
    """
    peer = writer.get_extra_info("peername")
    logging.info("Radar TCP: new connection from %s", peer)
    buf = b""
    node_id = None
    handshake_complete = False

    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logging.debug("Radar TCP: malformed JSON from %s", peer)
                    continue

                msg_type = msg.get("type")

                # ── HELLO ──────────────────────────────────────────────
                if msg_type == "HELLO":
                    node_id = msg.get("node_id", f"unknown-{peer}")
                    version = msg.get("version", "0.0")
                    is_synthetic = msg.get("is_synthetic", _is_synthetic_node(node_id))
                    node_capabilities = msg.get("capabilities", {})
                    logging.info("Radar TCP: HELLO from %s (version %s, synthetic=%s, caps=%s)",
                                 node_id, version, is_synthetic, list(node_capabilities.keys()))
                    continue

                # ── CONFIG ─────────────────────────────────────────────
                if msg_type == "CONFIG":
                    if node_id is None:
                        node_id = msg.get("node_id", f"unknown-{peer}")
                    config_hash = msg.get("config_hash", "")
                    config_payload = msg.get("config", {})
                    is_synthetic = msg.get("is_synthetic", _is_synthetic_node(node_id))
                    _connected_nodes[node_id] = {
                        "config_hash": config_hash,
                        "config": config_payload,
                        "status": "active",
                        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                        "peer": str(peer),
                        "is_synthetic": is_synthetic,
                        "capabilities": msg.get("capabilities", {}),
                    }
                    logging.info("Radar TCP: CONFIG from %s (hash=%s, synthetic=%s)", node_id, config_hash, is_synthetic)
                    await _send_msg(writer, {
                        "type": "CONFIG_ACK",
                        "config_hash": config_hash,
                        "server_version": RETINA_PROTOCOL_VERSION,
                        "server_capabilities": SERVER_CAPABILITIES,
                    })
                    # Register with analytics and association
                    _node_analytics.register_node(node_id, config_payload)
                    _node_associator.register_node(node_id, config_payload)
                    handshake_complete = True
                    continue

                # ── HEARTBEAT ──────────────────────────────────────────
                if msg_type == "HEARTBEAT":
                    hb_node_id = msg.get("node_id", node_id)
                    hb_hash = msg.get("config_hash", "")
                    hb_status = msg.get("status", "active")
                    if hb_node_id and hb_node_id in _connected_nodes:
                        _connected_nodes[hb_node_id]["last_heartbeat"] = msg.get("timestamp") or datetime.now(timezone.utc).isoformat()
                        _connected_nodes[hb_node_id]["status"] = hb_status
                        _node_analytics.record_heartbeat(hb_node_id)
                        stored_hash = _connected_nodes[hb_node_id].get("config_hash", "")
                        if stored_hash and hb_hash != stored_hash:
                            logging.warning("Radar TCP: config drift for %s (expected=%s got=%s)", hb_node_id, stored_hash, hb_hash)
                            await _send_msg(writer, {
                                "type": "CONFIG_REQUEST",
                                "node_id": hb_node_id,
                            })
                    continue

                # ── DETECTION ──────────────────────────────────────────
                if msg_type == "DETECTION":
                    frame = msg.get("data", msg)
                    if "timestamp" not in frame:
                        continue
                    # Tag with node_id for multi-node tracking
                    if node_id:
                        frame["_node_id"] = node_id
                    # Analytics
                    if node_id:
                        _node_analytics.record_detection_frame(node_id, frame)
                        assoc = _node_associator.submit_frame(
                            node_id, frame, frame.get("timestamp", 0),
                        )
                        if assoc:
                            solver_inputs = _node_associator.format_candidates_for_solver(assoc)
                            node_cfgs = _get_node_configs()
                            for s_in in solver_inputs:
                                if s_in["n_nodes"] < 2:
                                    continue
                                try:
                                    result = solve_multinode(s_in, node_cfgs)
                                except Exception:
                                    logging.debug("Multi-node solver exception", exc_info=True)
                                    result = None
                                if result and result.get("success"):
                                    key = f"mn-{result['timestamp_ms']}-{result['lat']:.3f}"
                                    _multinode_tracks[key] = result
                                    logging.info(
                                        "Multi-node solve: (%.4f,%.4f) alt=%.0fm "
                                        "v=(%.1f,%.1f) rms_d=%.2fμs rms_f=%.1fHz nodes=%d",
                                        result["lat"], result["lon"], result["alt_m"],
                                        result["vel_east"], result["vel_north"],
                                        result["rms_delay"], result["rms_doppler"],
                                        result["n_nodes"],
                                    )
                    _radar_pipeline.process_frame(frame)
                    aircraft_data = _radar_pipeline.generate_aircraft_json()
                    aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                    with open(aircraft_path, "w") as f:
                        json.dump(aircraft_data, f)
                    await _broadcast_aircraft(aircraft_data)
                    _node_analytics.maybe_auto_save()
                    # Archive detection batch to local / B2
                    try:
                        archive_detections(node_id or "unknown", [frame])
                    except Exception:
                        logging.debug("Archive write failed", exc_info=True)
                    continue

                # ── Legacy: bare detection frame (no type field) ───────
                if "timestamp" in msg and msg_type is None:
                    if node_id:
                        msg["_node_id"] = node_id
                        _node_analytics.record_detection_frame(node_id, msg)
                    _radar_pipeline.process_frame(msg)
                    aircraft_data = _radar_pipeline.generate_aircraft_json()
                    aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                    with open(aircraft_path, "w") as f:
                        json.dump(aircraft_data, f)
                    await _broadcast_aircraft(aircraft_data)
                    continue

    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        if node_id and node_id in _connected_nodes:
            _connected_nodes[node_id]["status"] = "disconnected"
        logging.info("Radar TCP: connection closed from %s (node=%s)", peer, node_id)
        writer.close()


async def _broadcast_aircraft(aircraft_data: dict):
    """Push updated aircraft data to all connected WebSocket clients."""
    global _latest_aircraft_json
    _latest_aircraft_json = aircraft_data
    if not _ws_clients:
        return
    payload = json.dumps(aircraft_data)
    stale = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            stale.add(ws)
    _ws_clients.difference_update(stale)


async def _reputation_evaluator():
    """Periodically evaluate node reputations (every 60s)."""
    while True:
        await asyncio.sleep(60)
        try:
            _node_analytics.evaluate_reputations()
        except Exception:
            logging.exception("Reputation evaluation failed")


async def _adsb_truth_fetcher():
    """Periodically fetch external ADS-B positions from OpenSky Network (every 30s).

    Provides an independent truth source for trust scoring, preventing
    nodes from self-validating with fabricated ADS-B data.
    """
    while True:
        await asyncio.sleep(30)
        try:
            await _fetch_external_adsb()
        except Exception:
            logging.debug("External ADS-B fetch skipped: %s", "no connected nodes or API error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    server = await asyncio.start_server(_handle_tcp_client, "0.0.0.0", TCP_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logging.info("Radar TCP server listening on %s", addrs)
    async with server:
        server_task = asyncio.create_task(server.serve_forever())
        reputation_task = asyncio.create_task(_reputation_evaluator())
        adsb_truth_task = asyncio.create_task(_adsb_truth_fetcher())
        yield
        server_task.cancel()
        reputation_task.cancel()
        adsb_truth_task.cancel()
        # Persist coverage maps on graceful shutdown
        _node_analytics.save_coverage_maps()
        logging.info("Coverage maps saved to %s", _COVERAGE_STORAGE_DIR)


app = FastAPI(title="Tower Finder API", lifespan=lifespan)

_CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,https://retina.fm,https://api.retina.fm",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.getenv("MAPRAD_API_KEY", "")
RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")  # required for POST /api/radar/detections when set

# ── Rate limiter: max 60 requests per 60s per IP ───────────────────────────────
_rate_buckets: dict[str, list] = defaultdict(list)
_RATE_LIMIT = int(os.getenv("RADAR_RATE_LIMIT", "60"))  # requests
_RATE_WINDOW = int(os.getenv("RADAR_RATE_WINDOW", "60"))  # seconds

def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    # Remove timestamps outside the window
    _rate_buckets[ip] = [t for t in bucket if now - t < _RATE_WINDOW]
    if len(_rate_buckets[ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    _rate_buckets[ip].append(now)


def _detect_source(lat: float, lon: float) -> str:
    """Detect data source from coordinates using bounding boxes."""
    if -45 <= lat <= -10 and 112 <= lon <= 155:
        return "au"
    # Canada checked before US: covers southern Ontario/Quebec down to 42°N
    if 42 <= lat <= 84 and -141 <= lon <= -52:
        return "ca"
    if 24 <= lat < 49 and -125 <= lon <= -66:
        return "us"
    if 51 <= lat <= 72 and -180 <= lon <= -129:
        return "us"  # Alaska
    if 18 <= lat <= 23 and -161 <= lon <= -154:
        return "us"  # Hawaii
    return "us"  # default fallback


async def _lookup_elevation(lat: float, lon: float) -> float | None:
    """Fetch ground elevation in metres from the Open-Meteo API."""
    result = await _batch_lookup_elevations([(lat, lon)])
    return result.get((round(lat, 6), round(lon, 6)))


async def _batch_lookup_elevations(
    coords: list[tuple[float, float]],
) -> dict[tuple[float, float], float]:
    """Fetch ground elevation for multiple coordinates in one Open-Meteo call."""
    if not coords:
        return {}
    url = "https://api.open-meteo.com/v1/elevation"
    # Deduplicate
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


@app.get("/api/towers")
async def find_towers(
    lat: float = Query(..., ge=-90, le=90, description="Latitude"),
    lon: float = Query(..., ge=-180, le=180, description="Longitude"),
    altitude: float = Query(0, ge=0, description="Receiver altitude in metres"),
    radius_km: int = Query(0, ge=0, le=300, description="Search radius in km (0 = use config default)"),
    limit: int = Query(0, ge=0, le=200, description="Max towers to return (0 = use config default)"),
    source: str = Query("auto", description="Data source: us, au, ca, auto"),
    frequencies: str = Query("", description="Comma-separated measured frequencies in MHz (up to 10)"),
):
    """
    Return nearby broadcast towers ranked for passive-radar suitability.
    """
    source = source.lower()
    if source == "auto":
        source = _detect_source(lat, lon)
    if source not in ("us", "au", "ca"):
        raise HTTPException(status_code=400, detail="Invalid source. Use: us, au, ca, auto")

    # Use config defaults if caller didn't specify
    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT

    # Parse user-measured frequencies (up to 10)
    user_freqs = parse_user_frequencies(frequencies)

    try:
        if source == "us":
            # Use FCC as primary source for US (more complete than Maprad)
            raw = await fetch_fcc_broadcast_systems(lat, lon, radius_km=effective_radius)
            # Supplement with Maprad if API key is available
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
    except Exception as exc:
        logging.exception("Tower data fetch failed")
        raise HTTPException(status_code=502, detail=f"Upstream API error: {exc}")

    # Auto-resolve altitude if not provided
    resolved_altitude = altitude
    if altitude == 0:
        elev = await _lookup_elevation(lat, lon)
        if elev is not None:
            resolved_altitude = elev

    towers = process_and_rank(raw, lat, lon, limit=effective_limit, user_frequencies=user_freqs, radius_km=effective_radius)

    # Enrich towers with ground elevation and total altitude above sea level
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
            "latitude": lat,
            "longitude": lon,
            "altitude_m": resolved_altitude,
            "radius_km": effective_radius,
            "source": source,
            "user_frequencies_mhz": user_freqs,
        },
        "count": len(towers),
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def get_config():
    """Return the current tower ranking configuration."""
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


@app.put("/api/config")
async def update_config(body: dict):
    """Update tower ranking configuration and reload."""
    with open(_CONFIG_PATH, "w") as f:
        json.dump(body, f, indent=2)
    reload_config()
    return {"status": "updated"}


@app.get("/api/elevation")
async def get_elevation(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Return the ground elevation (metres above sea level) for a coordinate."""
    elev = await _lookup_elevation(lat, lon)
    if elev is None:
        raise HTTPException(status_code=502, detail="Elevation lookup failed")
    return {"latitude": lat, "longitude": lon, "elevation_m": elev}


# ── Tower usage statistics ────────────────────────────────────────────────────
_STATS_PATH = os.path.join(os.path.dirname(__file__), "tower_stats.json")


def _load_stats() -> dict:
    if os.path.exists(_STATS_PATH):
        with open(_STATS_PATH, "r") as f:
            return json.load(f)
    return {"selections": []}


def _save_stats(stats: dict):
    with open(_STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)


@app.post("/api/stats/tower-selection")
async def record_tower_selection(
    body: dict = Body(...),
):
    """
    Record that a node selected a specific tower.
    Expected body: {
        "node_id": "...",
        "tower_callsign": "...",
        "tower_frequency_mhz": 123.4,
        "tower_lat": ..., "tower_lon": ...,
        "node_lat": ..., "node_lon": ...,
        "source": "au"
    }
    """
    required = ["tower_callsign", "tower_frequency_mhz", "node_lat", "node_lon"]
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    stats = _load_stats()
    stats["selections"].append({
        **body,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _save_stats(stats)
    return {"status": "recorded", "total_selections": len(stats["selections"])}


@app.get("/api/stats/summary")
async def tower_stats_summary():
    """
    Returns aggregated tower usage statistics.
    Shows which towers are most used and geographic coverage gaps.
    """
    stats = _load_stats()
    selections = stats.get("selections", [])

    # Aggregate by tower
    tower_usage: dict[str, int] = {}
    for s in selections:
        key = f"{s.get('tower_callsign', '?')}@{s.get('tower_frequency_mhz', '?')}"
        tower_usage[key] = tower_usage.get(key, 0) + 1

    # Sort by usage count descending
    ranked = sorted(tower_usage.items(), key=lambda x: -x[1])

    return {
        "total_selections": len(selections),
        "unique_towers": len(tower_usage),
        "tower_usage": [{"tower": k, "selections": v} for k, v in ranked],
    }


# ── Passive Radar / tar1090 Data Feed ────────────────────────────────────────

_TAR1090_DATA_DIR = os.path.join(os.path.dirname(__file__), "tar1090_data")
os.makedirs(_TAR1090_DATA_DIR, exist_ok=True)

# Global pipeline instance — processes incoming detection frames in real-time
_radar_pipeline = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

# Write initial receiver.json
_receiver_json = _radar_pipeline.generate_receiver_json()
with open(os.path.join(_TAR1090_DATA_DIR, "receiver.json"), "w") as _f:
    json.dump(_receiver_json, _f)


import math as _math


def _multinode_to_aircraft(key: str, r: dict) -> dict:
    """Convert a multi-node solver result to tar1090-compatible aircraft dict."""
    speed_ms = _math.sqrt(r["vel_east"] ** 2 + r["vel_north"] ** 2)
    heading = _math.degrees(_math.atan2(r["vel_east"], r["vel_north"])) % 360
    return {
        "hex": f"mn{abs(hash(key)) % 0xFFFF:04x}",
        "type": "multinode_solve",
        "flight": f"MN{r['n_nodes']}N",
        "alt_baro": round(r["alt_m"] / 0.3048),
        "alt_geom": round(r["alt_m"] / 0.3048),
        "gs": round(speed_ms * 1.94384, 1),
        "track": round(heading, 1),
        "lat": round(r["lat"], 5),
        "lon": round(r["lon"], 5),
        "seen": 0,
        "messages": r["n_measurements"],
        "rssi": -round(1.0 / max(r.get("rms_delay", 1), 0.01), 1),
        "multinode": True,
        "n_nodes": r["n_nodes"],
        "rms_delay": round(r["rms_delay"], 3),
        "rms_doppler": round(r["rms_doppler"], 2),
    }


@app.get("/api/radar/data/receiver.json")
async def tar1090_receiver():
    """Serve tar1090 receiver.json for the passive radar site."""
    return _radar_pipeline.generate_receiver_json()


@app.get("/api/radar/data/aircraft.json")
async def tar1090_aircraft():
    """Serve tar1090 aircraft.json with current tracked targets including multi-node."""
    data = _radar_pipeline.generate_aircraft_json()
    # Merge multi-node solver results
    import time as _time
    now = _time.time()
    stale_keys = []
    for key, r in _multinode_tracks.items():
        age_s = now - r.get("timestamp_ms", 0) / 1000
        if age_s > 60:
            stale_keys.append(key)
            continue
        data["aircraft"].append(_multinode_to_aircraft(key, r))
    for k in stale_keys:
        _multinode_tracks.pop(k, None)
    return data


@app.post("/api/radar/detections")
async def ingest_detections(
    request: Request,
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    # ── API key check (if RADAR_API_KEY is configured) ────────────────────────
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    # ── Rate limit by client IP ────────────────────────────────────────────────
    client_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    _check_rate_limit(client_ip)
    """Ingest a detection frame from a passive radar node.

    Expected body: {"timestamp": int, "delay": [...], "doppler": [...], "snr": [...]}
    Or a batch: {"frames": [{...}, ...]}
    """
    frames = body.get("frames", [body]) if "frames" in body else [body]
    processed = 0
    for frame in frames:
        if "timestamp" not in frame:
            continue
        _radar_pipeline.process_frame(frame)
        processed += 1

    # Persist latest aircraft.json to disk
    aircraft_data = _radar_pipeline.generate_aircraft_json()
    with open(os.path.join(_TAR1090_DATA_DIR, "aircraft.json"), "w") as f:
        json.dump(aircraft_data, f)
    await _broadcast_aircraft(aircraft_data)

    return {
        "status": "ok",
        "frames_processed": processed,
        "tracks": len(aircraft_data["aircraft"]),
    }


@app.post("/api/radar/load-file")
async def load_detection_file(body: dict = Body(...)):
    """Load a .detection file from a path on the server.

    Expected body: {"path": "/path/to/file.detection"}
    """
    filepath = body.get("path", "")
    if not filepath or not os.path.isfile(filepath):
        raise HTTPException(status_code=400, detail="File not found")
    if not filepath.endswith(".detection"):
        raise HTTPException(status_code=400, detail="Only .detection files accepted")

    tracks = _radar_pipeline.process_file(filepath)
    aircraft_data = _radar_pipeline.generate_aircraft_json()
    with open(os.path.join(_TAR1090_DATA_DIR, "aircraft.json"), "w") as f:
        json.dump(aircraft_data, f)

    return {
        "status": "ok",
        "tracks": len(tracks),
        "aircraft": aircraft_data["aircraft"],
    }


@app.get("/api/radar/status")
async def radar_status():
    """Return current passive radar pipeline status."""
    return {
        "node_id": _radar_pipeline.node_id,
        "total_tracks": len(_radar_pipeline.tracker.tracks),
        "geolocated_tracks": len(_radar_pipeline.geolocated_tracks),
        "multinode_tracks": len(_multinode_tracks),
        "track_events": len(_radar_pipeline.event_writer.get_events()),
        "external_adsb_cached": len(_external_adsb_cache),
        "config": {
            "rx_lat": _radar_pipeline.config["rx_lat"],
            "rx_lon": _radar_pipeline.config["rx_lon"],
            "tx_lat": _radar_pipeline.config["tx_lat"],
            "tx_lon": _radar_pipeline.config["tx_lon"],
            "FC": _radar_pipeline.config["FC"],
            "Fs": _radar_pipeline.config["Fs"],
        },
    }


@app.get("/api/radar/nodes")
async def radar_nodes():
    """Return status of all connected radar nodes."""
    return {
        "nodes": {
            nid: {
                "status": info.get("status"),
                "config_hash": info.get("config_hash"),
                "last_heartbeat": info.get("last_heartbeat"),
                "peer": info.get("peer"),
                "is_synthetic": info.get("is_synthetic", _is_synthetic_node(nid)),
                "capabilities": info.get("capabilities", {}),
            }
            for nid, info in _connected_nodes.items()
        },
        "connected": sum(1 for n in _connected_nodes.values() if n.get("status") not in ("disconnected",)),
        "total": len(_connected_nodes),
        "synthetic": sum(1 for n in _connected_nodes.values() if n.get("is_synthetic")),
    }


# ── Node Analytics Endpoints ─────────────────────────────────────────────────

@app.get("/api/radar/analytics")
async def radar_analytics():
    """Return analytics summaries for all connected nodes."""
    return {
        "nodes": _node_analytics.get_all_summaries(),
        "cross_node": _node_analytics.get_cross_node_analysis(),
    }


@app.get("/api/radar/analytics/{node_id}")
async def radar_node_analytics(node_id: str):
    """Return analytics for a specific node."""
    summary = _node_analytics.get_node_summary(node_id)
    if summary.keys() == {"node_id"}:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return summary


@app.post("/api/radar/analytics/adsb-report")
async def submit_adsb_report(body: dict = Body(...)):
    """Submit an ADS-B correlation report for trust scoring.

    Expected body: {
        "node_id": "...",
        "predicted_delay": float, "predicted_doppler": float,
        "measured_delay": float, "measured_doppler": float,
        "adsb_hex": "...", "adsb_lat": float, "adsb_lon": float,
        "timestamp_ms": int
    }
    """
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
    _node_analytics.record_adsb_correlation(body["node_id"], entry)
    ts = _node_analytics.trust_scores.get(body["node_id"])
    return {
        "status": "recorded",
        "trust_score": round(ts.score, 4) if ts else 0.0,
        "n_samples": ts.n_samples if ts else 0,
    }


# ── Inter-Node Association Endpoints ─────────────────────────────────────────

@app.get("/api/radar/association/overlaps")
async def association_overlaps():
    """Return overlap zone summaries for all node pairs."""
    return {
        "overlaps": _node_associator.get_overlap_summary(),
        "registered_nodes": list(_node_associator.node_geometries.keys()),
    }


@app.get("/api/radar/association/status")
async def association_status():
    """Return current state of inter-node association engine."""
    return {
        "registered_nodes": len(_node_associator.node_geometries),
        "overlap_zones": len(_node_associator.overlap_zones),
        "pending_frames": list(_node_associator._pending_frames.keys()),
        "overlaps": _node_associator.get_overlap_summary(),
    }


# ── Live Data Streaming ──────────────────────────────────────────────────────

@app.websocket("/ws/aircraft")
async def websocket_aircraft(ws: WebSocket):
    """WebSocket endpoint for live aircraft position updates.

    Clients connect and receive JSON pushes every time the pipeline
    produces new aircraft data.
    """
    await ws.accept()
    _ws_clients.add(ws)
    logging.info("WebSocket client connected (%d total)", len(_ws_clients))
    try:
        # Send current state immediately on connect
        if _latest_aircraft_json.get("aircraft"):
            await ws.send_text(json.dumps(_latest_aircraft_json))
        # Keep connection alive; actual data is pushed via _broadcast_aircraft
        while True:
            # Wait for client pings / disconnects
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)
        logging.info("WebSocket client disconnected (%d remaining)", len(_ws_clients))


@app.get("/api/radar/stream")
async def sse_aircraft_stream():
    """Server-Sent Events (SSE) fallback for live aircraft data.

    Pushes the latest aircraft.json every 2 seconds as SSE events.
    Useful for clients that cannot use WebSocket.
    """
    async def _generate():
        last_hash = ""
        while True:
            data = _latest_aircraft_json
            # Only push when data has changed
            current_hash = str(data.get("now", 0))
            if current_hash != last_hash:
                yield f"data: {json.dumps(data)}\n\n"
                last_hash = current_hash
            await asyncio.sleep(2)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Public Data Archive API ───────────────────────────────────────────────────

@app.get("/api/data/archive")
async def list_archive(
    date: str = Query(None, description="Date prefix, e.g. 2025/06/21 or 2025/06"),
    node_id: str = Query(None, description="Filter by node ID"),
):
    """List archived detection files with optional date and node filters."""
    files = list_archived_files(date_prefix=date, node_id=node_id)
    return {"files": files, "count": len(files)}


@app.get("/api/data/archive/{key:path}")
async def download_archive_file(key: str):
    """Download a specific archived detection file by its key."""
    data = read_archived_file(key)
    if data is None:
        raise HTTPException(status_code=404, detail="Archive file not found")
    return data
