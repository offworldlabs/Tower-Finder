"""Known-target claiming (KNOWN_LANE_MODE) — services/known_claiming.py,
state.known_claims, and process_one_frame's binding-mode exclusion.

Follows test_adsb_seed_backend.py's conventions: real associator geometry
registered per test, ADS-B states injected through state.adsb_aircraft or a
monkeypatched provider, process_one_frame exercised with a captured pipeline.
"""

import sys
import time
import types

import pytest
from retina_analytics.association import predict_observation

from config.constants import FT_TO_M
from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services import known_claiming as kc
from services.frame_processor import process_one_frame

_NODE_CFG = {
    "rx_lat": 34.85,
    "rx_lon": -82.40,
    "rx_alt_ft": 1000,
    "tx_lat": 34.9412,
    "tx_lon": -82.4103,
    "tx_alt_ft": 2000,
    "fc_hz": 183e6,
    "beam_width_deg": 90,
    "max_range_km": 60,
    "beam_azimuth_deg": 45.0,
}

_NODE_ID = "test-known-claiming"

# A position/velocity well inside _NODE_CFG's beam, reused across tests.
_LAT, _LON, _ALT_KM = 34.88, -82.35, 7.0
_ALT_BARO_FT = _ALT_KM * 1000.0 / FT_TO_M


def _register(node_id=_NODE_ID):
    state.node_associator.register_node(node_id, _NODE_CFG)
    return state.node_associator.node_geometries[node_id]


def _cache_state(hexn, ts_ms, lat=_LAT, lon=_LON, alt_baro=_ALT_BARO_FT, gs=0, track=0):
    state.adsb_aircraft[hexn] = {
        "hex": hexn,
        "lat": lat,
        "lon": lon,
        "alt_baro": alt_baro,
        "gs": gs,
        "track": track,
        "flight": "TST1",
        "last_seen_ms": ts_ms,
    }


def _frame(ts_ms, delays, dopplers, adsb=None):
    f = {
        "timestamp": ts_ms,
        "delay": list(delays),
        "doppler": list(dopplers),
        "snr": [20.0] * len(delays),
    }
    if adsb is not None:
        f["adsb"] = adsb
    return f


def _stationary_pred(geo):
    """Predicted observation for the shared test aircraft at rest."""
    return predict_observation(geo, _LAT, _LON, _ALT_BARO_FT * FT_TO_M / 1000.0, 0.0, 0.0)


class TestGlobalAssignment:
    def test_one_to_one_beats_greedy_on_a_crossing_pair(self, monkeypatch):
        """Two aircraft one gate-width apart, two detections: det0 scores best
        against A but det1 is feasible ONLY against A.  Greedy (the
        associate_detections_to_adsb shape) awards A to det0 on its better
        single score and leaves det1 unclaimable; the global assignment takes
        the only complete matching (det0→B, det1→A) instead."""
        _register()
        ts = int(time.time() * 1000)
        # Keyed by lat so the fake survives the (no-op, vel=0) dead-reckoning.
        _cache_state("aaa111", ts, lat=10.0)
        _cache_state("bbb222", ts, lat=20.0)
        monkeypatch.setattr(
            kc,
            "predict_observation",
            lambda geo, lat, lon, alt_km, ve=0.0, vn=0.0, vu=0.0: (100.0, 0.0) if lat < 15 else (104.0, 20.0),
        )
        # det0: A (0.5 µs, 2 Hz) score 0.13 / B (3.5, 18) score 1.07 — both feasible
        # det1: A (1.5, 6) score 0.39 / B (2.5, 26) — 26 Hz > 25 gate, infeasible
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [100.5, 101.5], [2.0, -6.0]))

        assert claimed == {0, 1}
        assert state.known_claims["aaa111"][-1]["delay_us"] == 101.5  # det1 → A
        assert state.known_claims["bbb222"][-1]["delay_us"] == 100.5  # det0 → B

    def test_ungated_detection_stays_dark(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("aaa111", ts)
        pd, pf = _stationary_pred(geo)
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd + 500.0], [pf + 500.0]))
        assert claimed == set()
        assert state.known_claims == {}

    def test_no_geometry_claims_nothing(self):
        ts = int(time.time() * 1000)
        _cache_state("aaa111", ts)
        claimed = kc.claim_known_targets("test-unregistered-node", _frame(ts, [50.0], [10.0]))
        assert claimed == set()
        assert state.known_claims == {}


class TestGateVsFixAge:
    """The allowance doubles linearly toward the 45 s age cap: a residual the
    base gate rejects on a fresh fix passes once dead-reckoning drift has had
    time to accumulate — and past the cap the state stops being a candidate
    at all.  vel=0 states so aging moves the gate, not the prediction."""

    def _claim_at_age(self, geo, age_s):
        ts = int(time.time() * 1000)
        _cache_state("aged01", ts - int(age_s * 1000))
        pd, pf = _stationary_pred(geo)
        # 12 µs: outside the 10 µs base gate, inside 10 × (1 + 40/45) ≈ 18.9.
        return kc.claim_known_targets(_NODE_ID, _frame(ts, [pd + 12.0], [pf]))

    def test_fresh_fix_keeps_the_base_gate(self):
        geo = _register()
        assert self._claim_at_age(geo, 0.0) == set()

    def test_aged_fix_widens_the_gate(self):
        geo = _register()
        assert self._claim_at_age(geo, 40.0) == {0}

    def test_past_the_age_cap_is_no_candidate_at_all(self):
        geo = _register()
        assert self._claim_at_age(geo, 50.0) == set()


