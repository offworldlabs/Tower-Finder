"""Archive storage stats pre-computation — runs every 5 minutes.

The first scan runs immediately at startup so the storage endpoint always has
data to return. Subsequent scans run every STORAGE_CACHE_TTL_S seconds.
Results are stored as pre-serialised bytes in state.latest_storage_bytes so
the /api/admin/storage endpoint returns instantly with no blocking work.
"""

import asyncio
import concurrent.futures
import logging
import shutil
import subprocess
import time
from pathlib import Path

import orjson

from config.constants import STORAGE_CACHE_TTL_S
from core import state

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

# Reuse the admin executor (2 threads) — same class of blocking I/O.
# Import lazily to avoid circular imports with routes.admin.
def _get_admin_executor() -> concurrent.futures.ThreadPoolExecutor:
    from routes.admin import _admin_executor
    return _admin_executor


def _scan_archive_dir(archive_dir: Path) -> tuple[int, int, dict]:
    """Blocking archive scan using subprocess du/find — avoids O(N) stat() calls.

    With 200k+ files, Python rglob+stat takes 120 s on Docker overlay FS.
    A single 'du --max-depth=4' call lets the kernel do the tree walk in C
    and returns per-node-day byte totals in ~1 s regardless of file count.
    """
    if not archive_dir.exists():
        return 0, 0, {}

    total_bytes = 0
    total_files = 0
    per_node: dict[str, dict] = {}

    # ── Total + per-node bytes via du ────────────────────────────────────────
    # archive structure: archive / YYYY / MM / DD / NODE_ID / *.parquet
    # --max-depth=4 prints one line per node-day dir; last line is archive total.
    try:
        r = subprocess.run(
            ["du", "-b", "--max-depth=4", str(archive_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "\t" not in line:
                    continue
                sz_str, path = line.split("\t", 1)
                sz = int(sz_str)
                try:
                    rel = Path(path).relative_to(archive_dir)
                except ValueError:
                    total_bytes = sz  # archive root line
                    continue
                parts = rel.parts
                if len(parts) == 0:
                    total_bytes = sz
                elif len(parts) == 4:  # YYYY/MM/DD/NODE_ID
                    node_id = parts[3]
                    e = per_node.setdefault(node_id, {"files": 0, "bytes": 0})
                    e["bytes"] += sz
    except Exception:
        # Fallback: du -sb for total only
        try:
            r = subprocess.run(
                ["du", "-sb", str(archive_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and r.stdout:
                total_bytes = int(r.stdout.split()[0])
        except Exception:
            pass

    if total_bytes == 0:
        total_bytes = sum(e["bytes"] for e in per_node.values())

    # ── File count + per-node counts via find ────────────────────────────────
    try:
        r = subprocess.run(
            ["find", str(archive_dir), "-type", "f", "-printf", "x"],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0:
            total_files = len(r.stdout)
            r2 = subprocess.run(
                ["find", str(archive_dir), "-mindepth", "5", "-maxdepth", "5",
                 "-type", "f", "-printf", "%P\n"],
                capture_output=True, text=True, timeout=120,
            )
            if r2.returncode == 0:
                for rel_path in r2.stdout.splitlines():
                    if not rel_path:
                        continue
                    parts = Path(rel_path).parts
                    node_id = parts[3] if len(parts) > 3 else "unknown"
                    per_node.setdefault(node_id, {"files": 0, "bytes": 0})["files"] += 1
    except Exception:
        pass

    return total_files, total_bytes, per_node


def _build_storage_result(archive_dir: Path) -> bytes:
    """Run scan and build the JSON bytes to store in state.latest_storage_bytes."""
    now = time.time()
    total_files, total_bytes, per_node = _scan_archive_dir(archive_dir)

    # Disk usage (shutil.disk_usage is fast — single stat call)
    disk_path = str(archive_dir) if archive_dir.exists() else str(_BACKEND_DIR)
    try:
        du = shutil.disk_usage(disk_path)
        disk_total, disk_used, disk_free = du.total, du.used, du.free
    except Exception:
        disk_total = disk_used = disk_free = 0

    # Estimate per-node write rate (bytes/day) from archive size and node uptime
    per_node_rate: dict[str, float] = {}
    for node_id, pn in per_node.items():
        node_bytes = pn.get("bytes", 0)
        if node_bytes <= 0:
            continue
        node_info = state.connected_nodes.get(node_id, {})
        first_seen = node_info.get("first_seen_ts")
        if not first_seen:
            continue
        age_days = max((now - first_seen) / 86400, 0.01)
        per_node_rate[node_id] = round(node_bytes / age_days, 0)

    total_rate = sum(per_node_rate.values())
    days_until_full = round(disk_free / total_rate, 1) if total_rate > 0 and disk_free > 0 else 0.0

    result = {
        "archive_files": total_files,
        "archive_bytes": total_bytes,
        "archive_mb": round(total_bytes / (1024 * 1024), 2),
        "per_node": per_node,
        "disk": {
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "free_bytes": disk_free,
            "total_gb": round(disk_total / (1024 ** 3), 2),
            "used_gb": round(disk_used / (1024 ** 3), 2),
            "free_gb": round(disk_free / (1024 ** 3), 2),
            "used_pct": round(disk_used / max(disk_total, 1) * 100, 1),
        },
        "write_rate": {
            "total_bytes_per_day": round(total_rate, 0),
            "total_mb_per_day": round(total_rate / (1024 * 1024), 2),
            "per_node_bytes_per_day": per_node_rate,
            "days_until_full": days_until_full,
        },
    }
    return orjson.dumps(result)


async def storage_refresh_task():
    """Pre-compute storage stats every STORAGE_CACHE_TTL_S and store in state."""
    archive_dir = _BACKEND_DIR / "coverage_data" / "archive"
    loop = asyncio.get_running_loop()
    # Run immediately at startup so the endpoint has data from the first request.
    while True:
        try:
            executor = _get_admin_executor()
            result_bytes = await loop.run_in_executor(
                executor, _build_storage_result, archive_dir
            )
            state.latest_storage_bytes = result_bytes
            state.task_last_success["storage_refresh"] = time.time()
        except Exception:
            state.task_error_counts["storage_refresh"] += 1
            logger.exception("Storage stats refresh failed")
        await asyncio.sleep(STORAGE_CACHE_TTL_S)
