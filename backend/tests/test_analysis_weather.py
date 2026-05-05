"""Tests for the on-demand historical weather helper."""

from datetime import datetime, timezone

from analysis import weather


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return self._payload


def _archive_payload() -> dict:
    """Mirrors the open-meteo archive-api response shape."""
    return {
        "hourly": {
            "time": [
                "2025-06-01T00:00",
                "2025-06-01T01:00",
                "2025-06-01T02:00",
            ],
            "temperature_2m": [18.4, 18.0, 17.6],
            "relative_humidity_2m": [64, 65, 66],
            "surface_pressure": [1013.2, 1013.1, 1013.0],
            "precipitation": [0.0, 0.0, 0.1],
            "wind_speed_10m": [5.4, 5.6, 5.2],
            "wind_direction_10m": [220, 222, 218],
            "cloud_cover": [30, 35, 40],
            "visibility": [24000, 22000, 20000],
            "weather_code": [2, 2, 3],
        }
    }


def test_fetch_historical_returns_one_row_per_hour(monkeypatch):
    captured: dict = {}

    def fake_get(url, params=None, timeout=30.0):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(_archive_payload())

    monkeypatch.setattr(weather.httpx, "get", fake_get)

    rows = weather.fetch_historical(
        lat=40.7, lon=-74.0,
        start_dt=datetime(2025, 6, 1, tzinfo=timezone.utc),
        end_dt=datetime(2025, 6, 1, 23, 59, tzinfo=timezone.utc),
    )

    assert captured["url"] == weather.ARCHIVE_API_URL
    assert captured["params"]["start_date"] == "2025-06-01"
    assert captured["params"]["end_date"] == "2025-06-01"

    assert len(rows) == 3
    assert rows[0]["temperature_c"] == 18.4
    assert rows[0]["lat"] == 40.7
    assert rows[0]["lon"] == -74.0
    expected_ms = int(
        datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert rows[0]["sample_ts_ms"] == expected_ms
    assert rows[2]["weather_code"] == 3


def test_fetch_historical_filters_to_requested_window(monkeypatch):
    """Hours outside the requested [start, end] interval are dropped."""
    monkeypatch.setattr(
        weather.httpx, "get",
        lambda url, params=None, timeout=30.0: _FakeResponse(_archive_payload()),
    )

    rows = weather.fetch_historical(
        lat=40.7, lon=-74.0,
        start_dt=datetime(2025, 6, 1, 1, 0, tzinfo=timezone.utc),
        end_dt=datetime(2025, 6, 1, 1, 30, tzinfo=timezone.utc),
    )

    # Only the 01:00 row falls within the window (00:00 is before, 02:00 is after).
    assert len(rows) == 1
    assert rows[0]["temperature_c"] == 18.0


def test_fetch_historical_returns_empty_on_http_error(monkeypatch):
    monkeypatch.setattr(
        weather.httpx, "get",
        lambda url, params=None, timeout=30.0: _FakeResponse({}, status=503),
    )
    rows = weather.fetch_historical(
        lat=0.0, lon=0.0,
        start_dt=datetime(2025, 6, 1, tzinfo=timezone.utc),
        end_dt=datetime(2025, 6, 2, tzinfo=timezone.utc),
    )
    assert rows == []


def test_fetch_historical_returns_empty_on_payload_missing_hourly(monkeypatch):
    monkeypatch.setattr(
        weather.httpx, "get",
        lambda url, params=None, timeout=30.0: _FakeResponse({"latitude": 0.0}),
    )
    rows = weather.fetch_historical(
        lat=0.0, lon=0.0,
        start_dt=datetime(2025, 6, 1, tzinfo=timezone.utc),
        end_dt=datetime(2025, 6, 2, tzinfo=timezone.utc),
    )
    assert rows == []


def test_fetch_historical_handles_naive_datetimes(monkeypatch):
    """Naive datetimes are treated as UTC."""
    monkeypatch.setattr(
        weather.httpx, "get",
        lambda url, params=None, timeout=30.0: _FakeResponse(_archive_payload()),
    )
    rows = weather.fetch_historical(
        lat=40.7, lon=-74.0,
        start_dt=datetime(2025, 6, 1),  # naive
        end_dt=datetime(2025, 6, 1, 23, 59),
    )
    assert len(rows) == 3


def test_fetch_historical_returns_empty_when_end_before_start(monkeypatch):
    rows = weather.fetch_historical(
        lat=0.0, lon=0.0,
        start_dt=datetime(2025, 6, 2, tzinfo=timezone.utc),
        end_dt=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    assert rows == []
