"""Tests for the Open-Meteo weather ingestion subsystem."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq

from services import weather_client as wc
from services import weather_writer as ww
from services.tasks import weather_archive as wa

# ── weather_client.fetch_current ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


def test_fetch_current_parses_open_meteo_payload(monkeypatch):
    """Maps Open-Meteo's 'current' block onto our flat sample dict."""
    payload = {
        "current": {
            "time": "2025-05-05T14:00",
            "temperature_2m": 18.4,
            "relative_humidity_2m": 64,
            "surface_pressure": 1013.2,
            "precipitation": 0.0,
            "wind_speed_10m": 5.4,
            "wind_direction_10m": 220,
            "cloud_cover": 30,
            "visibility": 24000,
            "weather_code": 2,
        }
    }

    def fake_get(url, params=None, timeout=10.0):
        return _FakeResponse(payload)

    monkeypatch.setattr(wc.httpx, "get", fake_get)

    sample = wc.fetch_current(40.7, -74.0)
    assert sample is not None
    assert sample["lat"] == 40.7
    assert sample["lon"] == -74.0
    assert sample["temperature_c"] == 18.4
    assert sample["humidity_pct"] == 64
    assert sample["wind_speed_ms"] == 5.4
    assert sample["weather_code"] == 2
    expected_ms = int(datetime(2025, 5, 5, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert sample["sample_ts_ms"] == expected_ms


def test_fetch_current_returns_none_on_http_error(monkeypatch):
    def fake_get(url, params=None, timeout=10.0):
        return _FakeResponse({}, status=500)

    monkeypatch.setattr(wc.httpx, "get", fake_get)
    assert wc.fetch_current(0.0, 0.0) is None


def test_fetch_current_returns_none_when_payload_lacks_current(monkeypatch):
    def fake_get(url, params=None, timeout=10.0):
        return _FakeResponse({"forecast": {}})

    monkeypatch.setattr(wc.httpx, "get", fake_get)
    assert wc.fetch_current(0.0, 0.0) is None


# ── weather_writer ────────────────────────────────────────────────────────────


def _sample(node_id: str = "node-A") -> dict:
    return {
        "sample_ts_ms": 1700000000000,
        "fetch_ts_ms": 1700000010000,
        "node_id": node_id,
        "lat": 40.7,
        "lon": -74.0,
        "temperature_c": 18.4,
        "humidity_pct": 64.0,
        "pressure_hpa": 1013.2,
        "precipitation_mm": 0.0,
        "wind_speed_ms": 5.4,
        "wind_dir_deg": 220.0,
        "cloud_cover_pct": 30.0,
        "visibility_m": 24000.0,
        "weather_code": 2,
    }


def test_writer_produces_hive_partitioned_per_node_file(tmp_path: Path):
    ts = datetime(2025, 5, 5, 14, 0, 0, tzinfo=timezone.utc)
    key = ww.write_weather_parquet(
        samples=[_sample()], base_dir=tmp_path, node_id="node-A", write_ts=ts,
    )
    assert key == "year=2025/month=05/day=05/node_id=node-A/hourly-140000.parquet"
    assert (tmp_path / key).exists()


def test_writer_round_trips_required_columns(tmp_path: Path):
    ts = datetime(2025, 5, 5, 14, 0, 0, tzinfo=timezone.utc)
    key = ww.write_weather_parquet(
        samples=[_sample()], base_dir=tmp_path, node_id="node-A", write_ts=ts,
    )
    table = pq.read_table(tmp_path / key)
    cols = set(table.column_names)
    expected = {
        "sample_ts_ms", "fetch_ts_ms", "node_id", "lat", "lon",
        "temperature_c", "humidity_pct", "pressure_hpa",
        "precipitation_mm", "wind_speed_ms", "wind_dir_deg",
        "cloud_cover_pct", "visibility_m", "weather_code",
    }
    assert expected <= cols
    rows = table.to_pylist()
    assert rows[0]["node_id"] == "node-A"
    assert rows[0]["temperature_c"] == 18.4


# ── weather_archive task ──────────────────────────────────────────────────────


def test_fetch_and_write_once_iterates_connected_nodes(tmp_path: Path, monkeypatch):
    """Each connected node with a usable rx_lat/rx_lon produces one Parquet file."""
    from core import state

    monkeypatch.setattr(wa, "_WEATHER_DIR", str(tmp_path))

    with patch.dict(state.connected_nodes, {
        "node-A": {"config": {"rx_lat": 40.7, "rx_lon": -74.0}},
        "node-B": {"config": {"rx_lat": 51.5, "rx_lon": -0.1}},
        "node-no-loc": {"config": {}},
    }, clear=True):
        with patch.object(wa, "fetch_current", return_value=_sample()):
            stats = wa.fetch_and_write_once(
                write_ts=datetime(2025, 5, 5, 14, 0, 0, tzinfo=timezone.utc),
            )

    # node-no-loc skipped (no rx_lat/lon)
    assert stats["nodes"] == 2
    assert stats["samples"] == 2
    assert stats["files"] == 2
    files = list(tmp_path.rglob("*.parquet"))
    assert len(files) == 2
    # Verify per-node partitioning
    paths = sorted(str(f.relative_to(tmp_path)) for f in files)
    assert paths[0].startswith("year=2025/month=05/day=05/node_id=node-A/")
    assert paths[1].startswith("year=2025/month=05/day=05/node_id=node-B/")


def test_fetch_and_write_once_counts_errors_when_fetch_fails(tmp_path: Path, monkeypatch):
    from core import state

    monkeypatch.setattr(wa, "_WEATHER_DIR", str(tmp_path))

    with patch.dict(state.connected_nodes, {
        "node-A": {"config": {"rx_lat": 40.7, "rx_lon": -74.0}},
    }, clear=True):
        with patch.object(wa, "fetch_current", return_value=None):
            stats = wa.fetch_and_write_once()

    assert stats["nodes"] == 1
    assert stats["errors"] == 1
    assert stats["files"] == 0
    assert not list(tmp_path.rglob("*.parquet"))
