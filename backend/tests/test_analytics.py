"""Tests for analytics subsystem — trust, reputation, coverage, manager, suggestions."""

import time

import pytest

from analytics import (
    TrustScoreState, AdsReportEntry, DetectionAreaState, NodeMetrics,
    NodeReputation, HistoricalCoverageMap, NodeAnalyticsManager,
    YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM,
)
from analytics.cross_node import coverage_suggestion


# ── Trust Score & Reputation ─────────────────────────────────────────────────


class TestTrustScore:
    def test_initial_score_zero(self):
        ts = TrustScoreState(node_id="test-node")
        assert ts.score == 0.0

    def test_good_sample_gives_1(self):
        ts = TrustScoreState(node_id="test-node")
        ts.add_sample(AdsReportEntry(
            timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
            measured_delay=10.5, measured_doppler=51.0,
            adsb_hex="abc123", adsb_lat=33.9, adsb_lon=-84.6,
        ))
        assert ts.score == 1.0

    def test_bad_sample_lowers_score(self):
        ts = TrustScoreState(node_id="test-node")
        ts.add_sample(AdsReportEntry(
            timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
            measured_delay=10.5, measured_doppler=51.0,
            adsb_hex="abc123", adsb_lat=33.9, adsb_lon=-84.6,
        ))
        ts.add_sample(AdsReportEntry(
            timestamp_ms=2000, predicted_delay=10.0, predicted_doppler=50.0,
            measured_delay=20.0, measured_doppler=100.0,
            adsb_hex="abc124", adsb_lat=33.9, adsb_lon=-84.6,
        ))
        assert ts.score == 0.5


class TestYagiConstants:
    def test_beam_width(self):
        assert YAGI_BEAM_WIDTH_DEG == 41.0

    def test_max_range(self):
        assert YAGI_MAX_RANGE_KM == 50.0


class TestDetectionArea:
    def test_defaults(self):
        da = DetectionAreaState(node_id="test-da")
        assert da.beam_width_deg == 41.0
        assert da.max_range_km == 50.0

    def test_update(self):
        da = DetectionAreaState(node_id="test-da")
        da.update(15.0, 80.0)
        da.update(25.0, -40.0)
        assert da.delay_range == (15.0, 25.0)
        assert da.doppler_range == (-40.0, 80.0)
        assert da.n_detections == 2


class TestNodeReputation:
    def test_initial_reputation(self):
        rep = NodeReputation(node_id="good-node")
        assert rep.reputation == 1.0
        assert not rep.blocked

    def test_good_trust_keeps_high(self):
        rep = NodeReputation(node_id="good-node")
        rep.evaluate_trust(0.9)
        assert rep.reputation >= 1.0

    def test_bad_actor_gets_blocked(self):
        rep = NodeReputation(node_id="bad-node")
        for _ in range(15):
            rep.evaluate_trust(0.05)
        assert rep.blocked
        assert "Reputation" in rep.block_reason or "Trust" in rep.block_reason

    def test_unblock(self):
        rep = NodeReputation(node_id="bad-node")
        for _ in range(15):
            rep.evaluate_trust(0.05)
        rep.unblock()
        assert not rep.blocked
        assert rep.reputation == 0.3

    def test_stale_heartbeat_penalty(self):
        rep = NodeReputation(node_id="stale-hb")
        rep.evaluate_heartbeat(time.time() - 600)
        assert rep.reputation < 1.0

    def test_high_detection_rate_penalty(self):
        rep = NodeReputation(node_id="high-rate")
        rep.evaluate_detection_rate(100.0)
        assert rep.reputation < 1.0


# ── Historical Coverage Map ──────────────────────────────────────────────────


class TestHistoricalCoverageMap:
    def test_empty_initially(self):
        cov = HistoricalCoverageMap(node_id="test-cov")
        assert cov.n_grid_cells == 0

    def test_add_detections(self):
        cov = HistoricalCoverageMap(node_id="test-cov")
        for i in range(30):
            cov.add_detection(33.9 + i * 0.01, -84.6 + i * 0.005,
                              alt_km=8.0, snr=15.0, delay_error=0.5)
        assert len(cov.entries) == 30
        assert cov.n_grid_cells > 0
        assert cov.coverage_area_km2 > 0

    def test_beam_width_estimate(self):
        cov = HistoricalCoverageMap(node_id="test-cov")
        for i in range(30):
            cov.add_detection(33.9 + i * 0.01, -84.6 + i * 0.005,
                              alt_km=8.0, snr=15.0, delay_error=0.5)
        bw = cov.estimate_beam_width()
        assert bw is not None
        assert bw <= 180.0

    def test_coverage_grid(self):
        cov = HistoricalCoverageMap(node_id="test-cov")
        for i in range(30):
            cov.add_detection(33.9 + i * 0.01, -84.6 + i * 0.005,
                              alt_km=8.0, snr=15.0, delay_error=0.5)
        grid = cov.get_coverage_grid()
        assert isinstance(grid, list)
        assert all("count" in c for c in grid)

    def test_summary(self):
        cov = HistoricalCoverageMap(node_id="test-cov")
        for i in range(5):
            cov.add_detection(33.9 + i * 0.01, -84.6 + i * 0.005,
                              alt_km=8.0, snr=15.0, delay_error=0.5)
        s = cov.summary()
        assert s["node_id"] == "test-cov"
        assert "grid_cells" in s


