"""Tests for the Parquet detection archive writer."""

from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from services import parquet_writer as pw


def _frame(timestamp_ms: int, n_dets: int = 3, with_adsb: bool = False) -> dict:
    return {
        "timestamp": timestamp_ms,
        "delay": [10.0 + i for i in range(n_dets)],
        "doppler": [-50.0 + i for i in range(n_dets)],
        "snr": [12.0 + i for i in range(n_dets)],
        "adsb": (
            [
                {"hex": "abcdef", "lat": 40.0, "lon": -74.0,
                 "alt_baro": 35000, "gs": 480, "track": 270, "flight": "UAL1"}
            ] + [None] * (n_dets - 1)
            if with_adsb else [None] * n_dets
        ),
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }


def test_writes_hive_partitioned_path(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )

    expected = "year=2025/month=01/day=15/node_id=node-A/part-143022.parquet"
    assert key == expected
    assert (tmp_path / key).exists()


def test_schema_is_per_detection_with_required_columns(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=4)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )

    table = pq.read_table(tmp_path / key)
    assert table.num_rows == 4
    cols = set(table.column_names)
    expected = {
        "frame_ts_ms", "node_id", "detection_index",
        "delay_us", "doppler_hz", "snr_db",
        "adsb_hex", "adsb_lat", "adsb_lon", "adsb_alt_baro",
        "adsb_gs", "adsb_track", "adsb_flight",
        "signing_mode", "signature_valid",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_adsb_match_populated_when_present(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=3, with_adsb=True)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )

    table = pq.read_table(tmp_path / key)
    rows = table.to_pylist()
    assert rows[0]["adsb_hex"] == "abcdef"
    assert rows[0]["adsb_lat"] == 40.0
    assert rows[1]["adsb_hex"] is None
    assert rows[2]["adsb_hex"] is None


def test_multiple_frames_concatenate(tmp_path: Path):
    frames = [
        _frame(timestamp_ms=1700000000000, n_dets=3),
        _frame(timestamp_ms=1700000001000, n_dets=2),
    ]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    table = pq.read_table(tmp_path / key)
    assert table.num_rows == 5
    rows = table.to_pylist()
    assert rows[0]["frame_ts_ms"] == 1700000000000
    assert rows[3]["frame_ts_ms"] == 1700000001000
    assert rows[3]["detection_index"] == 0


def test_empty_frames_returns_none(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = pw.write_detections_parquet(
        node_id="node-A", frames=[], base_dir=tmp_path, write_ts=ts,
    )
    assert key is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_uses_zstd_compression(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=10)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    meta = pq.read_metadata(tmp_path / key)
    rg = meta.row_group(0)
    codecs = {rg.column(i).compression for i in range(rg.num_columns)}
    assert "ZSTD" in codecs or "zstd" in {c.lower() for c in codecs}


def test_schema_includes_custody_and_ingest_columns(tmp_path: Path):
    """Schema must include payload_hash, signature, ingest_ts_ms and round-trip values."""
    frames = [{
        "timestamp": 1700000000000,
        "delay": [10.0, 11.0],
        "doppler": [-50.0, -49.0],
        "snr": [12.0, 13.0],
        "adsb": [None, None],
        "payload_hash": "deadbeef",
        "signature": "abcd1234",
        "_signing_mode": "ed25519",
        "_signature_valid": True,
    }]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    table = pq.read_table(tmp_path / key)
    cols = set(table.column_names)
    assert {"payload_hash", "signature", "ingest_ts_ms"} <= cols

    rows = table.to_pylist()
    assert rows[0]["payload_hash"] == "deadbeef"
    assert rows[0]["signature"] == "abcd1234"
    expected_ms = int(ts.timestamp() * 1000)
    assert rows[0]["ingest_ts_ms"] == expected_ms


def test_custody_columns_default_null_when_absent(tmp_path: Path):
    """Frames without payload_hash/signature get nulls; ingest_ts_ms is always set."""
    frames = [_frame(timestamp_ms=1700000000000, n_dets=2)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A", frames=frames, base_dir=tmp_path, write_ts=ts,
    )
    rows = pq.read_table(tmp_path / key).to_pylist()
    assert all(r["payload_hash"] is None for r in rows)
    assert all(r["signature"] is None for r in rows)
    assert all(isinstance(r["ingest_ts_ms"], int) for r in rows)


def test_round_trip_via_storage_module(tmp_path: Path, monkeypatch):
    """archive_detections + read_archived_file round-trips back to legacy JSON shape."""
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))

    from services.storage import archive_detections, read_archived_file

    frames = [_frame(timestamp_ms=1700000000000, n_dets=2, with_adsb=True)]
    key = archive_detections("node-X", frames)
    assert key is not None
    assert key.endswith(".parquet")

    decoded = read_archived_file(key)
    assert decoded is not None
    assert decoded["node_id"] == "node-X"
    assert decoded["count"] == 1
    fr0 = decoded["detections"][0]
    assert fr0["delay"] == [10.0, 11.0]
    assert fr0["adsb"][0]["hex"] == "abcdef"
    assert fr0["adsb"][1] is None
