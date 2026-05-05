"""
Local archive storage for detection data.

Files are written to coverage_data/archive/ relative to the backend directory.
New writes use Parquet with Hive-style partitioning; legacy JSON files are
still readable transparently via read_archived_file / list_archived_files.
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


def _partition_value(dirname: str) -> str:
    """Strip a Hive-style ``key=value`` prefix and return the bare value.

    For legacy directory names without ``=`` (e.g. ``2025``, ``node-A``),
    returns the name unchanged. Lets the listing code handle both layouts.
    """
    return dirname.split("=", 1)[-1]


# ---------- Public API ------------------------------------------------------

def archive_detections(node_id: str, detections: list[dict], *, tag: str = "detections") -> str | None:
    """Archive a batch of detection FRAMES to local filesystem as Parquet.

    Returns a Hive-style relative key, e.g.
        "year=2025/month=06/day=21/node_id=node01/part-143022.parquet"
    or None when ``detections`` is empty.

    `tag` is accepted for callsite compatibility but does not affect the
    Parquet filename.
    """
    _ensure_local_dir()
    from services.parquet_writer import write_detections_parquet

    # Snapshot the node's live CONFIG (rx/tx geometry + RF settings) so it
    # gets fanned into every Parquet row.  Failing to find it just means the
    # geometry columns end up null — never a fatal error for archival.
    node_cfg: dict | None = None
    try:
        from core import state  # local import to avoid cycle in tests
        info = state.connected_nodes.get(node_id) if hasattr(state, "connected_nodes") else None
        if info:
            node_cfg = info.get("config")
    except Exception:
        node_cfg = None

    return write_detections_parquet(
        node_id=node_id,
        frames=detections,
        base_dir=_LOCAL_ARCHIVE_DIR,
        node_cfg=node_cfg,
    )


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
        # Bounded scope — search both legacy (YYYY/MM/DD) and Hive
        # (year=YYYY/month=MM/day=DD) layouts under the same date_prefix.
        search_dirs: list[Path] = []
        legacy_dir = base / date_prefix
        if legacy_dir.exists():
            search_dirs.append(legacy_dir)
        hive_parts = date_prefix.split("/")
        hive_keys = ["year", "month", "day"]
        if 1 <= len(hive_parts) <= 3:
            hive_dir = base
            for k, v in zip(hive_keys, hive_parts):
                hive_dir = hive_dir / f"{k}={v}"
            if hive_dir.exists():
                search_dirs.append(hive_dir)

        if not search_dirs:
            return {"files": [], "count": 0, "total": 0}

        results = []
        for sd in search_dirs:
            for p in (*sd.rglob("*.parquet"), *sd.rglob("*.json")):
                rel = p.relative_to(base)
                parts = rel.parts
                file_node_id = _partition_value(parts[-2]) if len(parts) >= 2 else ""
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
                        if node_id and _partition_value(ndir.name) != node_id:
                            continue
                        try:
                            files = sorted(
                                [*ndir.glob("*.parquet"), *ndir.glob("*.json")],
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
    """Read an archived file by key. Returns parsed dict or None.

    Accepts both new Parquet keys (``*.parquet``, schema = per-detection rows)
    and legacy JSON keys (``*.json``, schema = nested per-frame). Parquet keys
    are reconstructed back into the legacy per-frame JSON shape so downstream
    consumers (the /api/data/archive/{key} endpoint) keep the same contract.
    """
    local_path = os.path.join(_LOCAL_ARCHIVE_DIR, key)
    if not os.path.isfile(local_path):
        return None
    # Prevent path traversal
    real_base = os.path.realpath(_LOCAL_ARCHIVE_DIR) + os.sep
    real_path = os.path.realpath(local_path)
    if not real_path.startswith(real_base):
        return None

    if key.endswith(".parquet"):
        return _read_parquet_as_legacy_json(local_path)
    with open(local_path) as f:
        return json.load(f)


def _read_parquet_as_legacy_json(path: str) -> dict:
    """Read a per-detection Parquet archive and reconstruct the per-frame JSON."""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    rows = table.to_pylist()
    if not rows:
        return {"node_id": "", "timestamp": "", "count": 0, "detections": []}

    node_id = rows[0]["node_id"] or ""
    by_frame: dict[int, dict] = {}
    for r in rows:
        ts = r["frame_ts_ms"]
        fr = by_frame.setdefault(ts, {
            "timestamp": ts,
            "delay": [], "doppler": [], "snr": [], "adsb": [],
            "_signing_mode": r.get("signing_mode"),
            "_signature_valid": r.get("signature_valid"),
            # Geometry/RF snapshot is per-frame (constant within a frame).
            "rx_lat": r.get("rx_lat"),
            "rx_lon": r.get("rx_lon"),
            "rx_alt_ft": r.get("rx_alt_ft"),
            "tx_lat": r.get("tx_lat"),
            "tx_lon": r.get("tx_lon"),
            "tx_alt_ft": r.get("tx_alt_ft"),
            "fc_hz": r.get("fc_hz"),
            "fs_hz": r.get("fs_hz"),
        })
        fr["delay"].append(r["delay_us"])
        fr["doppler"].append(r["doppler_hz"])
        fr["snr"].append(r["snr_db"])
        if r.get("adsb_hex"):
            fr["adsb"].append({
                "hex": r["adsb_hex"],
                "lat": r["adsb_lat"],
                "lon": r["adsb_lon"],
                "alt_baro": r["adsb_alt_baro"],
                "gs": r["adsb_gs"],
                "track": r["adsb_track"],
                "flight": r["adsb_flight"],
                "squawk": r.get("adsb_squawk"),
                "category": r.get("adsb_category"),
            })
        else:
            fr["adsb"].append(None)

    frames = [by_frame[k] for k in sorted(by_frame.keys())]
    return {
        "node_id": node_id,
        "timestamp": "",
        "count": len(frames),
        "detections": frames,
    }
