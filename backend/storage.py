"""
Backblaze B2 storage helper for archiving detection data.

Env vars required:
    B2_KEY_ID        – Application Key ID
    B2_APP_KEY       – Application Key
    B2_BUCKET_NAME   – Target bucket name

If credentials are missing, the module falls back to local-only storage
under coverage_data/ without raising errors.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCAL_ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "coverage_data", "archive")

# ---------- B2 SDK bootstrap (optional dependency) --------------------------

_b2_api = None
_b2_bucket = None


def _init_b2():
    """Lazily initialize Backblaze B2 API client from env vars."""
    global _b2_api, _b2_bucket

    if _b2_api is not None:
        return _b2_api is not False  # False = previously failed

    key_id = os.getenv("B2_KEY_ID", "")
    app_key = os.getenv("B2_APP_KEY", "")
    bucket_name = os.getenv("B2_BUCKET_NAME", "")

    if not all([key_id, app_key, bucket_name]):
        logger.info("B2 credentials not configured – using local storage only")
        _b2_api = False
        return False

    try:
        from b2sdk.v2 import B2Api, InMemoryAccountInfo

        info = InMemoryAccountInfo()
        api = B2Api(info)
        api.authorize_account("production", key_id, app_key)
        _b2_bucket = api.get_bucket_by_name(bucket_name)
        _b2_api = api
        logger.info("Connected to Backblaze B2 bucket '%s'", bucket_name)
        return True
    except Exception as exc:
        logger.warning("B2 init failed (%s) – falling back to local storage", exc)
        _b2_api = False
        return False


# ---------- Local archive helpers -------------------------------------------

def _ensure_local_dir():
    os.makedirs(_LOCAL_ARCHIVE_DIR, exist_ok=True)


def _date_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y/%m/%d")


# ---------- Public API ------------------------------------------------------

def archive_detections(node_id: str, detections: list[dict], *, tag: str = "detections") -> str:
    """Archive a batch of detections to local filesystem and (optionally) B2.

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

    # Local write
    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "w") as f:
        f.write(payload)

    # B2 upload (fire-and-forget style)
    if _init_b2() and _b2_bucket is not None:
        try:
            _b2_bucket.upload_bytes(payload.encode(), f"archive/{key}")
        except Exception as exc:
            logger.warning("B2 upload failed for %s: %s", key, exc)

    return key


def list_archived_files(date_prefix: str | None = None, node_id: str | None = None) -> list[dict]:
    """List archived detection files filtered by optional date prefix and node_id.

    Args:
        date_prefix: e.g. "2025/06/21" or "2025/06"
        node_id: filter to a specific node

    Returns list of {key, size_bytes, modified} dicts.
    """
    _ensure_local_dir()
    base = Path(_LOCAL_ARCHIVE_DIR)
    results = []

    search_dir = base
    if date_prefix:
        search_dir = base / date_prefix

    if not search_dir.exists():
        return results

    for p in sorted(search_dir.rglob("*.json")):
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

    return results


def read_archived_file(key: str) -> dict | None:
    """Read an archived JSON file by key. Returns parsed dict or None."""
    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    if not os.path.isfile(local_path):
        return None
    # Prevent path traversal
    real_base = os.path.realpath(_LOCAL_ARCHIVE_DIR)
    real_path = os.path.realpath(local_path)
    if not real_path.startswith(real_base + os.sep):
        return None
    with open(local_path, "r") as f:
        return json.load(f)
