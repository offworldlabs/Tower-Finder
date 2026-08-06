"""Tests for the simulation ingest routes (routes/sim_ingest.py).

First coverage for this router: the ground-truth push is the source of every
"simulated parameters" field the debug map shows, so its schema — including
tolerance of older fleet payloads — needs a guard.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from main import app  # noqa: E402

_KEY = {"X-API-Key": os.environ["RADAR_API_KEY"]}


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_state():
    def _wipe():
        state.ground_truth_trails.clear()
        state.ground_truth_meta.clear()
        with state.anomaly_lock:
            state.anomaly_hexes.clear()
        state.anomaly_log = []

    _wipe()
    yield
    _wipe()


def _ac(**overrides) -> dict:
    base = {
        "hex": "A1B2C3",
        "lat": 34.85,
        "lon": -82.4,
        "alt_m": 9500.0,
        "heading": 270.0,
        "speed_ms": 230.0,
        "object_type": "aircraft",
        "is_anomalous": False,
        "has_adsb": True,
        "adsb_callsign": "ABC1234",
        "anomaly_event": None,
    }
    base.update(overrides)
    return base


class TestGroundTruthPush:
    def test_stores_trail_and_full_meta(self, client):
        r = client.post("/api/test/ground-truth/push", headers=_KEY,
                        json={"ts_ms": int(time.time() * 1000), "aircraft": [_ac()]})
        assert r.status_code == 200
        assert "a1b2c3" in state.ground_truth_trails
        meta = state.ground_truth_meta["a1b2c3"]
        assert meta["object_type"] == "aircraft"
        assert meta["has_adsb"] is True
        assert meta["adsb_callsign"] == "ABC1234"
        assert meta["anomaly_event"] is None

    def test_old_payload_without_new_keys_defaults(self, client):
        legacy = {k: v for k, v in _ac().items()
                  if k not in ("has_adsb", "adsb_callsign", "anomaly_event")}
        r = client.post("/api/test/ground-truth/push", headers=_KEY,
                        json={"aircraft": [legacy]})
        assert r.status_code == 200
        meta = state.ground_truth_meta["a1b2c3"]
        assert meta["has_adsb"] is False
        assert meta["adsb_callsign"] is None
        assert meta["anomaly_event"] is None

    def test_anomalous_push_flags_hex_and_logs_event(self, client):
        r = client.post("/api/test/ground-truth/push", headers=_KEY,
                        json={"aircraft": [_ac(is_anomalous=True,
                                               anomaly_event="hijack")]})
        assert r.status_code == 200
        assert "a1b2c3" in state.anomaly_hexes
        assert state.ground_truth_meta["a1b2c3"]["anomaly_event"] == "hijack"
        assert any(e["hex"] == "a1b2c3" for e in state.anomaly_log)

    def test_non_anomalous_push_clears_hex(self, client):
        with state.anomaly_lock:
            state.anomaly_hexes.add("a1b2c3")
        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"aircraft": [_ac()]})
        assert "a1b2c3" not in state.anomaly_hexes

    def test_invalid_latlon_skipped(self, client):
        r = client.post("/api/test/ground-truth/push", headers=_KEY,
                        json={"aircraft": [_ac(lat=None)]})
        assert r.status_code == 200
        assert "a1b2c3" not in state.ground_truth_trails

    def test_non_list_aircraft_rejected(self, client):
        r = client.post("/api/test/ground-truth/push", headers=_KEY,
                        json={"aircraft": "nope"})
        assert r.status_code == 400

    def test_missing_api_key_rejected(self, client):
        r = client.post("/api/test/ground-truth/push",
                        json={"aircraft": [_ac()]})
        assert r.status_code == 401
