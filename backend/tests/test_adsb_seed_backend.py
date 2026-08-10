"""Backend-side ADS-B seeding (ADSB_SEED_MODE) — _view_adsb_hex,
confirmed_track_views' adsb_hex export, state._adsb_for_seeding, and
process_one_frame's predictive-tagging / solver-queue plumbing.

The mechanism itself (verification, exclusion, seeded-input emission) is
covered by the lib's test_adsb_seeding.py; these tests pin the backend
plumbing around it, following test_frame_processor.py's conventions.
"""

import time
import types

import pytest
from fastapi.testclient import TestClient
from retina_analytics.association import AssociationRound, predict_observation
from retina_tracker.track import TrackState

from core import state
from main import app
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services.frame_processor import (
    _view_adsb_hex,
    confirmed_track_views,
    process_one_frame,
)

_NODE_CFG = {
    "rx_lat": 34.85, "rx_lon": -82.40, "rx_alt_ft": 1000,
    "tx_lat": 34.9412, "tx_lon": -82.4103, "tx_alt_ft": 2000,
    "fc_hz": 183e6, "beam_width_deg": 90, "max_range_km": 60,
    "beam_azimuth_deg": 45.0,
}


def _make_frame(ts: int = None, n: int = 3) -> dict:
    if ts is None:
        ts = int(time.time() * 1000)
    return {
        "timestamp": ts,
        "delay": [50.0 + i * 2.0 for i in range(n)],
        "doppler": [10.0 + i * 5.0 for i in range(n)],
        "snr": [20.0 + i for i in range(n)],
    }


def _hist_entry(hexn=None):
    return {"timestamp": 0, "delay": 10.0, "doppler": 1.0, "snr": 10.0,
            "adsb": {"hex": hexn} if hexn else None}


def _no_adsb_key_entry():
    """A detection with no "adsb" key at all — pre-correlation shape."""
    return {"timestamp": 0, "delay": 10.0, "doppler": 1.0, "snr": 10.0}


class TestViewAdsbHex:
    def test_all_tagged_history_returns_the_hex(self):
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_hist_entry("ABC123") for _ in range(5)]
        assert _view_adsb_hex(track, hist) == "abc123"

    def test_last_n_untagged_is_the_swap_signature(self):
        """Older entries tagged, the newest 3 (ADSB_VIEW_TAG_FRESH_N)
        untagged — the receiver stopped correlating this hex, which is
        exactly the identity-swap signature, so the tag must not export."""
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_hist_entry("abc123") for _ in range(7)] + [_hist_entry(None) for _ in range(3)]
        assert _view_adsb_hex(track, hist) is None

    def test_two_distinct_hexes_in_window_is_ambiguous(self):
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_hist_entry("abc123")] * 3 + [_hist_entry("def456")] * 3
        assert _view_adsb_hex(track, hist) is None

    def test_track_adsb_hex_mismatch_forces_none(self):
        track = types.SimpleNamespace(adsb_hex="zzzzzz")
        hist = [_hist_entry("abc123") for _ in range(5)]
        assert _view_adsb_hex(track, hist) is None

    def test_track_adsb_hex_matching_is_fine(self):
        track = types.SimpleNamespace(adsb_hex="ABC123")
        hist = [_hist_entry("abc123") for _ in range(5)]
        assert _view_adsb_hex(track, hist) == "abc123"

    def test_hist_without_adsb_keys_returns_none(self):
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_no_adsb_key_entry() for _ in range(5)]
        assert _view_adsb_hex(track, hist) is None


