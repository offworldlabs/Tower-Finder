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
