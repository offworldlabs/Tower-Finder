"""Admin-only API routes — user management, events, config, leaderboard."""

import asyncio
import concurrent.futures
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

# Dedicated executor for blocking admin operations so they never compete with
# the default thread pool used by frame processors.
_admin_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="admin-io")

import orjson
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from config.constants import (
    CONFIG_LIVE_CACHE_TTL_S,
    EVENT_LOG_MAX,
    NODE_HEALTH_CHECK_INTERVAL_S,
    NODE_OFFLINE_THRESHOLD_S,
)
from core import state
from core.auth import get_all_users, get_current_user, require_admin, update_user_role
from core.task_registry import TASK_EXPECTED_INTERVAL_S

logger = logging.getLogger(__name__)


def _get_stale_tasks() -> list[str]:
    now = time.time()
    stale = []
    for task_name, expected_s in TASK_EXPECTED_INTERVAL_S.items():
        last = state.task_last_success.get(task_name)
        if last is None:
            continue
        if (now - last) > expected_s * 2:
            stale.append(task_name)
    return stale
router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Persistent event log ─────────────────────────────────────────────────────

_EVENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "events.json"
_events: deque = deque(maxlen=EVENT_LOG_MAX)


def _load_events():
    """Load events from disk on startup."""
    if _EVENTS_FILE.exists():
        try:
            data = json.loads(_EVENTS_FILE.read_text())
            for ev in data:
                _events.append(ev)
        except Exception:
            pass


def _save_events():
    """Persist events to disk."""
    _EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EVENTS_FILE.write_text(json.dumps(list(_events), default=str))


_load_events()


def log_event(category: str, message: str, severity: str = "info", meta: dict | None = None):
    _events.appendleft({
        "ts": time.time(),
        "category": category,
        "message": message,
        "severity": severity,
        "meta": meta or {},
    })
    # Persist every 10 events to avoid excessive I/O
    if len(_events) % 10 == 0:
        _save_events()


# ── Node health monitoring (auto-detect offline nodes) ───────────────────────

_OFFLINE_THRESHOLD_S = NODE_OFFLINE_THRESHOLD_S
_last_health_check = 0.0


def check_node_health():
    """Called periodically from background task to detect offline nodes."""
    global _last_health_check
    now = time.time()
    if now - _last_health_check < NODE_HEALTH_CHECK_INTERVAL_S:
        return
    _last_health_check = now

    with state.connected_nodes_lock:
        snapshot = list(state.connected_nodes.items())
    for node_id, info in snapshot:
        hb = info.get("last_heartbeat")
        if not hb:
            continue
        try:
            from datetime import datetime, timezone
            hb_time = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - hb_time).total_seconds()
        except Exception:
            continue
        if age_s > _OFFLINE_THRESHOLD_S and info.get("status") != "disconnected":
            with state.connected_nodes_lock:
                info["status"] = "disconnected"
            log_event(
                "node",
                f"Node {node_id} went offline (no heartbeat for {int(age_s)}s)",
                "warning",
                {"node_id": node_id, "age_s": int(age_s)},
            )


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(_admin=Depends(require_admin)):
    return get_all_users()


class RoleUpdate(BaseModel):
    role: str


@router.put("/users/{user_id}/role")
async def set_user_role(user_id: str, body: RoleUpdate, _admin=Depends(require_admin)):
    user = update_user_role(user_id, body.role)
    if not user:
        raise HTTPException(404, "User not found or invalid role")
    log_event("user", f"Role changed to {body.role} for {user['email']}", "warning")
    return user


# ── Events ────────────────────────────────────────────────────────────────────

@router.get("/events")
async def list_events(limit: int = 200, _admin=Depends(require_admin)):
    return list(_events)[:limit]


# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "data" / "config_history"
_BACKEND_DIR = Path(__file__).resolve().parent.parent


@router.get("/config/nodes")
async def get_node_config(_admin=Depends(require_admin)):
    global _nodes_config_cache
    fp = _BACKEND_DIR / "nodes_config.json"
    if fp.exists():
        return Response(content=fp.read_bytes(), media_type="application/json")
    # Live fallback with TTL cache — iterating 1000 nodes is O(n)
    now = time.time()
    if _nodes_config_cache is not None and now - _nodes_config_cache[0] < _CONFIG_LIVE_CACHE_TTL:
        return Response(content=_nodes_config_cache[1], media_type="application/json")
    nodes_cfg = {}
    with state.connected_nodes_lock:
        _nodes_items = list(state.connected_nodes.items())
    for nid, info in _nodes_items:
        cfg = info.get("config", {})
        nodes_cfg[nid] = {
            "name": cfg.get("name", nid),
            "frequency": cfg.get("FC", cfg.get("frequency")),
            "rx_lat": cfg.get("rx_lat"),
            "rx_lon": cfg.get("rx_lon"),
            "tx_lat": cfg.get("tx_lat"),
            "tx_lon": cfg.get("tx_lon"),
            "status": info.get("status"),
        }
    result_bytes = orjson.dumps({"_source": "live", "nodes": nodes_cfg, "total": len(nodes_cfg)})
    _nodes_config_cache = (now, result_bytes)
    return Response(content=result_bytes, media_type="application/json")


