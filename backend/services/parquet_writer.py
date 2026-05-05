"""Parquet writer for detection archive files.

Writes one Parquet file per flush in Hive-partitioned form:

    archive/year=YYYY/month=MM/day=DD/node_id=XXX/part-HHMMSS.parquet

Schema is per-detection: each row corresponds to one detection inside a frame.
Frame-level metadata (timestamp, signing mode, signature validity) is repeated
on every detection row from that frame. ADS-B truth data, when associated
with a detection at frame build time, lands in the adsb_* columns; when no
match is present those columns are null.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


# Stable schema — adding new columns is fine (Parquet is schema-on-read for
# missing cols), but never rename or reorder.
SCHEMA = pa.schema([
    ("frame_ts_ms", pa.int64()),
    ("ingest_ts_ms", pa.int64()),
    ("node_id", pa.string()),
    ("detection_index", pa.int32()),
    ("delay_us", pa.float64()),
    ("doppler_hz", pa.float64()),
    ("snr_db", pa.float64()),
    ("adsb_hex", pa.string()),
    ("adsb_lat", pa.float64()),
    ("adsb_lon", pa.float64()),
    ("adsb_alt_baro", pa.int32()),
    ("adsb_gs", pa.float64()),
    ("adsb_track", pa.float64()),
    ("adsb_flight", pa.string()),
    ("signing_mode", pa.string()),
    ("signature_valid", pa.bool_()),
    ("payload_hash", pa.string()),
    ("signature", pa.string()),
])


def _safe_float(arr, i: int) -> float | None:
    if i >= len(arr):
        return None
    v = arr[i]
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _get_float(d: dict | None, key: str) -> float | None:
    if not d:
        return None
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _get_int(d: dict | None, key: str) -> int | None:
    if not d:
        return None
    v = d.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _flatten(node_id: str, frames: list[dict], ingest_ts_ms: int) -> dict[str, list]:
    """Flatten a list of per-frame dicts into per-detection columnar dict."""
    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for frame in frames:
        delay = frame.get("delay") or []
        doppler = frame.get("doppler") or []
        snr = frame.get("snr") or []
        adsb = frame.get("adsb") or []
        n = max(len(delay), len(doppler), len(snr))
        if n == 0:
            continue
        frame_ts = int(frame.get("timestamp", 0) or 0)
        sig_mode = frame.get("_signing_mode") or frame.get("signing_mode")
        sig_valid = frame.get("_signature_valid")
        if sig_valid is None:
            sig_valid = frame.get("signature_valid")
        payload_hash = frame.get("payload_hash") or None
        signature = frame.get("signature") or None
        for i in range(n):
            ae = adsb[i] if i < len(adsb) and isinstance(adsb[i], dict) else None
            cols["frame_ts_ms"].append(frame_ts)
            cols["ingest_ts_ms"].append(ingest_ts_ms)
            cols["node_id"].append(node_id)
            cols["detection_index"].append(i)
            cols["delay_us"].append(_safe_float(delay, i))
            cols["doppler_hz"].append(_safe_float(doppler, i))
            cols["snr_db"].append(_safe_float(snr, i))
            cols["adsb_hex"].append(ae.get("hex") if ae else None)
            cols["adsb_lat"].append(_get_float(ae, "lat"))
            cols["adsb_lon"].append(_get_float(ae, "lon"))
            cols["adsb_alt_baro"].append(_get_int(ae, "alt_baro"))
            cols["adsb_gs"].append(_get_float(ae, "gs"))
            cols["adsb_track"].append(_get_float(ae, "track"))
            cols["adsb_flight"].append(ae.get("flight") if ae else None)
            cols["signing_mode"].append(sig_mode)
            cols["signature_valid"].append(bool(sig_valid) if sig_valid is not None else None)
            cols["payload_hash"].append(payload_hash)
            cols["signature"].append(signature)
    return cols


def write_detections_parquet(
    *,
    node_id: str,
    frames: list[dict],
    base_dir: str | Path,
    write_ts: datetime | None = None,
) -> str | None:
    """Write a list of detection frames as a single Parquet file.

    Returns the relative key (Hive-partitioned path) or None if frames is empty.
    """
    if not frames:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    ingest_ts_ms = int(write_ts.timestamp() * 1000)
    cols = _flatten(node_id, frames, ingest_ts_ms)
    if not cols["frame_ts_ms"]:
        return None

    table = pa.table(cols, schema=SCHEMA)

    key = (
        f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}/"
        f"node_id={node_id}/part-{write_ts:%H%M%S}.parquet"
    )
    out_path = Path(base_dir) / key
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    return key
