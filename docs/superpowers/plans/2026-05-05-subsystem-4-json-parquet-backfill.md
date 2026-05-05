# Subsystem 4 — JSON → Parquet historical backfill

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a one-shot script that reads existing legacy JSON detection archives (from R2 or the local archive directory) and rewrites them as Parquet files using the new Hive-style layout, so the open dataset starts continuous from day one rather than from cutover.

**Architecture:**
- New module `backfill/json_to_parquet.py` containing pure conversion logic (testable without R2).
- New script `scripts/backfill_archive.py` that wires the conversion module to either a local archive directory or R2.
- Conversion is idempotent: if the target Parquet key already exists, the JSON is skipped (unless `--force`).
- Original JSON files are **not** deleted by the script; that's a follow-up decision after verification.
- Source `timestamp` (ISO8601 wallclock) drives the Hive partition values, so the chronology of the original archive is preserved.

**Tech Stack:** `pyarrow` (already in deps), `boto3` (already a dep — used by `services.r2_client`).

---

### Task 1: Pure conversion module

**Files:**
- Create: `backend/backfill/__init__.py`
- Create: `backend/backfill/json_to_parquet.py`
- Test: `backend/tests/test_backfill_json_to_parquet.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_backfill_json_to_parquet.py`:

```python
"""Tests for backfill conversion of legacy JSON detection archives to Parquet."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from backfill import json_to_parquet as b


def _legacy_payload(node_id: str = "node-A") -> dict:
    return {
        "node_id": node_id,
        "timestamp": "2025-06-21T14:30:22.123456+00:00",
        "count": 1,
        "detections": [{
            "timestamp": 1700000000000,
            "delay": [12.34, 56.78],
            "doppler": [-100.5, 33.3],
            "snr": [15.0, 22.0],
            "adsb": [None, {"hex": "abcd12", "lat": 40.0, "lon": -74.0,
                            "alt_baro": 35000, "gs": 480, "track": 270,
                            "flight": "DLH123"}],
            "_signing_mode": "unknown",
            "_signature_valid": False,
        }],
    }


def test_target_key_uses_iso_timestamp_for_partitioning():
    payload = _legacy_payload(node_id="alpha")
    key = b.target_key_for(payload)
    assert key == "archive/year=2025/month=06/day=21/node_id=alpha/part-143022.parquet"


def test_convert_roundtrips_into_parquet(tmp_path: Path):
    payload = _legacy_payload()
    out_path = tmp_path / "out.parquet"
    b.convert_payload_to_parquet(payload, out_path)
    table = pq.read_table(out_path)
    rows = table.to_pylist()
    assert table.num_rows == 2  # 2 detections
    assert rows[0]["node_id"] == "node-A"
    assert rows[0]["delay_us"] == 12.34
    assert rows[1]["adsb_hex"] == "abcd12"
    assert rows[0]["adsb_hex"] is None


def test_target_key_handles_missing_timestamp():
    """When the legacy JSON lacks a top-level timestamp, fall back to first frame ts."""
    payload = _legacy_payload()
    payload["timestamp"] = ""
    key = b.target_key_for(payload)
    # frame_ts 1700000000000 → 2023-11-14 22:13:20 UTC
    assert key.startswith("archive/year=2023/month=11/day=14/node_id=node-A/part-22")


def test_target_key_node_id_fallback_to_first_frame():
    """If top-level node_id missing, derive from frame _node_id field."""
    payload = _legacy_payload()
    payload["node_id"] = ""
    payload["detections"][0]["_node_id"] = "from-frame"
    key = b.target_key_for(payload)
    assert "node_id=from-frame" in key


def test_convert_legacy_bytes(tmp_path: Path):
    """Convenience wrapper: bytes in -> Parquet bytes + key out."""
    raw = json.dumps(_legacy_payload()).encode()
    key, parquet_bytes = b.convert_legacy_bytes(raw)
    assert key.endswith(".parquet")
    out = tmp_path / "x.parquet"
    out.write_bytes(parquet_bytes)
    table = pq.read_table(out)
    assert table.num_rows == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_backfill_json_to_parquet.py --no-cov -v`
