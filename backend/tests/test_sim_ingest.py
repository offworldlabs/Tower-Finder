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


class TestStationaryPush:
    """Sub-5.5 m movement must refresh liveness/meta, not vanish the object.

    The old code `continue`d before both the trail append AND the meta update,
    so a hovering object's last trail timestamp aged past the 10 s display GC
    while it was still being pushed every 2 s — the dot blinked out until the
    object cleared 5.5 m, and anomaly transitions during the hover were lost.
    """

    T0_MS = 1_700_000_000_000

    def test_refreshes_liveness_without_duplicate_point(self, client):
        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"ts_ms": self.T0_MS, "aircraft": [_ac()]})
        trail = state.ground_truth_trails["a1b2c3"]
        assert len(trail) == 1

        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"ts_ms": self.T0_MS + 2000, "aircraft": [_ac()]})
        assert len(trail) == 1  # geometry unchanged — no duplicate point
        assert trail[-1][3] == round((self.T0_MS + 2000) / 1000.0, 1)

    def test_meta_and_anomaly_update_while_stationary(self, client):
        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"ts_ms": self.T0_MS, "aircraft": [_ac()]})
        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"ts_ms": self.T0_MS + 2000,
                          "aircraft": [_ac(is_anomalous=True,
                                           anomaly_event="hijack")]})
        meta = state.ground_truth_meta["a1b2c3"]
        assert meta["is_anomalous"] is True
        assert meta["anomaly_event"] == "hijack"
        assert "a1b2c3" in state.anomaly_hexes

    def test_moved_push_still_appends(self, client):
        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"ts_ms": self.T0_MS, "aircraft": [_ac()]})
        client.post("/api/test/ground-truth/push", headers=_KEY,
                    json={"ts_ms": self.T0_MS + 2000,
                          "aircraft": [_ac(lat=34.86)]})
        assert len(state.ground_truth_trails["a1b2c3"]) == 2


class TestSimulationConfig:
    def test_defaults_are_boot_stamped(self, client):
        # 0.0 meant "never pushed": the orchestrator only applies fractions
        # when _updated_at strictly exceeds its last-seen value (starts 0.0),
        # so the world silently ran its own constructor defaults instead.
        r = client.get("/api/simulation/config")
        assert r.status_code == 200
        assert r.json()["_updated_at"] > 0

    def test_counts_split_out_dark_aircraft(self, client):
        state.ground_truth_meta.update({
            "aaa111": {"object_type": "aircraft", "has_adsb": True},
            "obj-00001": {"object_type": "aircraft", "has_adsb": False},
            "bbb222": {"object_type": "drone", "has_adsb": False},
            "ccc333": {"object_type": "anomalous", "has_adsb": True,
                       "is_anomalous": True},
        })
        counts = client.get("/api/simulation/config").json()["ground_truth_counts"]
        assert counts == {"anomalous": 1, "drone": 1, "aircraft": 1,
                          "dark": 1, "total": 4}

    def test_scene_keys_absent_by_default(self, client):
        # Only-if-set pattern (state.py): a fresh backend never ships
        # n_nodes/dual_fraction, so the fleet container falls back to env.
        r = client.get("/api/simulation/config").json()
        assert "n_nodes" not in r
        assert "dual_fraction" not in r

    def test_scene_keys_accepted_and_echoed(self, client):
        r = client.put("/api/simulation/config", json={"n_nodes": 32, "dual_fraction": 0.2})
        assert r.status_code == 200
        assert r.json()["config"]["n_nodes"] == 32
        assert r.json()["config"]["dual_fraction"] == 0.2
        echoed = client.get("/api/simulation/config").json()
        assert echoed["n_nodes"] == 32
        assert echoed["dual_fraction"] == 0.2

    def test_n_nodes_below_range_rejected(self, client):
        r = client.put("/api/simulation/config", json={"n_nodes": 2})
        assert r.status_code == 400

    def test_n_nodes_above_range_rejected(self, client):
        r = client.put("/api/simulation/config", json={"n_nodes": 1000})
        assert r.status_code == 400

    def test_n_nodes_float_rejected(self, client):
        r = client.put("/api/simulation/config", json={"n_nodes": 40.5})
        assert r.status_code == 400

    def test_n_nodes_bool_rejected(self, client):
        # bool is an int subclass in Python — True/False must not sneak
        # through the isinstance(v, int) check as 1/0.
        r = client.put("/api/simulation/config", json={"n_nodes": True})
        assert r.status_code == 400

    def test_dual_fraction_above_range_rejected(self, client):
        r = client.put("/api/simulation/config", json={"dual_fraction": 1.5})
        assert r.status_code == 400

    def test_dual_fraction_below_range_rejected(self, client):
        r = client.put("/api/simulation/config", json={"dual_fraction": -0.1})
        assert r.status_code == 400

    def test_partial_put_leaves_scene_keys_absent(self, client):
        r = client.put("/api/simulation/config", json={"frac_dark": 0.1})
        assert r.status_code == 200
        assert "n_nodes" not in r.json()["config"]
        assert "dual_fraction" not in r.json()["config"]
