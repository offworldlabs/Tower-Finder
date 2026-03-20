"""Admin-only API routes — user management, events, config."""

import json
import logging
import os
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import require_admin, get_all_users, update_user_role

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── In-memory event log ──────────────────────────────────────────────────────

_events: deque = deque(maxlen=500)


def log_event(category: str, message: str, severity: str = "info", meta: dict | None = None):
    _events.appendleft({
        "ts": time.time(),
        "category": category,
        "message": message,
        "severity": severity,
        "meta": meta or {},
    })


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
async def list_events(limit: int = 100, _admin=Depends(require_admin)):
    return list(_events)[:limit]


# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@router.get("/config/nodes")
async def get_node_config(_admin=Depends(require_admin)):
    """Return contents of nodes_config.json."""
    fp = Path(__file__).resolve().parent.parent / "nodes_config.json"
    if not fp.exists():
        return {}
    return json.loads(fp.read_text())


@router.get("/config/towers")
async def get_tower_config(_admin=Depends(require_admin)):
    fp = Path(__file__).resolve().parent.parent / "tower_config.json"
    if not fp.exists():
        return {}
    return json.loads(fp.read_text())


# ── Storage stats ─────────────────────────────────────────────────────────────

@router.get("/storage")
async def storage_stats(_admin=Depends(require_admin)):
    """Basic storage usage stats."""
    archive_dir = Path(__file__).resolve().parent.parent / "coverage_data" / "archive"
    total_files = 0
    total_bytes = 0
    if archive_dir.exists():
        for f in archive_dir.rglob("*"):
            if f.is_file():
                total_files += 1
                total_bytes += f.stat().st_size
    return {
        "archive_files": total_files,
        "archive_bytes": total_bytes,
        "archive_mb": round(total_bytes / (1024 * 1024), 2),
    }