Expected: ALL FAIL — module doesn't exist yet.

- [ ] **Step 3: Create the module**

Create `backend/backfill/__init__.py` (empty file):

```python
"""Backfill scripts and conversion helpers for the open dataset."""
```

Create `backend/backfill/json_to_parquet.py`:

```python
"""Convert legacy per-frame JSON detection archives to per-detection Parquet.

The legacy JSON layout was:

    {"node_id": "...", "timestamp": "<ISO>", "count": N,
     "detections": [{"timestamp": ms, "delay": [...], ...}, ...]}

The new Parquet layout uses the schema from services.parquet_writer.SCHEMA,
flattened to one row per detection with frame metadata duplicated.

Public surface:
    target_key_for(payload)               -> str (Hive R2 key)
    convert_payload_to_parquet(payload, out_path)
    convert_legacy_bytes(raw_json_bytes)  -> (target_key, parquet_bytes)
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from services.parquet_writer import SCHEMA, _flatten


def _resolve_node_id(payload: dict) -> str:
    nid = (payload.get("node_id") or "").strip()
    if nid:
        return nid
    for fr in payload.get("detections", []) or []:
        candidate = (fr.get("_node_id") or fr.get("node_id") or "").strip()
        if candidate:
            return candidate
    return "unknown"


def _resolve_write_ts(payload: dict) -> datetime:
    """Pick the wallclock timestamp used to compute the Hive partition keys.

    Priority:
      1. Top-level ``timestamp`` (ISO8601 string written by the legacy archiver).
      2. First frame's ``timestamp`` (unix ms).
      3. Epoch — should never happen in practice, but keeps the function total.
    """
    raw = (payload.get("timestamp") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    for fr in payload.get("detections", []) or []:
        ts = fr.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def target_key_for(payload: dict) -> str:
    """Compute the Hive-partitioned R2 key for a given legacy payload."""
    node_id = _resolve_node_id(payload)
    ts = _resolve_write_ts(payload)
    return (
        f"archive/year={ts:%Y}/month={ts:%m}/day={ts:%d}/"
        f"node_id={node_id}/part-{ts:%H%M%S}.parquet"
    )


def _build_table(payload: dict, ingest_ts_ms: int) -> pa.Table:
    node_id = _resolve_node_id(payload)
    frames = payload.get("detections", []) or []
    cols = _flatten(node_id, frames, ingest_ts_ms)
    return pa.table(cols, schema=SCHEMA)


def convert_payload_to_parquet(payload: dict, out_path: str | Path) -> Path:
    """Write a single legacy payload as Parquet at ``out_path``."""
    write_ts = _resolve_write_ts(payload)
    ingest_ts_ms = int(write_ts.timestamp() * 1000)
    table = _build_table(payload, ingest_ts_ms)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    return out_path


def convert_legacy_bytes(raw: bytes) -> tuple[str, bytes]:
    """Convert raw legacy JSON bytes to (target_key, parquet_bytes)."""
    payload = json.loads(raw)
    target = target_key_for(payload)
    write_ts = _resolve_write_ts(payload)
    ingest_ts_ms = int(write_ts.timestamp() * 1000)
    table = _build_table(payload, ingest_ts_ms)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", compression_level=3)
    return target, buf.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_backfill_json_to_parquet.py --no-cov -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/backfill/__init__.py backend/backfill/json_to_parquet.py backend/tests/test_backfill_json_to_parquet.py
git commit -m "feat(backfill): pure JSON->Parquet conversion module"
```

---

### Task 2: R2-driven backfill script

**Files:**
- Create: `backend/scripts/backfill_archive.py`

- [ ] **Step 1: Create the script**

Create `backend/scripts/backfill_archive.py`:

