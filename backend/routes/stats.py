"""Tower usage statistics endpoints."""

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()

_STATS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tower_stats.json")


def _load_stats() -> dict:
    if os.path.exists(_STATS_PATH):
        with open(_STATS_PATH) as f:
            return json.load(f)
    return {"selections": []}


def _save_stats(stats: dict):
    with open(_STATS_PATH, "w") as f:
        json.dump(stats, f, indent=2)


@router.post("/api/stats/tower-selection")
async def record_tower_selection(body: dict = Body(...)):
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


@router.get("/api/stats/summary")
async def tower_stats_summary():
    stats = _load_stats()
    selections = stats.get("selections", [])
    tower_usage: dict[str, int] = {}
    for s in selections:
        key = f"{s.get('tower_callsign', '?')}@{s.get('tower_frequency_mhz', '?')}"
        tower_usage[key] = tower_usage.get(key, 0) + 1
    ranked = sorted(tower_usage.items(), key=lambda x: -x[1])
    return {
        "total_selections": len(selections),
        "unique_towers": len(tower_usage),
        "tower_usage": [{"tower": k, "selections": v} for k, v in ranked],
    }
