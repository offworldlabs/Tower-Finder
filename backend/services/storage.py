"""
Local archive storage for detection data.

Files are written to coverage_data/archive/ relative to the backend directory.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCAL_ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data", "archive")


# ---------- Helpers ---------------------------------------------------------

def _ensure_local_dir():
    os.makedirs(_LOCAL_ARCHIVE_DIR, exist_ok=True)


# ---------- Public API ------------------------------------------------------

def archive_detections(node_id: str, detections: list[dict], *, tag: str = "detections") -> str:
    """Archive a batch of detections to local filesystem.

    Returns the archive key (relative path like "2025/06/21/node01/detections_143022.json").
    """
    _ensure_local_dir()

    ts = datetime.now(timezone.utc)
    prefix = ts.strftime("%Y/%m/%d")
    filename = f"{tag}_{ts.strftime('%H%M%S')}.json"
    key = f"{prefix}/{node_id}/{filename}"

    payload = json.dumps(
        {"node_id": node_id, "timestamp": ts.isoformat(), "count": len(detections), "detections": detections},
        default=str,
    )

    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "w") as f:
        f.write(payload)

    return key


def list_archived_files(
    date_prefix: str | None = None,
    node_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort_desc: bool = True,
) -> dict:
    """List archived detection files filtered by optional date prefix and node_id.

    Args:
        date_prefix: e.g. "2025/06/21" or "2025/06"
        node_id: filter to a specific node
        limit: max number of results to return (default 100, max 500)
        offset: pagination offset
        sort_desc: if True, newest files first

    Returns dict of {files: [...], count: N, total: N}.
    """
    _ensure_local_dir()
    base = Path(_LOCAL_ARCHIVE_DIR)
    results = []

    search_dir = base
    if date_prefix:
        search_dir = base / date_prefix

    if not search_dir.exists():
        return {"files": [], "count": 0, "total": 0}

    for p in search_dir.rglob("*.json"):
        rel = p.relative_to(base)
        parts = rel.parts
        # key structure: YYYY/MM/DD/node_id/filename.json
        file_node_id = parts[-2] if len(parts) >= 2 else ""
        if node_id and file_node_id != node_id:
            continue
        stat = p.stat()
        results.append({
            "key": str(rel),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    results.sort(key=lambda x: x["modified"], reverse=sort_desc)
    total = len(results)
    limit = min(limit, 500)  # hard cap
    page = results[offset: offset + limit]
    return {"files": page, "count": len(page), "total": total}


def read_archived_file(key: str) -> dict | None:
    """Read an archived JSON file by key. Returns parsed dict or None."""
    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    if not os.path.isfile(local_path):
        return None
    # Prevent path traversal
    real_base = os.path.realpath(_LOCAL_ARCHIVE_DIR) + os.sep
    real_path = os.path.realpath(local_path)
    if not real_path.startswith(real_base):
        return None
    with open(local_path, "r") as f:
        return json.load(f)
