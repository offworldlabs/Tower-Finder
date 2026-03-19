"""Tower-finding, config, elevation, and health endpoints."""

import json
import logging
import os

import httpx
from fastapi import APIRouter, Query, HTTPException, Body

from services.tower_ranking import (
    process_and_rank, reload_config, _CONFIG_PATH,
    DEFAULT_RADIUS_KM, DEFAULT_LIMIT, parse_user_frequencies,
)
from clients.maprad import fetch_broadcast_systems
from clients.fcc import fetch_fcc_broadcast_systems

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
    except Exception as exc:
        logging.exception("Tower data fetch failed")
        raise HTTPException(status_code=502, detail=f"Upstream API error: {exc}")

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
    return {"status": "ok"}


@router.get("/api/config")
async def get_config():
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


@router.put("/api/config")
async def update_config(body: dict):
    with open(_CONFIG_PATH, "w") as f:
        json.dump(body, f, indent=2)
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
