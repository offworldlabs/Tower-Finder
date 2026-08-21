"""Shared health-check evaluation.

`compute_health_issues()` inspects live server state and returns a list of
structured issues. It is the single source of truth used by both the
`/api/health` endpoint (synchronous, on-demand) and the health-monitor task
(periodic, owns alerting). Keeping the logic here means the endpoint and the
alerter can never disagree about what "degraded" means.

Each issue is a dict: {"type": str, "severity": "critical"|"warning", "message": str}.
`type` is the stable alert key — alert dedup/cooldown is keyed on it, so it
includes enough context (e.g. the task name) to distinguish distinct problems.
"""

import logging
import os
import resource
import shutil
import sys
import time

import orjson

from core import state

CRITICAL = "critical"
WARNING = "warning"

_NODE_DROPOUT_THRESHOLD = float(os.getenv("NODE_DROPOUT_THRESHOLD", "0.8"))

# Critical pipeline tasks and the max age (s) of their last success before stale.
#
# coverage_constraints is deliberately absent: it is budgeted, so falling behind
# is its designed response to a fleet whose coverage moves faster than it can
# rebuild, not a failure. What must not happen is falling behind *forever*, and
# that is a backlog question, not a last-success one — see below.
_CRITICAL_TASKS = {"frame_processor": 20, "analytics_refresh": 120, "aircraft_flush": 15}

# Queued overlap-grid rebuilds tolerated before the budget is judged too small
# for the fleet. One cycle's budget is a few nodes; a backlog past this has
# taken more than a handful of cycles to work off, so the constraints the grids
# follow are drifting faster than the grids track them.
_COVERAGE_BACKLOG_WARN = int(os.getenv("COVERAGE_BACKLOG_WARN", "12"))


def compute_health_issues() -> list[dict]:
    """Evaluate all health conditions against current state. Empty list == healthy."""
    issues: list[dict] = []

    def add(type_: str, severity: str, message: str) -> None:
        issues.append({"type": type_, "severity": severity, "message": message})

    # Frame queue saturation (>90%)
    q_pct = state.frame_queue.qsize() / max(state.frame_queue.maxsize, 1)
    if q_pct > 0.9:
        add("frame_queue_saturated", CRITICAL, f"Frame queue saturated ({q_pct:.0%})")

    # Critical task staleness
    now = time.time()
    for task, max_age_s in _CRITICAL_TASKS.items():
        last = state.task_last_success.get(task)
        if last is not None and (now - last) > max_age_s:
            add(f"stale_task:{task}", CRITICAL, f"Task {task} stale ({now - last:.0f}s since last success)")

    # Overlap-grid rebuilds queued behind the per-cycle budget
    if state.coverage_rebuild_backlog > _COVERAGE_BACKLOG_WARN:
        add(
            "coverage_rebuild_backlog",
            WARNING,
            f"Coverage grid rebuilds backlogged ({state.coverage_rebuild_backlog} nodes queued)",
        )

    # Solver queue drops (solver can't keep up)
    if state.solver_queue_drops > 0:
        add("solver_queue_drops", WARNING, f"Solver dropped {state.solver_queue_drops} jobs")

    # Disk space (<500 MB free)
    try:
        disk = shutil.disk_usage(state.COVERAGE_STORAGE_DIR)
        free_mb = disk.free / (1024 * 1024)
        if free_mb < 500:
            add("disk_low", CRITICAL, f"Disk low: {free_mb:.0f}MB free")
    except Exception:
        logging.debug("health probe failed", exc_info=True)

    # Process memory (>3 GB on a 4 GB droplet)
    try:
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        rss_mb = rusage.ru_maxrss / 1024 if sys.platform == "linux" else rusage.ru_maxrss / (1024 * 1024)
        if rss_mb > 3000:
            add("memory_high", CRITICAL, f"Memory high: {rss_mb:.0f}MB RSS")
    except Exception:
        logging.debug("health probe failed", exc_info=True)

    # Solver queue backpressure (>50% — early warning before drops)
    solver_q_pct = state.solver_queue.qsize() / max(state.solver_queue.maxsize, 1)
    if solver_q_pct > 0.5:
        add("solver_queue_high", WARNING, f"Solver queue high ({solver_q_pct:.0%})")

    # Solver end-to-end latency (>30s)
    if state.solver_last_latency_s > 30:
        add("solver_latency_high", WARNING, f"Solver latency high ({state.solver_last_latency_s:.0f}s)")

    # Node dropout (active < 80% of peak)
    with state.connected_nodes_lock:
        active_nodes = sum(1 for n in state.connected_nodes.values() if n.get("status") != "disconnected")
    if state.peak_connected_nodes > 10 and active_nodes < state.peak_connected_nodes * _NODE_DROPOUT_THRESHOLD:
        add("node_dropout", CRITICAL, f"Node dropout: {active_nodes}/{state.peak_connected_nodes} active")

    # Zero tracks after warmup (pipeline failure)
    if state.frames_processed > 500 and len(state.adsb_aircraft) == 0 and len(state.multinode_tracks) == 0:
        add("no_active_tracks", CRITICAL, "No active tracks after warmup")

    # Anomaly flood (>50% anomalous = tracker misfiring)
    total_aircraft = len(state.adsb_aircraft)
    if total_aircraft > 10 and len(state.anomaly_hexes) / total_aircraft > 0.5:
        add("anomaly_flood", WARNING, f"Anomaly flood: {len(state.anomaly_hexes)}/{total_aircraft}")

    # Solver accuracy degradation (mean error >10 km).
    # Threshold only on *trusted point fixes* — multi-node and ADS-B-anchored
    # solves. Single-receiver positions (`single_node_ellipse_arc`,
    # `solver_single_node`) are geometry-limited loci that sit tens of km off
    # truth by construction (bistatic root ambiguity / poor GDOP); including
    # them made the overall mean read ~27 km on prod even though multi-node
    # accuracy is ~1 km, masking real solver regressions behind a constant flag.
    try:
        if state.latest_accuracy_bytes and state.latest_accuracy_bytes != b"{}":
            acc = orjson.loads(state.latest_accuracy_bytes)
            by_source = acc.get("by_source") or {}
            untrusted = {"single_node_ellipse_arc", "solver_single_node"}
            trusted = [
                (st.get("n_samples", 0), st.get("mean_km", 0.0))
                for src, st in by_source.items()
                if src not in untrusted
            ]
            n_trusted = sum(n for n, _ in trusted)
            if n_trusted > 20:
                mean_trusted = sum(n * m for n, m in trusted) / n_trusted
                if mean_trusted > 10:
                    add(
                        "solver_accuracy_degraded",
                        WARNING,
                        f"Solver mean error {mean_trusted:.1f}km over {n_trusted} trusted-position samples",
                    )
    except Exception:
        logging.debug("health probe failed", exc_info=True)

    # Fleet-wide miss rate (avg >70% = network effectively blind)
    try:
        if state.latest_missed_detections:
            rates = [v["miss_rate"] for v in state.latest_missed_detections.values() if v.get("in_range", 0) > 0]
            if rates:
                avg = sum(rates) / len(rates)
                if avg > 0.7:
                    add("high_miss_rate", WARNING, f"High miss rate ({avg:.0%})")
    except Exception:
        logging.debug("health probe failed", exc_info=True)

    return issues
