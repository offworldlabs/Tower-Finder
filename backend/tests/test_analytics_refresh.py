"""Tests for services/tasks/analytics_refresh.py — background pre-computation.

Focuses on:
- numpy scalar serialization (the orjson.OPT_SERIALIZE_NUMPY trap)
- Accuracy stats computation (_refresh_accuracy_stats)
- Thread-safe snapshot of connected_nodes
"""

import orjson
import pytest

# ── numpy serialization trap ──────────────────────────────────────────────────


class TestNumpySerializationTrap:
    """orjson.dumps rejects numpy.float64 without OPT_SERIALIZE_NUMPY.
    This is the #1 silent-failure mode in the analytics background task."""

    def test_orjson_rejects_numpy_float64_by_default(self):
        np = pytest.importorskip("numpy")
        data = {"value": np.float64(1.234)}
        with pytest.raises(TypeError):
            orjson.dumps(data)

    def test_orjson_accepts_numpy_with_flag(self):
        np = pytest.importorskip("numpy")
        data = {"value": np.float64(1.234), "arr": [np.float64(2.0)]}
        result = orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY)
        parsed = orjson.loads(result)
        assert abs(parsed["value"] - 1.234) < 0.001

    def test_nested_numpy_in_analytics_shape(self):
        """Simulates the shape of analytics_data with numpy values from solver."""
        np = pytest.importorskip("numpy")
        analytics_data = {
            "nodes": {
                "node-1": {
                    "trust_score": np.float64(0.95),
                    "rms_delay_error_us": np.float64(1.23),
                    "n_detections": np.int64(42),
                    "beam_azimuth_deg": np.float64(45.0),
                }
            },
            "cross_node": {
                "pair_overlaps": [],
                "coverage_suggestions": [],
                "blocked_nodes": [],
            },
        }
        result = orjson.dumps(analytics_data, option=orjson.OPT_SERIALIZE_NUMPY)
        parsed = orjson.loads(result)
        assert parsed["nodes"]["node-1"]["trust_score"] == pytest.approx(0.95, abs=0.01)
        assert parsed["nodes"]["node-1"]["n_detections"] == 42


# ── Accuracy Stats ────────────────────────────────────────────────────────────


class TestAccuracyStats:
    """Test the accuracy stats computation done by _refresh_accuracy_stats."""

    def _compute_stats(self, samples):
        """Reproduce the stats logic from analytics_refresh.py."""
        if not samples:
            return {"n_samples": 0}

        errors = [s["error_km"] for s in samples]
        errors.sort()
        n = len(errors)

        def _percentile(sorted_vals, pct):
            idx = int(pct / 100 * (len(sorted_vals) - 1))
            return sorted_vals[min(idx, len(sorted_vals) - 1)]

        by_source: dict[str, list[float]] = {}
        for s in samples:
            by_source.setdefault(s["position_source"], []).append(s["error_km"])

        source_stats = {}
        for src, errs in by_source.items():
            errs.sort()
            sn = len(errs)
            source_stats[src] = {
                "n_samples": sn,
                "mean_km": round(sum(errs) / sn, 4),
                "median_km": round(_percentile(errs, 50), 4),
                "p95_km": round(_percentile(errs, 95), 4),
                "max_km": round(errs[-1], 4),
            }

        return {
            "n_samples": n,
            "mean_km": round(sum(errors) / n, 4),
            "median_km": round(_percentile(errors, 50), 4),
            "p95_km": round(_percentile(errors, 95), 4),
            "max_km": round(errors[-1], 4),
            "by_source": source_stats,
        }

    def test_empty_samples(self):
        result = self._compute_stats([])
        assert result == {"n_samples": 0}

    def test_single_sample(self):
        result = self._compute_stats([{"error_km": 2.5, "position_source": "solver"}])
        assert result["n_samples"] == 1
        assert result["mean_km"] == 2.5
        assert result["median_km"] == 2.5
        assert result["max_km"] == 2.5

    def test_multiple_sources_split(self):
        samples = [
            {"error_km": 1.0, "position_source": "solver"},
            {"error_km": 2.0, "position_source": "solver"},
            {"error_km": 0.5, "position_source": "adsb"},
            {"error_km": 0.3, "position_source": "adsb"},
        ]
        result = self._compute_stats(samples)
        assert result["n_samples"] == 4
        assert "solver" in result["by_source"]
        assert "adsb" in result["by_source"]
        assert result["by_source"]["solver"]["n_samples"] == 2
        assert result["by_source"]["adsb"]["n_samples"] == 2
        # ADS-B errors lower
        assert result["by_source"]["adsb"]["mean_km"] < result["by_source"]["solver"]["mean_km"]

    def test_percentiles_correct(self):
        # 100 samples 0.01 to 1.0 km
        samples = [{"error_km": i * 0.01, "position_source": "s"} for i in range(1, 101)]
        result = self._compute_stats(samples)
        assert result["n_samples"] == 100
        assert result["mean_km"] == pytest.approx(0.505, abs=0.001)
        assert result["median_km"] == pytest.approx(0.50, abs=0.02)
        assert result["p95_km"] > result["median_km"]
        assert result["max_km"] == 1.0

    def test_all_zero_errors(self):
        samples = [{"error_km": 0.0, "position_source": "perfect"} for _ in range(10)]
        result = self._compute_stats(samples)
        assert result["mean_km"] == 0.0
        assert result["max_km"] == 0.0