# ── Manager Integration ──────────────────────────────────────────────────────


class TestNodeAnalyticsManager:
    @pytest.fixture()
    def mgr(self):
        m = NodeAnalyticsManager()
        m.register_node("node-A", {
            "rx_lat": 33.939, "rx_lon": -84.651,
            "tx_lat": 33.756, "tx_lon": -84.331,
            "fc_hz": 195e6,
        })
        m.register_node("node-B", {
            "rx_lat": 34.0, "rx_lon": -84.5,
            "tx_lat": 33.8, "tx_lon": -84.2,
            "fc_hz": 195e6,
        })
        return m

    def test_reputation_registered(self, mgr):
        assert "node-A" in mgr.reputations

    def test_coverage_map_registered(self, mgr):
        assert "node-B" in mgr.coverage_maps

    def test_node_not_blocked(self, mgr):
        assert not mgr.is_node_blocked("node-A")

    def test_record_frame_accepted(self, mgr):
        accepted = mgr.record_detection_frame("node-A", {
            "delay": [15.0, 20.0], "doppler": [50.0, -30.0], "snr": [12.0, 8.0],
        })
        assert accepted is True

    def test_adsb_populates_coverage(self, mgr):
        mgr.record_adsb_correlation("node-A", AdsReportEntry(
            timestamp_ms=1000, predicted_delay=15.0, predicted_doppler=50.0,
            measured_delay=15.2, measured_doppler=50.5,
            adsb_hex="abc123", adsb_lat=34.0, adsb_lon=-84.5,
        ))
        assert len(mgr.coverage_maps["node-A"].entries) == 1

    def test_summary_fields(self, mgr):
        s = mgr.get_node_summary("node-A")
        assert "reputation" in s
        assert "coverage_map" in s

    def test_evaluate_reputations(self, mgr):
        mgr.evaluate_reputations()  # should not raise

    def test_cross_node_analysis(self, mgr):
        cross = mgr.get_cross_node_analysis()
        assert "blocked_nodes" in cross

    def test_blocked_node_rejects_frames(self, mgr):
        mgr.reputations["node-A"].blocked = True
        mgr.reputations["node-A"].block_reason = "test"
        assert mgr.is_node_blocked("node-A")
        rejected = mgr.record_detection_frame("node-A", {
            "delay": [10.0], "doppler": [20.0], "snr": [5.0],
        })
        assert rejected is False

    def test_admin_unblock(self, mgr):
        mgr.reputations["node-A"].blocked = True
        mgr.reputations["node-A"].block_reason = "test"
        mgr.unblock_node("node-A")
        assert not mgr.is_node_blocked("node-A")


# ── Track Quality — Gap Stats ────────────────────────────────────────────────


class TestTrackQuality:
    def test_gap_stats(self):
        metrics = NodeMetrics(node_id="gap-test")
        ts_list = [1000 + i * 10000 for i in range(5)]
        ts_list.append(1000 + 200_000)
        for t in ts_list:
            metrics.record_frame({"delay": [1.0], "doppler": [1.0], "snr": [5.0], "timestamp": t})

        gap = metrics.gap_stats
        assert gap["gap_count"] >= 1
        assert gap["max_gap_s"] >= 100
        assert gap["continuity_ratio"] <= 1.0

    def test_summary_has_track_quality(self):
        metrics = NodeMetrics(node_id="gap-test")
        for t in [1000, 2000, 3000]:
            metrics.record_frame({"delay": [1.0], "doppler": [1.0], "snr": [5.0], "timestamp": t})
        s = metrics.summary()
        assert "track_quality" in s
        assert "gap_count" in s["track_quality"]


# ── Coverage Suggestion ──────────────────────────────────────────────────────


class TestCoverageSuggestion:
    @pytest.fixture()
    def areas(self):
        return [
            DetectionAreaState(node_id="cs-A", rx_lat=33.939, rx_lon=-84.651,
                               beam_azimuth_deg=135, beam_width_deg=41, max_range_km=50),
            DetectionAreaState(node_id="cs-B", rx_lat=34.05, rx_lon=-84.4,
                               beam_azimuth_deg=210, beam_width_deg=41, max_range_km=50),
        ]

    def test_default_returns_list(self, areas):
        result = coverage_suggestion(areas, center_lat=34.0, center_lon=-84.5)
        assert isinstance(result, list)

    def test_strategy_1_with_trust(self, areas):
        ts_a = TrustScoreState(node_id="cs-A")
        ts_a.add_sample(AdsReportEntry(
            timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
            measured_delay=10.2, measured_doppler=50.5,
            adsb_hex="x", adsb_lat=34.0, adsb_lon=-84.5,
        ))
        ts_b = TrustScoreState(node_id="cs-B")
        ts_b.add_sample(AdsReportEntry(
            timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
            measured_delay=10.1, measured_doppler=50.2,
            adsb_hex="y", adsb_lat=34.0, adsb_lon=-84.5,
        ))
        result = coverage_suggestion(areas, 34.0, -84.5,
                                     trust_scores={"cs-A": ts_a, "cs-B": ts_b})
        assert isinstance(result, list)

    def test_strategy_2_with_solver_rms(self, areas):
        rms = [5.0, 4.8, 4.7, 4.65, 4.62, 4.60, 4.59, 4.58, 4.575, 4.572]
        result = coverage_suggestion(areas, 34.0, -84.5,
                                     solver_rms_history=rms)
        assert isinstance(result, list)
