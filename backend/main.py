import json
import os
import logging
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from maprad_client import fetch_broadcast_systems
from calculations import (
    process_and_rank, reload_config, _CONFIG_PATH,
    DEFAULT_RADIUS_KM, DEFAULT_LIMIT,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Tower Finder API")

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
):
    """
    Return nearby broadcast towers ranked for passive-radar suitability.
    """
    if not API_KEY:
        raise HTTPException(status_code=500, detail="MAPRAD_API_KEY not configured")

    source = source.lower()
    if source == "auto":
        source = _detect_source(lat, lon)
    if source not in ("us", "au", "ca"):
        raise HTTPException(status_code=400, detail="Invalid source. Use: us, au, ca, auto")

    # Use config defaults if caller didn't specify
    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT

    try:
        raw = await fetch_broadcast_systems(
            API_KEY, lat, lon, radius_km=effective_radius, source=source,
        )
    except Exception as exc:
        logging.exception("Maprad API call failed")
        raise HTTPException(status_code=502, detail=f"Upstream API error: {exc}")

    # Auto-resolve altitude if not provided
    resolved_altitude = altitude
    if altitude == 0:
        elev = await _lookup_elevation(lat, lon)
        if elev is not None:
            resolved_altitude = elev

    towers = process_and_rank(raw, lat, lon, limit=effective_limit)

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