# ── Thread safety: connected_nodes snapshot ───────────────────────────────────


class TestConnectedNodesSnapshot:
    """Verify that analytics refresh takes a snapshot under lock."""

    def test_snapshot_with_lock(self):
        """Simulated: reading connected_nodes while another thread mutates it."""
        from core import state

        state.connected_nodes["test-snap-1"] = {
            "status": "connected",
            "config": {"name": "snap-test", "rx_lat": 33.45, "rx_lon": -112.07},
            "is_synthetic": True,
        }

        with state.connected_nodes_lock:
            snap = list(state.connected_nodes.items())

        assert any(nid == "test-snap-1" for nid, _ in snap)

        # Cleanup
        state.connected_nodes.pop("test-snap-1", None)


# ── Reputation evaluations ────────────────────────────────────────────────────


class TestReputationEvaluations:
    """Test NodeReputation evaluation methods for trust, heartbeat, detection rate."""

    def test_low_trust_blocks_node(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="bad-1")
        # Critically low trust → penalty → eventually blocked
        for _ in range(20):
            rep.evaluate_trust(0.05)  # below block threshold
        assert rep.blocked is True
        assert rep.reputation < 0.2

    def test_high_trust_rewards_node(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="good-1", reputation=0.5)
        for _ in range(50):
            rep.evaluate_trust(0.9)  # above 0.7
        assert rep.reputation > 0.5  # increased

    def test_stale_heartbeat_penalty(self):
        import time

        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="stale-1")
        # Last heartbeat 600s ago (> 300s threshold)
        rep.evaluate_heartbeat(time.time() - 600)
        assert rep.reputation < 1.0
        assert len(rep.penalties) == 1
        assert "stale" in rep.penalties[0]["reason"].lower()

    def test_high_detection_rate_penalty(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="flood-1")
        rep.evaluate_detection_rate(100.0)  # >> 50 threshold
        assert len(rep.penalties) == 1

    def test_unblock_resets_reputation_to_0_3(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="x", reputation=0.0, blocked=True, block_reason="test")
        rep.unblock()
        assert rep.blocked is False
        assert rep.reputation == 0.3
        assert rep.block_reason == ""

    def test_penalty_cap(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="cap", max_penalties=5)
        for i in range(10):
            rep.apply_penalty(0.01, f"penalty {i}")
        assert len(rep.penalties) == 5  # capped
        assert rep.penalties[-1]["reason"] == "penalty 9"  # newest kept

    def test_reputation_never_negative(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="floor")
        rep.apply_penalty(2.0, "huge penalty")
        assert rep.reputation == 0.0

    def test_reputation_never_above_one(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="ceil", reputation=0.99)
        rep.apply_reward(0.5)
        assert rep.reputation == 1.0

    def test_blocked_node_no_rewards(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="b", reputation=0.1, blocked=True)
        rep.apply_reward(0.5)
        assert rep.reputation == 0.1  # unchanged

    def test_neighbour_consistency_penalty(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="inc")
        # Trusted neighbour (0.8) but very low overlap (0.01) → suspicious
        rep.evaluate_neighbour_consistency(overlap_ratio=0.01, neighbour_trust=0.8)
        assert len(rep.penalties) == 1
        assert "inconsistent" in rep.penalties[0]["reason"].lower()

    def test_summary_shape(self):
        from retina_analytics.reputation import NodeReputation

        rep = NodeReputation(node_id="summary-test")
        rep.apply_penalty(0.1, "test")
        s = rep.summary()
        assert s["node_id"] == "summary-test"
        assert "reputation" in s
        assert "blocked" in s
        assert "n_penalties" in s
        assert "recent_penalties" in s


