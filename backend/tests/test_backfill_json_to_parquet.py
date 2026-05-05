"""Tests for backfill conversion of legacy JSON detection archives to Parquet."""

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

# Make the scripts/ directory importable as a top-level package for tests
# that exercise the CLI entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

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
    assert table.num_rows == 2
    assert rows[0]["node_id"] == "node-A"
    assert rows[0]["delay_us"] == 12.34
    assert rows[1]["adsb_hex"] == "abcd12"
    assert rows[0]["adsb_hex"] is None


def test_target_key_handles_missing_timestamp():
    """When the legacy JSON lacks a top-level timestamp, fall back to first frame ts."""
    payload = _legacy_payload()
    payload["timestamp"] = ""
    key = b.target_key_for(payload)
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


def test_run_uploads_parquet_for_each_legacy_key(monkeypatch):
    """Driver run() should download each JSON and upload exactly one Parquet."""
    import importlib
    ba = importlib.import_module("scripts.backfill_archive")

    payload_bytes = json.dumps(_legacy_payload()).encode()
    fake_keys = ["archive/2025/06/21/alpha/detections_143022.json"]
    uploads: list[tuple[str, bytes]] = []

    monkeypatch.setattr(ba.r2_client, "is_enabled", lambda: True)
    monkeypatch.setattr(ba.r2_client, "list_keys", lambda prefix="": fake_keys)

    def fake_download(key: str):
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