class TestConfirmedTrackViewsAdsbHex:
    def test_view_carries_the_adsb_hex_key(self):
        hist = [_hist_entry("abc123") for _ in range(3)]
        track = types.SimpleNamespace(
            id="trk-1", state_status=TrackState.ACTIVE, adsb_hex=None,
            get_recent_detections=lambda n: hist[-n:],
        )
        tracker = types.SimpleNamespace(tracks=[track])
        views = confirmed_track_views(tracker)
        assert len(views) == 1
        assert views[0]["adsb_hex"] == "abc123"

    def test_untagged_track_carries_none(self):
        hist = [_no_adsb_key_entry() for _ in range(3)]
        track = types.SimpleNamespace(
            id="trk-2", state_status=TrackState.ACTIVE, adsb_hex=None,
            get_recent_detections=lambda n: hist[-n:],
        )
        tracker = types.SimpleNamespace(tracks=[track])
        views = confirmed_track_views(tracker)
        assert views[0]["adsb_hex"] is None


class TestAdsbForSeeding:
    def test_entry_conversion(self):
        state.adsb_aircraft["abc123"] = {
            "hex": "abc123", "lat": 33.9, "lon": -84.6,
            "alt_baro": 10000, "gs": 100, "track": 90,
            "flight": "TST1", "last_seen_ms": 123456,
        }
        out = state._adsb_for_seeding()
        assert "abc123" in out
        e = out["abc123"]
        assert e["alt_m"] == pytest.approx(10000 * 0.3048)
        # gs=100 kn, track=090 deg (east) -> vel_east ~= 100*0.514444, vel_north ~= 0
        assert e["vel_east"] == pytest.approx(51.4444, abs=0.01)
        assert e["vel_north"] == pytest.approx(0.0, abs=1e-6)
        assert e["timestamp_ms"] == 123456

    def test_skips_invalid_latlon(self):
        state.adsb_aircraft["badll"] = {
            "hex": "badll", "lat": float("nan"), "lon": -84.6,
            "alt_baro": 0, "gs": 0, "track": 0, "last_seen_ms": 0,
        }
        out = state._adsb_for_seeding()
        assert "badll" not in out

    def test_keys_stay_lowercase(self):
        # Writers already normalize (normalize_hex_key); the provider
        # trusts the dict key as-is rather than re-deriving it.
        state.adsb_aircraft["lower1"] = {
            "hex": "lower1", "lat": 33.9, "lon": -84.6,
            "alt_baro": 0, "gs": 0, "track": 0, "last_seen_ms": 0,
        }
        out = state._adsb_for_seeding()
        assert list(out.keys()) == ["lower1"]


class TestProcessOneFrameSolverQueue:
    def _drain_queue(self):
        while True:
            try:
                state.solver_queue.get_nowait()
            except Exception:
                break

    def test_adsb_inputs_reach_the_solver_queue(self, monkeypatch):
        """adsb_inputs are already solver-input shaped (see
        association._adsb_seed_round), so process_one_frame must hand them
        to the queue directly, alongside pairs/anchored_inputs."""
        seeded = {
            "initial_guess": {"lat": 35.0, "lon": -82.0, "alt_km": 7.0},
            "initial_velocity": {"vel_east_ms": 100.0, "vel_north_ms": 50.0},
            "measurements": [
                {"node_id": "site-a", "delay_us": 10.0, "doppler_hz": 5.0, "snr": 15.0},
                {"node_id": "site-b", "delay_us": 12.0, "doppler_hz": 4.0, "snr": 14.0},
            ],
            "n_nodes": 2,
            "timestamp_ms": int(time.time() * 1000),
            "adsb_hex": "abc123",
            "chi2_per_dof": None,
            "n_epochs": 2,
            "cv_epochs": [{"t_s": 0.0, "measurements": []}],
            "track_pair_ids": [("t1", "t2")],
            "track_ids": ["t1", "t2"],
            "track_ids_by_node": {"site-a": ["t1"], "site-b": ["t2"]},
        }
        monkeypatch.setattr(
            state.node_associator, "submit_tracks_round",
            lambda *a, **kw: AssociationRound(pairs=[], anchored_inputs=[],
                                              claims=[], adsb_inputs=[seeded]),
        )
        self._drain_queue()

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame("test-adsb-seed-queue", _make_frame(), default)

        s_in, _node_cfgs, _enqueued_at = state.solver_queue.get_nowait()
        assert s_in["adsb_hex"] == "abc123"
        assert s_in["n_nodes"] == 2

    def test_empty_adsb_inputs_add_nothing(self, monkeypatch):
        monkeypatch.setattr(
            state.node_associator, "submit_tracks_round",
            lambda *a, **kw: AssociationRound(pairs=[], anchored_inputs=[],
                                              claims=[], adsb_inputs=[]),
        )
        self._drain_queue()

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame("test-adsb-seed-empty", _make_frame(), default)

        with pytest.raises(Exception):
            state.solver_queue.get_nowait()