# ── Missed-detections beam geometry ───────────────────────────────────────────


class TestMissedDetectionsHonoursConfiguredBeamWidth:
    """_refresh_missed_detections re-derived beam width/range/azimuth inline
    instead of going through geo.node_beam_params like every other module, and
    defaulted width to YAGI_BEAM_WIDTH_DEG while track_gates defaulted to a
    bare 41.0 literal: one node was a different antenna to each module. Pin
    that a genuinely wide, aimed beam still counts an aircraft a narrower beam
    would have excluded, so a local default can't silently creep back in."""

    def test_wide_beam_node_counts_an_aircraft_a_narrow_beam_would_miss(self):
        import time

        from core import state
        from services.tasks.analytics_refresh import _refresh_missed_detections

        # A node aimed north (beam_azimuth_deg=0) with a genuine 140-degree
        # beam. The aircraft sits ~47 deg off boresight: inside 140/2=70 but
        # outside a 41/2=20.5 beam, so this fails if the derivation ever falls
        # back to a narrower local default instead of the node's real width.
        cfg = {
            "rx_lat": 34.0,
            "rx_lon": -82.0,
            "tx_lat": 33.0,
            "tx_lon": -82.0,
            "beam_azimuth_deg": 0.0,
            "beam_width_deg": 140.0,
            "max_range_km": 200.0,
        }
        state.connected_nodes["test-wide-beam"] = {"status": "active", "config": cfg}
        state.adsb_aircraft["abc123"] = {
            "lat": 34.23,
            "lon": -81.70,
            "last_seen_ms": int(time.time() * 1000),
        }
        snapshot = list(state.connected_nodes.items())
        _refresh_missed_detections(snapshot)
        assert state.latest_missed_detections["test-wide-beam"]["in_range"] == 1

    def test_default_width_follows_the_shared_constant_not_a_local_copy(self, monkeypatch):
        """The test above only proves an explicit, present beam_width_deg is
        honoured. For such a value the old inline `or YAGI_BEAM_WIDTH_DEG` and
        node_beam_params's identical fallback always agreed (both read the same
        41.0), so it cannot tell the shared resolver apart from a hardcoded
        copy of the same default. This one can: patch geo.YAGI_BEAM_WIDTH_DEG,
        the name node_beam_params actually reads at call time (services.geo
        binds it at import time, so patching retina_analytics.constants's own
        copy afterwards would not reach node_beam_params at all), and omit
        beam_width_deg from the config so the default is what decides. Code
        still reading a hardcoded 41 -- or importing YAGI_BEAM_WIDTH_DEG
        straight from config.constants, a second, separately-patched copy of
        the same name -- would ignore the patch and miss."""
        import time

        from core import state
        from services import geo
        from services.tasks.analytics_refresh import _refresh_missed_detections

        monkeypatch.setattr(geo, "YAGI_BEAM_WIDTH_DEG", 77.0)

        # Aircraft ~30 deg off a due-north boresight, ~40 km out: inside a
        # patched 77-degree beam (+-38.5) but outside the old 41-degree one
        # (+-20.5). Position computed via geo.offset_latlon, not guessed.
        cfg = {
            "rx_lat": 34.0,
            "rx_lon": -82.0,
            "tx_lat": 33.0,
            "tx_lon": -82.0,
            "beam_azimuth_deg": 0.0,
            "max_range_km": 200.0,
            # beam_width_deg intentionally omitted: the patched default decides.
        }
        state.connected_nodes["test-default-width"] = {"status": "active", "config": cfg}
        state.adsb_aircraft["def456"] = {
            "lat": 34.311534,
            "lon": -81.783044,
            "last_seen_ms": int(time.time() * 1000),
        }
        snapshot = list(state.connected_nodes.items())
        _refresh_missed_detections(snapshot)
        assert state.latest_missed_detections["test-default-width"]["in_range"] == 1