class TestNodeTagPrecedence:
    def test_node_supplied_tag_wins_over_a_better_cached_match(self):
        """The node's own correlation is authoritative: even a cached hex whose
        prediction matches the detection exactly must not displace the tag."""
        geo = _register()
        ts = int(time.time() * 1000)
        pd, pf = _stationary_pred(geo)
        _cache_state("cached1", ts)  # predicts (pd, pf) — a perfect score-0 match
        tag = {"hex": "TAGGED1", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
        frame = _frame(ts, [pd], [pf], adsb=[tag])

        claimed = kc.claim_known_targets(_NODE_ID, frame)

        assert claimed == {0}
        assert "tagged1" in state.known_claims  # normalize_hex_key'd
        assert "cached1" not in state.known_claims
        # And the invariant that predates this stage: the node's list is
        # never rewritten by the backend.
        assert frame["adsb"] == [tag]

    def test_tag_is_not_regated(self):
        """A tag whose residual would fail the assignment gates still claims:
        trust covers which-echo, the gates exist for the backend's own
        guesses."""
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "far001", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [9999.0], [999.0], adsb=[tag]))
        assert claimed == {0}
        rec = state.known_claims["far001"][-1]
        assert rec["delay_us"] == 9999.0
        assert rec["pred_delay_us"] != 9999.0  # prediction recorded for the residual path


class TestContention:
    def _global(self, key, ts_ms, n_nodes, solve_count):
        state.multinode_tracks[key] = {
            "lat": _LAT,
            "lon": _LON,
            "alt_m": _ALT_BARO_FT * FT_TO_M,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "vel_up": 0.0,
            "timestamp_ms": ts_ms,
            "n_nodes": n_nodes,
            "solve_count": solve_count,
        }

    def test_claim_over_an_established_dark_track_is_contested(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("cont01", ts)
        self._global("mn-dark-cont1", ts - 5000, n_nodes=3, solve_count=5)
        pd, pf = _stationary_pred(geo)

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert claimed == {0}
        assert state.known_claims["cont01"][-1]["contested"] is True
        assert state.known_claim_contentions == 1
        # The dark track is flagged against, never suppressed.
        assert "mn-dark-cont1" in state.multinode_tracks

    def test_ineligible_dark_track_does_not_contest(self):
        """A one-shot unconfirmed solve fails claim_eligible, so it is not an
        "established" track and must not mark real claims contested."""
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("cont02", ts)
        self._global("mn-dark-cont2", ts - 5000, n_nodes=2, solve_count=1)
        pd, pf = _stationary_pred(geo)

        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert state.known_claims["cont02"][-1]["contested"] is False
        assert state.known_claim_contentions == 0


class TestModesInProcessOneFrame:
    """Shadow records but the dark lane sees the full frame; binding strips
    the claimed indices from what the pipeline processes — and only from
    that: the original frame (archive, ADS-B extraction) stays whole."""

    def _run(self, monkeypatch, mode):
        _register()
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", mode)
        ts = int(time.time() * 1000)
        tag = {"hex": "bind01", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
        frame = _frame(ts, [50.0, 52.0], [10.0, 15.0], adsb=[tag, None])

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        seen = []
        monkeypatch.setattr(default, "process_frame", lambda f: seen.append(f))
        process_one_frame(_NODE_ID, frame, default)
        return frame, seen[0]

    def test_binding_strips_claimed_indices_from_the_pipeline_frame(self, monkeypatch):
        frame, pframe = self._run(monkeypatch, "binding")
        assert pframe["delay"] == [52.0]
        assert pframe["doppler"] == [15.0]
        assert pframe["snr"] == [20.0]
        assert pframe["adsb"] == [None]
        # Original untouched — it still feeds archive + ADS-B extraction.
        assert len(frame["delay"]) == 2 and frame["adsb"][0]["hex"] == "bind01"
        assert state.known_claims_bound == 1
        assert state.known_claims_made == 1

    def test_shadow_records_the_claim_but_changes_nothing_downstream(self, monkeypatch):
        frame, pframe = self._run(monkeypatch, "shadow")
        assert pframe is frame  # same object, not even a copy
        assert "bind01" in state.known_claims
        assert state.known_claims_made == 1
        assert state.known_claims_bound == 0

    def test_off_computes_nothing(self, monkeypatch):
        frame, pframe = self._run(monkeypatch, "off")
        assert pframe is frame
        assert state.known_claims == {}
        assert state.known_claims_made == 0


class TestRegistryContract:
    def test_claim_record_shape(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("shape1", ts, gs=100, track=90)
        pd, pf = predict_observation(geo, _LAT, _LON, _ALT_BARO_FT * FT_TO_M / 1000.0, 100 * 0.514444, 0.0)
        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        rec = state.known_claims["shape1"][-1]
        assert set(rec.keys()) == {
            "node_id",
            "delay_us",
            "doppler_hz",
            "pred_delay_us",
            "pred_doppler_hz",
            "ts_ms",
            "adsb_fix",
            "contested",
        }
        assert set(rec["adsb_fix"].keys()) == {"lat", "lon", "alt_baro", "gs", "track", "fix_ts_ms"}
        assert rec["node_id"] == _NODE_ID
        assert rec["ts_ms"] == ts
        # Assignment-path claims carry the REPORTED fix and its own timestamp.
        assert rec["adsb_fix"]["lat"] == _LAT
        assert rec["adsb_fix"]["fix_ts_ms"] == ts
        assert state.known_claims["shape1"].maxlen == state.KNOWN_CLAIMS_PER_HEX_MAX

    def test_reset_for_tests_clears_registry_and_counters(self):
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "reset1", "lat": _LAT, "lon": _LON, "alt_baro": 0, "gs": 0, "track": 0}
        kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]))
        assert state.known_claims and state.known_claims_made == 1

        state._reset_for_tests()

        assert state.known_claims == {}
        assert state.known_claims_made == 0
        assert state.known_claim_contentions == 0
        assert state.known_claims_bound == 0


