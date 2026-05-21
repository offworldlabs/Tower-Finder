"""Tests for the shared health evaluation + the health-monitor alert cycle."""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services import health  # noqa: E402
from services.health import compute_health_issues  # noqa: E402
from services.tasks import health_monitor  # noqa: E402


class TestComputeHealthIssues:
    def test_healthy_state_has_no_issues(self, monkeypatch):
        # Force every check into its healthy branch.
        monkeypatch.setattr(health.state, "solver_queue_drops", 0)
        monkeypatch.setattr(health.state, "solver_last_latency_s", 0)
        monkeypatch.setattr(health.state, "peak_connected_nodes", 0)
        monkeypatch.setattr(health.state, "frames_processed", 0)
        monkeypatch.setattr(health.state, "adsb_aircraft", {})
        monkeypatch.setattr(health.state, "anomaly_hexes", set())
        monkeypatch.setattr(health.state, "latest_accuracy_bytes", b"{}")
        monkeypatch.setattr(health.state, "latest_missed_detections", {})
        assert compute_health_issues() == []

    def test_solver_drops_flagged_as_warning(self, monkeypatch):
        monkeypatch.setattr(health.state, "solver_queue_drops", 5)
        issues = {i["type"]: i for i in compute_health_issues()}
        assert "solver_queue_drops" in issues
        assert issues["solver_queue_drops"]["severity"] == health.WARNING

    def test_single_node_arc_error_does_not_flag_accuracy(self, monkeypatch):
        # Single-node loci are wildly off (30 km) but multi-node is great (1 km):
        # accuracy must NOT be flagged — the loci aren't trusted point fixes.
        import orjson
        payload = orjson.dumps({
            "n_samples": 140, "mean_km": 27.0,
            "by_source": {
                "single_node_ellipse_arc": {"n_samples": 100, "mean_km": 30.0},
                "multinode_solve": {"n_samples": 40, "mean_km": 1.0},
            },
        })
        monkeypatch.setattr(health.state, "latest_accuracy_bytes", payload)
        assert "solver_accuracy_degraded" not in {i["type"] for i in compute_health_issues()}

    def test_trusted_solve_degradation_flags_accuracy(self, monkeypatch):
        # Multi-node solves themselves degrade past 10 km → flag fires.
        import orjson
        payload = orjson.dumps({
            "n_samples": 60, "mean_km": 14.0,
            "by_source": {"multinode_solve": {"n_samples": 60, "mean_km": 14.0}},
        })
        monkeypatch.setattr(health.state, "latest_accuracy_bytes", payload)
        assert "solver_accuracy_degraded" in {i["type"] for i in compute_health_issues()}


class TestRunCycle:
    def test_fires_alert_per_issue(self, monkeypatch):
        sent = []
        monkeypatch.setattr(health_monitor, "send_alert", lambda t, m, meta=None: sent.append((t, meta)))
        monkeypatch.setattr(health_monitor, "compute_health_issues", lambda: [
            {"type": "disk_low", "severity": "critical", "message": "x"},
            {"type": "solver_queue_drops", "severity": "warning", "message": "y"},
        ])
        active = health_monitor.run_cycle(set())
        assert active == {"disk_low", "solver_queue_drops"}
        types = [t for t, _ in sent]
        assert "disk_low" in types and "solver_queue_drops" in types

    def test_resolved_alert_when_issue_clears(self, monkeypatch):
        sent = []
        monkeypatch.setattr(health_monitor, "send_alert", lambda t, m, meta=None: sent.append(t))
        monkeypatch.setattr(health_monitor, "compute_health_issues", lambda: [])
        active = health_monitor.run_cycle({"disk_low"})
        assert active == set()
        assert "resolved:disk_low" in sent