@router.get("/config/towers")
async def get_tower_config(_admin=Depends(require_admin)):
    global _towers_config_cache
    fp = _BACKEND_DIR / "tower_config.json"
    if fp.exists():
        return Response(content=fp.read_bytes(), media_type="application/json")
    # Live fallback with TTL cache
    now = time.time()
    if _towers_config_cache is not None and now - _towers_config_cache[0] < _CONFIG_LIVE_CACHE_TTL:
        return Response(content=_towers_config_cache[1], media_type="application/json")
    towers = {}
    with state.connected_nodes_lock:
        _tower_items = list(state.connected_nodes.items())
    for nid, info in _tower_items:
        cfg = info.get("config", {})
        tx_lat = cfg.get("tx_lat")
        tx_lon = cfg.get("tx_lon")
        if tx_lat and tx_lon:
            key = f"{tx_lat:.4f},{tx_lon:.4f}"
            if key not in towers:
                towers[key] = {
                    "lat": tx_lat,
                    "lon": tx_lon,
                    "frequency": cfg.get("FC", cfg.get("frequency")),
                    "nodes_using": [],
                }
            towers[key]["nodes_using"].append(nid)
    result_bytes = orjson.dumps({"_source": "live", "towers": towers, "total": len(towers)})
    _towers_config_cache = (now, result_bytes)
    return Response(content=result_bytes, media_type="application/json")


class ConfigUpdate(BaseModel):
    config: dict


@router.put("/config/nodes")
async def update_node_config(body: ConfigUpdate, _admin=Depends(require_admin)):
    global _nodes_config_cache
    _nodes_config_cache = None  # invalidate live cache
    fp = _BACKEND_DIR / "nodes_config.json"
    # Save version history
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    if fp.exists():
        history_fp = _CONFIG_DIR / f"nodes_{ts}.json"
        history_fp.write_text(fp.read_text())
    fp.write_text(json.dumps(body.config, indent=2))
    log_event("config", "Node config updated", "info")
    return {"status": "ok", "saved_at": ts}


@router.put("/config/towers")
async def update_tower_config(body: ConfigUpdate, _admin=Depends(require_admin)):
    global _towers_config_cache
    _towers_config_cache = None  # invalidate live cache
    fp = _BACKEND_DIR / "tower_config.json"
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    if fp.exists():
        history_fp = _CONFIG_DIR / f"towers_{ts}.json"
        history_fp.write_text(fp.read_text())
    fp.write_text(json.dumps(body.config, indent=2))
    log_event("config", "Tower config updated", "info")
    return {"status": "ok", "saved_at": ts}


@router.get("/config/history")
async def config_history(_admin=Depends(require_admin)):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(_CONFIG_DIR.glob("*.json"), reverse=True)
    result = []
    for f in files[:50]:
        name = f.stem  # e.g. "nodes_1711234567"
        parts = name.rsplit("_", 1)
        config_type = parts[0] if len(parts) > 1 else name
        ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        result.append({"filename": f.name, "type": config_type, "timestamp": ts, "size": f.stat().st_size})
    return result


# ── Storage stats ─────────────────────────────────────────────────────────────
# Results are pre-computed by storage_refresh_task (services/tasks/storage_refresh.py)
# and stored in state.latest_storage_bytes. The endpoint just returns those bytes.

# TTL cache for live-generated node/tower config (active when JSON files absent)
_nodes_config_cache: tuple | None = None
_towers_config_cache: tuple | None = None
_CONFIG_LIVE_CACHE_TTL = CONFIG_LIVE_CACHE_TTL_S


@router.get("/storage")
async def storage_stats(_admin=Depends(require_admin)):
    if state.latest_storage_bytes == b'{}':
        # Background task hasn't completed its first scan yet (startup in progress).
        # Return 202 so the frontend knows to retry rather than treating it as an error.
        return Response(content=b'{"status":"initializing"}', status_code=202,
                        media_type="application/json")
    return Response(content=state.latest_storage_bytes, media_type="application/json")


# ── Leaderboard ──────────────────────────────────────────────────────────────