class TestNodeBiasHook:
    def test_missing_module_degrades_silently(self, monkeypatch):
        """services.node_bias is optional at import time — a tree without it
        (or a rollback that drops it) must claim normally, and the ImportError
        verdict must be cached once.  Its presence in THIS tree means absence
        has to be simulated: None in sys.modules makes the import raise, and
        the package attribute (bound by conftest's reset imports) must go too
        or `from services import node_bias` never reaches the import system."""
        import services

        monkeypatch.delattr(services, "node_bias")
        monkeypatch.setitem(sys.modules, "services.node_bias", None)
        kc._reset_for_tests()
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "nobias", "lat": _LAT, "lon": _LON, "alt_baro": 0, "gs": 0, "track": 0}
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]))
        assert claimed == {0}
        assert kc._node_bias() is None
        assert kc._node_bias_unavailable is True

    def _install_fake(self, monkeypatch, untrusted=frozenset(), recorder=None):
        import services

        fake = types.SimpleNamespace(
            get_untrusted_hexes=lambda: untrusted,
            record_claim_residual=recorder or (lambda *a: None),
        )
        monkeypatch.setitem(sys.modules, "services.node_bias", fake)
        monkeypatch.setattr(services, "node_bias", fake, raising=False)
        kc._reset_for_tests()  # forget any cached ImportError from earlier tests

    def test_residuals_are_recorded_signed(self, monkeypatch):
        recorded = []
        self._install_fake(monkeypatch, recorder=lambda *a: recorded.append(a))
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("resid1", ts)
        pd, pf = _stationary_pred(geo)

        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd + 2.0], [pf - 5.0]))

        assert len(recorded) == 1
        node_id, hexn, d_res, f_res, ts_out = recorded[0]
        assert (node_id, hexn, ts_out) == (_NODE_ID, "resid1", ts)
        # Signed, measured minus predicted — bias direction matters downstream.
        assert d_res == pytest.approx(2.0)
        assert f_res == pytest.approx(-5.0)

    def test_untrusted_hexes_are_skipped_on_both_paths(self, monkeypatch):
        self._install_fake(monkeypatch, untrusted={"LIAR01", "liar02"})
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("liar01", ts)  # would gate perfectly on the assignment path
        pd, pf = _stationary_pred(geo)
        tag = {"hex": "liar02", "lat": _LAT, "lon": _LON, "alt_baro": 0, "gs": 0, "track": 0}

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd, 999.0], [pf, 99.0], adsb=[None, tag]))

        assert claimed == set()
        assert state.known_claims == {}


class TestStripAndGc:
    def test_strip_all_claimed_leaves_empty_lists(self):
        f = _frame(1000, [1.0, 2.0], [3.0, 4.0])
        out = kc.strip_claimed_detections(f, {0, 1})
        assert out["delay"] == [] and out["doppler"] == [] and out["snr"] == []
        assert f["delay"] == [1.0, 2.0]

    def test_non_list_keys_pass_through(self):
        f = _frame(1000, [1.0], [3.0])
        f["adsb"] = None
        out = kc.strip_claimed_detections(f, {0})
        assert out["adsb"] is None
        assert out["timestamp"] == 1000

    def test_feed_gc_prunes_stale_hexes_only(self):
        from collections import deque

        from services.feed_gc import prune_stale_stores

        now = time.time()
        state.known_claims["fresh1"] = deque([{"ts_ms": int((now - 10) * 1000)}], maxlen=4)
        state.known_claims["stale1"] = deque([{"ts_ms": int((now - 500) * 1000)}], maxlen=4)
        prune_stale_stores(now)
        assert "fresh1" in state.known_claims
        assert "stale1" not in state.known_claims
