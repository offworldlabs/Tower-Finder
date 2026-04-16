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
    When no date_prefix is given, traverses directories in reverse-chronological
    order and stops early once enough files are collected — avoids full rglob scan
    over potentially hundreds of thousands of files.
    """
    _ensure_local_dir()
    base = Path(_LOCAL_ARCHIVE_DIR)
    limit = min(limit, 500)  # hard cap

    if date_prefix:
        # Bounded scope — safe to rglob a single date subtree
        search_dir = base / date_prefix
        if not search_dir.exists():
            return {"files": [], "count": 0, "total": 0}

        results = []
        for p in search_dir.rglob("*.json"):
            rel = p.relative_to(base)
            parts = rel.parts
            file_node_id = parts[-2] if len(parts) >= 2 else ""
            if node_id and file_node_id != node_id:
                continue
            st = p.stat()
            results.append({
                "key": str(rel),
                "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })

        results.sort(key=lambda x: x["modified"], reverse=sort_desc)
        total = len(results)
        page = results[offset: offset + limit]
        return {"files": page, "count": len(page), "total": total}

    # No date_prefix — traverse in reverse-chronological order and exit early.
    # Archive structure: base/YYYY/MM/DD/node_id/filename.json
    if not base.exists():
        return {"files": [], "count": 0, "total": 0}

    # How many files we need to serve the requested page + to estimate total.
    needed = offset + limit
    # Scan at most this many files to estimate the total count.
    MAX_SCAN = 5000

    def _sorted_subdirs(path: Path, reverse: bool) -> list[Path]:
        try:
            return sorted(
                (d for d in path.iterdir() if d.is_dir()),
                key=lambda d: d.name,
                reverse=reverse,
            )
        except OSError:
            return []

    def _iter_files_ordered():
        """Yield Path objects in approximate (reverse-)chronological order."""
        for year_dir in _sorted_subdirs(base, reverse=sort_desc):
            for month_dir in _sorted_subdirs(year_dir, reverse=sort_desc):
                for day_dir in _sorted_subdirs(month_dir, reverse=sort_desc):
                    for ndir in _sorted_subdirs(day_dir, reverse=False):
                        if node_id and ndir.name != node_id:
                            continue
                        try:
                            files = sorted(
                                ndir.glob("*.json"),
                                key=lambda f: f.name,
                                reverse=sort_desc,
                            )
                        except OSError:
                            continue
                        yield from files

    collected: list[dict] = []
    total_scanned = 0
    for p in _iter_files_ordered():
        total_scanned += 1
        if total_scanned <= needed:
            # Only stat the files we actually need for the page
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(base)
            collected.append({
                "key": str(rel),
                "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
        if total_scanned >= MAX_SCAN:
            break

    page = collected[offset: offset + limit]
    return {
        "files": page,
        "count": len(page),
        "total": total_scanned,
        "truncated": total_scanned >= MAX_SCAN,
    }


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
