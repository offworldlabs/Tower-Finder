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
    ("adsb_squawk", pa.string()),
    ("adsb_category", pa.string()),
    ("signing_mode", pa.string()),
    ("signature_valid", pa.bool_()),
    ("payload_hash", pa.string()),
    ("signature", pa.string()),
    # Per-frame TX/RX geometry & RF config snapshot.  These never appear on
    # the wire frame itself — they live in the node's CONFIG handshake — but
    # they're cheap to fan out into every row (Parquet's per-column dictionary
    # encoding makes constants effectively free) and impossible to reconstruct
    # later if the node config drifts or the node is taken offline.
    ("rx_lat", pa.float64()),
    ("rx_lon", pa.float64()),
    ("rx_alt_ft", pa.float64()),
    ("tx_lat", pa.float64()),
    ("tx_lon", pa.float64()),
    ("tx_alt_ft", pa.float64()),
    ("fc_hz", pa.float64()),
    ("fs_hz", pa.float64()),
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


def _flatten(
    node_id: str,
    frames: list[dict],
    ingest_ts_ms: int,
    node_cfg: dict | None = None,
) -> dict[str, list]:
    """Flatten a list of per-frame dicts into per-detection columnar dict."""
    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    cfg_rx_lat = _get_float(node_cfg, "rx_lat")
    cfg_rx_lon = _get_float(node_cfg, "rx_lon")
    cfg_rx_alt = _get_float(node_cfg, "rx_alt_ft")
    cfg_tx_lat = _get_float(node_cfg, "tx_lat")
    cfg_tx_lon = _get_float(node_cfg, "tx_lon")
    cfg_tx_alt = _get_float(node_cfg, "tx_alt_ft")
    # Node config uses fc_hz/fs_hz on the wire but some legacy paths use FC/Fs.
    cfg_fc = _get_float(node_cfg, "fc_hz")
    if cfg_fc is None:
        cfg_fc = _get_float(node_cfg, "FC")
    cfg_fs = _get_float(node_cfg, "fs_hz")
    if cfg_fs is None:
        cfg_fs = _get_float(node_cfg, "Fs")
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
        # Per-frame geometry override: if the node ever sends rx/tx in the
        # frame itself (e.g. mobile receivers in the future), use that value
        # in preference to the static node config snapshot.
        f_rx_lat = _get_float(frame, "rx_lat")
        f_rx_lon = _get_float(frame, "rx_lon")
        f_rx_alt = _get_float(frame, "rx_alt_ft")
        f_tx_lat = _get_float(frame, "tx_lat")
        f_tx_lon = _get_float(frame, "tx_lon")
        f_tx_alt = _get_float(frame, "tx_alt_ft")
        f_fc = _get_float(frame, "fc_hz")
        f_fs = _get_float(frame, "fs_hz")
        rx_lat = f_rx_lat if f_rx_lat is not None else cfg_rx_lat
        rx_lon = f_rx_lon if f_rx_lon is not None else cfg_rx_lon
        rx_alt = f_rx_alt if f_rx_alt is not None else cfg_rx_alt
        tx_lat = f_tx_lat if f_tx_lat is not None else cfg_tx_lat
        tx_lon = f_tx_lon if f_tx_lon is not None else cfg_tx_lon
        tx_alt = f_tx_alt if f_tx_alt is not None else cfg_tx_alt
        fc_hz = f_fc if f_fc is not None else cfg_fc
        fs_hz = f_fs if f_fs is not None else cfg_fs
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
            cols["adsb_squawk"].append(ae.get("squawk") if ae else None)
            cols["adsb_category"].append(ae.get("category") if ae else None)
            cols["signing_mode"].append(sig_mode)
            cols["signature_valid"].append(bool(sig_valid) if sig_valid is not None else None)
            cols["payload_hash"].append(payload_hash)
            cols["signature"].append(signature)
            cols["rx_lat"].append(rx_lat)
            cols["rx_lon"].append(rx_lon)
            cols["rx_alt_ft"].append(rx_alt)
            cols["tx_lat"].append(tx_lat)
            cols["tx_lon"].append(tx_lon)
            cols["tx_alt_ft"].append(tx_alt)
            cols["fc_hz"].append(fc_hz)
            cols["fs_hz"].append(fs_hz)
    return cols


def write_detections_parquet(
    *,
    node_id: str,
    frames: list[dict],
    base_dir: str | Path,
    write_ts: datetime | None = None,
    node_cfg: dict | None = None,
) -> str | None:
    """Write a list of detection frames as a single Parquet file.

    ``node_cfg`` is the live node CONFIG dict (rx_lat/rx_lon/tx_lat/tx_lon,
    fc_hz, fs_hz, …) snapshotted at flush time; its values are fanned out to
    every row so the dataset captures the geometry that produced the
    detections, not just the detections themselves.  Returns the relative
    Hive-partitioned key, or None if ``frames`` is empty.
    """
    if not frames:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    ingest_ts_ms = int(write_ts.timestamp() * 1000)
    cols = _flatten(node_id, frames, ingest_ts_ms, node_cfg)
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