@router.get("/leaderboard")
async def leaderboard(_user=Depends(get_current_user)):
    """Public leaderboard — rankings by detections, uptime, trust."""
    import orjson
    # Use the pre-computed analytics snapshot (refreshed every 30 s by the
    # background task) to avoid holding the analytics lock in this handler.
    raw = state.latest_analytics_bytes
    summaries: dict = {}
    if raw and raw != b'{}':
        try:
            summaries = orjson.loads(raw).get("nodes", {})
        except Exception:
            pass
    # Fall back to live computation only if the snapshot is empty
    if not summaries:
        loop = asyncio.get_running_loop()
        summaries = await loop.run_in_executor(_admin_executor, state.node_analytics.get_all_summaries)

    entries = []
    for node_id, s in summaries.items():
        m = s.get("metrics", {})
        t = s.get("trust", {})
        r = s.get("reputation", {})
        miss = state.latest_missed_detections.get(node_id, {})
        entries.append({
            "node_id": node_id,
            "name": state.connected_nodes.get(node_id, {}).get("config", {}).get("name", node_id),
            "detections": m.get("total_detections", 0),
            "frames": m.get("total_frames", 0),
            "tracks": m.get("total_tracks", 0),
            "uptime_s": m.get("uptime_s", 0),
            "avg_snr": m.get("avg_snr", 0),
            "trust_score": t.get("trust_score", 0),
            "reputation": r.get("reputation", 0),
            "online": state.connected_nodes.get(node_id, {}).get("status") not in ("disconnected", None),
            "in_range": miss.get("in_range", 0),
            "detected_in_range": miss.get("detected", 0),
            "missed": miss.get("missed", 0),
            "miss_rate": miss.get("miss_rate", 0.0),
        })
    # Sort by detections descending
    entries.sort(key=lambda e: e["detections"], reverse=True)
    # Add rank
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    return {"leaderboard": entries, "total": len(entries)}


# ── User alerts (public, non-admin) ─────────────────────────────────────────

@router.get("/alerts")
async def user_alerts(_user=Depends(get_current_user)):
    """Return recent events visible to logged-in users."""
    visible = [
        e for e in _events
        if e.get("severity") in ("warning", "error", "critical")
        or e.get("category") in ("node", "config", "system")
    ]
    return visible[:100]


@router.get("/metrics")
async def system_metrics(_user=Depends(require_admin)):
    """Operational metrics: task health, error counts, queue depths."""
    import resource
    import shutil

    rusage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is in KB on Linux, bytes on macOS — normalise to MB
    import sys
    rss_mb = rusage.ru_maxrss / 1024 if sys.platform == "linux" else rusage.ru_maxrss / (1024 * 1024)

    disk = shutil.disk_usage(state.COVERAGE_STORAGE_DIR)

    return {
        "task_last_success": dict(state.task_last_success),
        "task_error_counts": dict(state.task_error_counts),
        "frame_queue_depth": state.frame_queue.qsize(),
        "frame_queue_max": state.frame_queue.maxsize,
        "frames_dropped": state.frames_dropped,
        "frames_processed": state.frames_processed,
        "solver_successes": state.solver_successes,
        "solver_failures": state.solver_failures,
        "solver_queue_depth": state.solver_queue.qsize(),
        "solver_queue_drops": state.solver_queue_drops,
        "solver_last_latency_s": round(state.solver_last_latency_s, 3),
        "solver_avg_latency_s": round(
            state.solver_total_latency_s / max(state.solver_total_solved, 1), 2
        ),
        "solver_queue_pct": round(
            state.solver_queue.qsize() / max(state.solver_queue.maxsize, 1) * 100, 1
        ),
        "connected_nodes": len([n for n in list(state.connected_nodes.values()) if n.get("status") == "active"]),
        "peak_connected_nodes": state.peak_connected_nodes,
        "active_geo_aircraft": len(state.active_geo_aircraft),
        "multinode_tracks": len(state.multinode_tracks),
        "adsb_aircraft": len(state.adsb_aircraft),
        "ws_clients": len(state.ws_clients),
        "ws_live_clients": len(state.ws_live_clients),
        "stale_tasks": _get_stale_tasks(),
        "process_rss_mb": round(rss_mb, 1),
        "load_avg": list(os.getloadavg()),
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "disk_used_gb": round(disk.used / (1024**3), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
    }


@router.post("/coverage/dump")
async def coverage_dump(_user=Depends(require_admin)):
    """Flush runtime coverage data to disk and generate an HTML report.

    Only meaningful when the server was started with ``COVERAGE_ENABLED=1``.
    Collection continues after the dump — no restart required.
    """
    import services.runtime_coverage as _rc
    html_dir = await asyncio.get_event_loop().run_in_executor(
        _admin_executor, _rc.save
    )
    if html_dir is None:
        return {"status": "disabled", "detail": "COVERAGE_ENABLED is not set"}
    return {"status": "ok", "html_report": html_dir}
