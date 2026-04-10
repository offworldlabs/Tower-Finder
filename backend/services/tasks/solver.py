"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import os
import queue
import threading
import time

from core import state

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))


def _run_solver_worker():
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread."""
    from retina_geolocator.multinode_solver import solve_multinode
    while True:
        try:
            s_in, node_cfgs = state.solver_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            result = solve_multinode(s_in, node_cfgs)
        except Exception:
            state.task_error_counts["solver"] += 1
            state.solver_failures += 1
            logging.exception("Multinode solver failed")
            result = None
        if result and result.get("success"):
            state.solver_successes += 1
            state.task_last_success["solver"] = time.time()
            for nid in result.get("contributing_node_ids", []):
                state.node_analytics.record_calibration_point(
                    nid, result["lat"], result["lon"]
                )
            key = f"mn-{result['timestamp_ms']}-{result['lat']:.3f}"
            state.multinode_tracks[key] = result


def start_solver_workers():
    """Start N daemon threads that continuously drain the solver queue."""
    for i in range(_N_SOLVER_WORKERS):
        t = threading.Thread(
            target=_run_solver_worker, daemon=True, name=f"solver-{i}",
        )
        t.start()
    logging.info("Started %d multinode solver worker(s)", _N_SOLVER_WORKERS)