```python
"""One-shot backfill: convert legacy JSON detection archives in R2 to Parquet.

Usage:
    PYTHONPATH=. python3 scripts/backfill_archive.py [--prefix archive/] [--limit N] [--dry-run] [--force]

Behaviour:
- Lists all keys under --prefix (default ``archive/``) ending in ``.json``.
- For each, downloads, converts via backfill.json_to_parquet, uploads to the
  Hive-partitioned target key (also under ``archive/``).
- Skips keys whose target already exists in R2 unless --force is given.
- The original JSON is **not** deleted; that decision is left for a separate
  cleanup pass after the new files are verified.
"""

from __future__ import annotations

import argparse
import logging
import sys

from backfill.json_to_parquet import convert_legacy_bytes
from services import r2_client

logger = logging.getLogger("backfill")


def _target_exists(key: str) -> bool:
    """Check whether the target Parquet key already lives in R2."""
    return r2_client.download_bytes(key) is not None


def run(prefix: str = "archive/", limit: int | None = None,
        dry_run: bool = False, force: bool = False) -> dict:
    if not r2_client.is_enabled():
        logger.error("R2 is not configured; aborting.")
        return {"error": "r2_disabled"}

    stats = {"scanned": 0, "converted": 0, "skipped": 0, "errors": 0}

    keys = [k for k in r2_client.list_keys(prefix) if k.endswith(".json")]
    if limit:
        keys = keys[:limit]
    logger.info("Found %d legacy JSON keys under %s", len(keys), prefix)

    for src_key in keys:
        stats["scanned"] += 1
        try:
            raw = r2_client.download_bytes(src_key)
            if not raw:
                stats["errors"] += 1
                continue
            target_key, parquet_bytes = convert_legacy_bytes(raw)
            if not force and _target_exists(target_key):
                stats["skipped"] += 1
                continue
            if dry_run:
                logger.info("DRY: %s -> %s (%d bytes)", src_key, target_key, len(parquet_bytes))
            else:
                ok = r2_client.upload_bytes(
                    target_key, parquet_bytes,
                    content_type="application/octet-stream",
                )
                if not ok:
                    stats["errors"] += 1
                    continue
            stats["converted"] += 1
            if stats["scanned"] % 100 == 0:
                logger.info("Progress: %s", stats)
        except Exception:
            logger.exception("Failed to convert %s", src_key)
            stats["errors"] += 1

    logger.info("Backfill done: %s", stats)
    return stats


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default="archive/")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    stats = run(prefix=args.prefix, limit=args.limit,
                dry_run=args.dry_run, force=args.force)
    if stats.get("error"):
        sys.exit(2)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Add a test that exercises `run()` with a stubbed r2_client**

Append to `backend/tests/test_backfill_json_to_parquet.py`:

```python
def test_run_uploads_parquet_for_each_legacy_key(monkeypatch):
    """Driver run() should download each JSON and upload exactly one Parquet."""
    from scripts import backfill_archive as ba

    payload_bytes = json.dumps(_legacy_payload()).encode()
    fake_keys = ["archive/2025/06/21/alpha/detections_143022.json"]
    uploads: list[tuple[str, bytes]] = []

    monkeypatch.setattr(ba.r2_client, "is_enabled", lambda: True)
    monkeypatch.setattr(ba.r2_client, "list_keys", lambda prefix="": fake_keys)
    # First call to download_bytes returns the source JSON;
    # second call (target-exists check) returns None.
    calls = {"n": 0}

    def fake_download(key: str):
        calls["n"] += 1
        if key == fake_keys[0]:
            return payload_bytes
        return None

    monkeypatch.setattr(ba.r2_client, "download_bytes", fake_download)

    def fake_upload(key, data, **kw):
        uploads.append((key, data))
        return True

    monkeypatch.setattr(ba.r2_client, "upload_bytes", fake_upload)

    stats = ba.run(prefix="archive/", limit=None, dry_run=False, force=False)
    assert stats["scanned"] == 1
    assert stats["converted"] == 1
    assert stats["skipped"] == 0
    assert len(uploads) == 1
    assert uploads[0][0].startswith("archive/year=2025/month=06/day=21/")
```

- [ ] **Step 3: Run tests + full suite**

Run: `cd backend && python3 -m pytest tests/test_backfill_json_to_parquet.py --no-cov -v`
Expected: 6 PASS.

Run: `cd backend && python3 -m pytest --no-cov -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/backfill_archive.py backend/tests/test_backfill_json_to_parquet.py
git commit -m "feat(backfill): R2-driven backfill script for JSON->Parquet conversion"
```