class TestPredictiveAttach:
    def _register(self, node_id):
        state.node_associator.register_node(node_id, _NODE_CFG)
        return state.node_associator.node_geometries[node_id]

    def test_frame_without_adsb_gains_index_aligned_list_in_active_mode(self, monkeypatch):
        node_id = "test-predictive-attach"
        geo = self._register(node_id)

        lat, lon, alt_km, ve, vn = 34.88, -82.35, 7.0, 180.0, -90.0
        d0, f0 = predict_observation(geo, lat, lon, alt_km, ve, vn)
        frame = _make_frame()
        frame["delay"] = [d0, d0 + 500.0]   # detection 0 = the plane, 1 = clutter
        frame["doppler"] = [f0, f0 + 500.0]
        frame.pop("adsb", None)

        fixed_states = {
            "abc123": {
                "hex": "abc123", "lat": lat, "lon": lon, "alt_m": alt_km * 1000.0,
                "vel_east": ve, "vel_north": vn, "timestamp_ms": frame["timestamp"],
                "alt_baro": 23000, "gs": 250, "track": 90, "flight": "TST1",
            },
        }
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: fixed_states)
        monkeypatch.setattr(state, "ADSB_SEED_MODE", "active")

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame(node_id, frame, default)

        assert frame["adsb"] is not None
        assert frame["adsb"][0]["hex"] == "abc123"
        assert frame["adsb"][1] is None

    def test_existing_adsb_list_never_overwritten(self, monkeypatch):
        node_id = "test-predictive-existing"
        self._register(node_id)
        frame = _make_frame()
        frame["adsb"] = [{"hex": "already", "lat": 33.9, "lon": -84.6,
                          "alt_baro": 0, "gs": 0, "track": 0}]
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: {})
        monkeypatch.setattr(state, "ADSB_SEED_MODE", "active")

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame(node_id, frame, default)

        assert frame["adsb"] == [{"hex": "already", "lat": 33.9, "lon": -84.6,
                                  "alt_baro": 0, "gs": 0, "track": 0}]

    def test_off_and_shadow_mode_leave_the_frame_untouched(self, monkeypatch):
        node_id = "test-predictive-offshadow"
        self._register(node_id)
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: {
            "abc123": {"hex": "abc123", "lat": 34.88, "lon": -82.35,
                      "alt_m": 7000.0, "vel_east": 0.0, "vel_north": 0.0,
                      "timestamp_ms": int(time.time() * 1000)},
        })
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        for mode in ("off", "shadow"):
            frame = _make_frame()
            frame.pop("adsb", None)
            monkeypatch.setattr(state, "ADSB_SEED_MODE", mode)
            process_one_frame(node_id, frame, default)
            assert "adsb" not in frame or frame["adsb"] is None


class TestAdsbSeedStatusPayload:
    def test_association_status_carries_the_adsb_seed_block(self):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/radar/association/status")
            assert r.status_code == 200
            body = r.json()
            assert "adsb_seed" in body
            assert body["adsb_seed"].keys() == {
                "mode", "rounds", "tagged", "no_state", "gate_rejects",
                "tracklets_excluded", "inputs_emitted",
            }
            assert body["adsb_seed"]["mode"] == state.node_associator.adsb_seed_mode
