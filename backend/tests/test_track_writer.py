"""Tests for the multi-node solver track Parquet writer."""

from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from services import track_writer as tw


def _record(ts_ms: int = 1700000000000, **overrides) -> dict:
    base = {
        "solve_ts_ms": ts_ms + 50,
        "timestamp_ms": ts_ms,
        "lat": 40.7,
        "lon": -74.0,
        "alt_m": 10500.0,
        "vel_east": 220.5,
        "vel_north": -33.0,
        "vel_up": 0.0,
        "n_nodes": 3,
        "contributing_node_ids": ["node-A", "node-B", "node-C"],
        "adsb_hex": "abcd12",
        "rms_delay": 1.2,
        "rms_doppler": 4.5,
        "target_class": "aircraft",
    }
    base.update(overrides)
    return base


def test_writes_hive_partitioned_path(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[_record()], base_dir=tmp_path, write_ts=ts)
    assert key == "year=2025/month=01/day=15/part-143022.parquet"
    assert (tmp_path / key).exists()


def test_schema_columns_present(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[_record()], base_dir=tmp_path, write_ts=ts)
    cols = set(pq.read_table(tmp_path / key).column_names)
    expected = {
        "solve_ts_ms", "frame_ts_ms", "lat", "lon", "alt_m",
        "vel_east_ms", "vel_north_ms", "vel_up_ms",
        "n_nodes", "contributing_node_ids", "adsb_hex",
        "rms_delay_us", "rms_doppler_hz", "target_class",
    }
    assert expected <= cols


def test_contributing_nodes_serialized_as_csv(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[_record()], base_dir=tmp_path, write_ts=ts)
    rows = pq.read_table(tmp_path / key).to_pylist()
    assert rows[0]["contributing_node_ids"] == "node-A,node-B,node-C"


def test_nullable_fields_round_trip(tmp_path: Path):
    rec = _record()
    rec.pop("alt_m")
    rec.pop("rms_delay")
    rec["adsb_hex"] = None
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[rec], base_dir=tmp_path, write_ts=ts)
    row = pq.read_table(tmp_path / key).to_pylist()[0]
    assert row["alt_m"] is None
    assert row["rms_delay_us"] is None
    assert row["adsb_hex"] is None
    assert row["lat"] == 40.7  # required field still populated


def test_empty_records_returns_none(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[], base_dir=tmp_path, write_ts=ts)
    assert key is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_flush_track_archive_buffer_drains_state(tmp_path: Path, monkeypatch):
    """flush_track_archive_buffer drains state.track_archive_buffer into a Parquet file."""
    from core import state
    from services.tasks import track_archive as ta

    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    state.track_archive_buffer.clear()
    state.track_archive_buffer.append(_record(ts_ms=1700000000000))
    state.track_archive_buffer.append(_record(ts_ms=1700000001000))

    key = ta.flush_track_archive_buffer()
    assert key is not None
    assert key.endswith(".parquet")
    assert len(state.track_archive_buffer) == 0

    table = pq.read_table(tmp_path / key)
    assert table.num_rows == 2


def test_flush_track_archive_buffer_no_op_when_empty(tmp_path: Path, monkeypatch):
    from core import state
    from services.tasks import track_archive as ta

    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    state.track_archive_buffer.clear()
    assert ta.flush_track_archive_buffer() is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_solver_hook_appends_to_track_archive_buffer():
    """A successful solve should append a record to state.track_archive_buffer."""
    from core import state
    from services.tasks import solver as sv

    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()

    fake_result = {
        "success": True,
        "lat": 40.0,
        "lon": -74.0,
        "alt_m": 11000.0,
        "vel_east": 200.0,
        "vel_north": -10.0,
        "n_nodes": 3,
        "contributing_node_ids": ["a", "b", "c"],
        "timestamp_ms": 1700000000000,
        "rms_delay": 1.0,
        "rms_doppler": 2.0,
    }

    def fake_solve(s_in, node_cfgs):
        return fake_result

    import time as _t
    item = ({"n_nodes": 3, "candidates": []}, {}, _t.time())
    sv._process_solver_item(item, fake_solve)

    assert len(state.track_archive_buffer) == 1
    rec = state.track_archive_buffer[0]
    assert rec["lat"] == 40.0
    assert "solve_ts_ms" in rec
