import asyncio
import json
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from maprad_client import fetch_broadcast_systems
from fcc_client import fetch_fcc_broadcast_systems
from calculations import (
    process_and_rank, reload_config, _CONFIG_PATH,
    DEFAULT_RADIUS_KM, DEFAULT_LIMIT, parse_user_frequencies,
)
from passive_radar import PassiveRadarPipeline, DEFAULT_NODE_CONFIG

load_dotenv()
logging.basicConfig(level=logging.INFO)

TCP_PORT = int(os.getenv("RADAR_TCP_PORT", "3012"))


async def _handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single TCP connection from a synthetic or real radar node.

    Reads newline-delimited JSON detection frames and feeds them to the
    radar pipeline. Each line must be a valid JSON object.
    """
    peer = writer.get_extra_info("peername")
    logging.info("Radar TCP: new connection from %s", peer)
    buf = b""
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
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    logging.debug("Radar TCP: malformed JSON from %s", peer)
                    continue
                if "timestamp" not in frame:
                    continue
                _radar_pipeline.process_frame(frame)
                aircraft_data = _radar_pipeline.generate_aircraft_json()
                aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                with open(aircraft_path, "w") as f:
                    json.dump(aircraft_data, f)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        logging.info("Radar TCP: connection closed from %s", peer)
        writer.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    server = await asyncio.start_server(_handle_tcp_client, "0.0.0.0", TCP_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logging.info("Radar TCP server listening on %s", addrs)
    async with server:
        server_task = asyncio.create_task(server.serve_forever())
        yield
        server_task.cancel()


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
    limit: int = Query(0, ge=0, le=100, description="Max towers to return (0 = use config default)"),
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

    towers = process_and_rank(raw, lat, lon, limit=effective_limit, user_frequencies=user_freqs)

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


@app.get("/api/radar/data/receiver.json")
async def tar1090_receiver():
    """Serve tar1090 receiver.json for the passive radar site."""
    return _radar_pipeline.generate_receiver_json()


@app.get("/api/radar/data/aircraft.json")
async def tar1090_aircraft():
    """Serve tar1090 aircraft.json with current tracked targets."""
    return _radar_pipeline.generate_aircraft_json()


@app.post("/api/radar/detections")
async def ingest_detections(body: dict = Body(...)):
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
        "track_events": len(_radar_pipeline.event_writer.get_events()),
        "config": {
            "rx_lat": _radar_pipeline.config["rx_lat"],
            "rx_lon": _radar_pipeline.config["rx_lon"],
            "tx_lat": _radar_pipeline.config["tx_lat"],
            "tx_lon": _radar_pipeline.config["tx_lon"],
            "FC": _radar_pipeline.config["FC"],
            "Fs": _radar_pipeline.config["Fs"],
        },
    }
