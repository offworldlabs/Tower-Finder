"""Tests for tower-finding, health, and helper functions."""

import os
import unittest.mock

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── _detect_source ───────────────────────────────────────────────────────────

class TestDetectSource:
    def test_us_mainland(self):
        from routes.towers import _detect_source

        assert _detect_source(34.05, -118.25) == "us"

    def test_australia(self):
        from routes.towers import _detect_source

        assert _detect_source(-33.87, 151.21) == "au"

    def test_canada(self):
        from routes.towers import _detect_source

        assert _detect_source(45.42, -75.69) == "ca"

    def test_hawaii(self):
        from routes.towers import _detect_source

        assert _detect_source(21.31, -157.86) == "us"

    def test_alaska(self):
        from routes.towers import _detect_source

        assert _detect_source(64.2, -152.5) == "us"

    def test_unknown_defaults_to_us(self):
        from routes.towers import _detect_source

        assert _detect_source(48.85, 2.35) == "us"  # Paris → falls through to us


# ── Health endpoint ──────────────────────────────────────────────────────────

class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_degraded_stale_task(self, client):
        import time

        state.task_last_success["frame_processor"] = time.time() - 9999
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert "issues" not in body  # details are logged, not exposed publicly
        finally:
            state.task_last_success.pop("frame_processor", None)

    def test_health_degraded_queue_saturated(self, client):
        """Fill the frame queue past 90% to trigger saturation warning."""
        import queue

        orig_queue = state.frame_queue
        # Create a small queue and fill it
        small_q = queue.Queue(maxsize=10)
        for i in range(10):
            small_q.put(i)
        state.frame_queue = small_q
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert "issues" not in body  # details are logged, not exposed publicly
        finally:
            state.frame_queue = orig_queue

    def test_health_degraded_disk_low(self, client):
        """shutil.disk_usage returns <500 MB free → disk_low appended → degraded."""
        import shutil

        mock_disk = unittest.mock.MagicMock()
        mock_disk.free = 100 * 1024 * 1024  # 100 MB < 500 MB threshold
        with unittest.mock.patch.object(shutil, "disk_usage", return_value=mock_disk):
            r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"

    def test_health_disk_usage_exception_does_not_crash(self, client):
        """shutil.disk_usage raises OSError → exception pass → endpoint still 200."""
        import shutil

        with unittest.mock.patch.object(shutil, "disk_usage", side_effect=OSError("no disk")):
            r = client.get("/api/health")
        assert r.status_code == 200


# ── Tower search validation ──────────────────────────────────────────────────

class TestTowerSearch:
    def test_missing_lat_lon(self, client):
        r = client.get("/api/towers")
        assert r.status_code == 422  # Missing required query params

    def test_invalid_source(self, client):
        r = client.get("/api/towers?lat=33.45&lon=-112.07&source=invalid")
        assert r.status_code == 400
        assert "Invalid source" in r.json()["detail"]

    def test_lat_out_of_range(self, client):
        r = client.get("/api/towers?lat=100&lon=0")
        assert r.status_code == 422


# ── Config endpoints ─────────────────────────────────────────────────────────

class TestTowerConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200

    def test_update_config_too_large_returns_413(self, client):
        """PUT /api/config with a body > 1 MB → 413 before writing to disk."""
        huge_body = {"data": "x" * 1_100_000}
        with unittest.mock.patch("routes.towers.require_admin", return_value=None):
            r = client.put("/api/config", json=huge_body)
        assert r.status_code == 413
        assert "too large" in r.json()["detail"].lower()


# ── /api/elevation ──────────────────────────────────────────────────────────


class TestElevationEndpoint:
    async def test_elevation_lookup_failed_returns_502(self):
        with unittest.mock.patch(
            "routes.towers._lookup_elevation",
            new=unittest.mock.AsyncMock(return_value=None),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/elevation?lat=33.9&lon=-84.6")
        assert r.status_code == 502
        assert "Elevation lookup failed" in r.json()["detail"]

    async def test_elevation_lookup_success(self):
        with unittest.mock.patch(
            "routes.towers._lookup_elevation",
            new=unittest.mock.AsyncMock(return_value=312.5),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/elevation?lat=33.9&lon=-84.6")
        assert r.status_code == 200
        body = r.json()
        assert body["elevation_m"] == pytest.approx(312.5)
        assert body["latitude"] == pytest.approx(33.9)
        assert body["longitude"] == pytest.approx(-84.6)


# ── _batch_lookup_elevations ─────────────────────────────────────────────────

def _make_httpx_mock(get_return=None, get_side_effect=None):
    """Return a patch context manager that intercepts httpx.AsyncClient."""
    mock_client = unittest.mock.AsyncMock()
    mock_client.get = unittest.mock.AsyncMock(
        return_value=get_return, side_effect=get_side_effect
    )
    mock_ctx = unittest.mock.MagicMock()
    mock_ctx.__aenter__ = unittest.mock.AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = unittest.mock.AsyncMock(return_value=False)
    return unittest.mock.patch("httpx.AsyncClient", return_value=mock_ctx)


class TestBatchLookupElevations:
    async def test_empty_list_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        result = await _batch_lookup_elevations([])
        assert result == {}

    async def test_http_success_returns_elevation(self):
        from routes.towers import _batch_lookup_elevations

        mock_resp = unittest.mock.MagicMock()
        mock_resp.raise_for_status = unittest.mock.MagicMock()
        mock_resp.json.return_value = {"elevation": [123.4]}

        with _make_httpx_mock(get_return=mock_resp):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {(33.9, -84.6): 123.4}

    async def test_http_timeout_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        with _make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {}

    async def test_http_500_error_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        mock_resp = unittest.mock.MagicMock()
        mock_resp.raise_for_status = unittest.mock.MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500 Server Error",
                request=unittest.mock.MagicMock(),
                response=unittest.mock.MagicMock(),
            )
        )

        with _make_httpx_mock(get_return=mock_resp):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {}

    async def test_generic_connection_error_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        with _make_httpx_mock(get_side_effect=httpx.ConnectError("connection refused")):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {}


# ── find_towers service-error paths ─────────────────────────────────────────

class TestFindTowersServiceErrors:
    def test_fcc_succeeds_maprad_fails_returns_200(self):
        fcc_data = [
            {
                "call_sign": "TEST",
                "latitude": 33.9,
                "longitude": -84.6,
                "distance_km": 10,
                "frequency_mhz": 100.1,
            }
        ]

        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=fcc_data),
            ),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("Maprad down")),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=us")

        assert r.status_code == 200
        assert "towers" in r.json()

    def test_fcc_fetch_fails_returns_502(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("Network error")),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=us")

        assert r.status_code == 502

    def test_non_us_no_api_key_returns_500(self):
        with unittest.mock.patch("routes.towers.API_KEY", ""):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=au")

        assert r.status_code == 500
        assert "MAPRAD_API_KEY not configured" in r.json()["detail"]

    def test_non_us_with_api_key_fetch_fails_returns_502(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("AU service down")),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=au")

        assert r.status_code == 502
