"""Additional branch coverage for the /api/health endpoint.

Covers the branches that are not exercised by test_towers_routes.py:
  - solver_queue_drops > 0
  - solver_queue_high
  - solver_latency_high
  - no_active_tracks
  - anomaly_flood
  - solver_accuracy_degraded
  - high_miss_rate
  - node_dropout
"""

import os

import orjson
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from main import app  # noqa: E402
from services import health  # noqa: E402
from services.health import compute_health_issues  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _assert_degraded(r):
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"


class TestHealthDegradedBranches:
    def test_solver_queue_drops(self, client):
        state.solver_queue_drops = 5
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.solver_queue_drops = 0

    def test_solver_queue_backpressure(self, client, monkeypatch):
        # Solver worker threads drain the real queue, so we stub qsize/maxsize
        # rather than filling the queue (which is racy).
        class _StubQueue:
            def qsize(self):
                return 6

            maxsize = 10

        monkeypatch.setattr(state, "solver_queue", _StubQueue())
        _assert_degraded(client.get("/api/health"))

    def test_solver_latency_high(self, client):
        orig = state.solver_last_latency_s
        state.solver_last_latency_s = 45.0
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.solver_last_latency_s = orig

    def test_no_active_tracks(self, client):
        orig_frames = state.frames_processed
        orig_aircraft = dict(state.adsb_aircraft)
        orig_tracks = dict(state.multinode_tracks)
        state.frames_processed = 1000
        state.adsb_aircraft.clear()
        state.multinode_tracks.clear()
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.frames_processed = orig_frames
            state.adsb_aircraft.update(orig_aircraft)
            state.multinode_tracks.update(orig_tracks)

    def test_anomaly_flood(self, client):
        # >10 aircraft, more than half flagged anomalous
        for i in range(20):
            state.adsb_aircraft[f"ac{i:03d}"] = {"hex": f"ac{i:03d}"}
        state.anomaly_hexes.update(f"ac{i:03d}" for i in range(15))
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.adsb_aircraft.clear()
            state.anomaly_hexes.clear()

    def test_solver_accuracy_degraded(self, client):
        orig = state.latest_accuracy_bytes
        state.latest_accuracy_bytes = orjson.dumps(
            {
                "n_samples": 50,
                "mean_km": 25.0,
                "by_source": {"multinode_solve": {"n_samples": 50, "mean_km": 25.0}},
            }
        )
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.latest_accuracy_bytes = orig

    def test_high_miss_rate(self, client):
        # Rates raised from 0.8/0.9 when _HIGH_MISS_RATE_THRESHOLD moved to
        # 0.95: an 0.85 fleet average is inside the band the network reports
        # when it is working, so it no longer degrades and cannot exercise
        # this branch. See TestMissRateThreshold for the boundary itself.
        orig = dict(state.latest_missed_detections)
        state.latest_missed_detections.clear()
        state.latest_missed_detections.update(
            {
                "n1": {"in_range": 10, "miss_rate": 0.97},
                "n2": {"in_range": 20, "miss_rate": 0.99},
            }
        )
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.latest_missed_detections.clear()
            state.latest_missed_detections.update(orig)

    def test_node_dropout(self, client):
        orig_peak = state.peak_connected_nodes
        state.peak_connected_nodes = 100
        # No connected nodes → active_nodes=0, far below 80% threshold
        with state.connected_nodes_lock:
            state.connected_nodes.clear()
        try:
            _assert_degraded(client.get("/api/health"))
        finally:
            state.peak_connected_nodes = orig_peak

    def test_invalid_accuracy_bytes_handled(self, client):
        """Malformed latest_accuracy_bytes should not crash health."""
        orig = state.latest_accuracy_bytes
        state.latest_accuracy_bytes = b"{not json"
        try:
            r = client.get("/api/health")
            assert r.status_code == 200  # exception swallowed
        finally:
            state.latest_accuracy_bytes = orig


class TestMissRateThreshold:
    """The fleet miss rate scores ADS-B aircraft inside each node's
    theoretical beam wedge against what that node's tracker actually
    detected. For passive bistatic radar the wedge is a far larger set than
    what is physically detectable, so a high miss rate is the normal
    operating point rather than a fault.

    Production reported 72% to 94% for as long as the alert channel has
    existed, across both a 27-node synthetic fleet and the three real nodes
    that replaced it, flapping across the old 70% threshold and costing a
    fire-and-resolve pair each crossing. The threshold now sits above that
    observed band, leaving the check as a tripwire for a network that has
    genuinely gone blind rather than a running commentary on the operating
    point. Replacing the measure itself is ClickUp 86cb81gkn.
    """

    def _types(self, rates):
        """Health issue types with `rates` as the per-node miss rates."""
        orig = dict(state.latest_missed_detections)
        state.latest_missed_detections.clear()
        state.latest_missed_detections.update({f"n{i}": {"in_range": 10, "miss_rate": r} for i, r in enumerate(rates)})
        try:
            return {issue["type"] for issue in compute_health_issues()}
        finally:
            state.latest_missed_detections.clear()
            state.latest_missed_detections.update(orig)

    def test_the_observed_operating_band_is_not_degraded(self):
        """The worst fleet average production has ever reported, and the
        band either side of it, must not raise an alert: this is what the
        network looks like when it is working.
        """
        assert "high_miss_rate" not in self._types([0.72, 0.85, 0.94])

    def test_a_blind_network_is_still_degraded(self):
        """The check is kept rather than deleted so that a fleet seeing
        almost nothing still says so."""
        assert "high_miss_rate" in self._types([0.99, 0.98])

    def test_the_threshold_is_the_boundary_it_claims_to_be(self, monkeypatch):
        """Pinned either side of the configured value rather than at some
        comfortable distance from it, so a change to the comparison shows up
        here.
        """
        monkeypatch.setattr(health, "_HIGH_MISS_RATE_THRESHOLD", 0.95)
        assert "high_miss_rate" not in self._types([0.95])
        assert "high_miss_rate" in self._types([0.96])

    def test_the_threshold_is_configurable(self, monkeypatch):
        """Production and staging see different workloads, and staging is
        the only box still running a simulated fleet, so the boundary has to
        be settable per environment without a deploy. Follows
        NODE_DROPOUT_THRESHOLD, the health threshold that is already a
        setting.
        """
        monkeypatch.setattr(health, "_HIGH_MISS_RATE_THRESHOLD", 0.5)
        assert "high_miss_rate" in self._types([0.6])

    def test_nodes_with_nothing_in_range_do_not_drag_the_average_down(self):
        """A node with an empty wedge reports miss_rate 0.0, which would
        otherwise halve the fleet average and mask a genuinely blind
        network. Existing behaviour, pinned because the threshold change
        above makes the average matter more.
        """
        orig = dict(state.latest_missed_detections)
        state.latest_missed_detections.clear()
        state.latest_missed_detections.update(
            {
                "blind": {"in_range": 10, "miss_rate": 0.99},
                "quiet": {"in_range": 0, "miss_rate": 0.0},
            }
        )
        try:
            types = {issue["type"] for issue in compute_health_issues()}
        finally:
            state.latest_missed_detections.clear()
            state.latest_missed_detections.update(orig)

        assert "high_miss_rate" in types
