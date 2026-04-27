"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import os
import queue
import threading
import time

from core import state

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

# Altitude layers (km) to try when solving. The association provides an
# initial altitude guess, but it can be wrong by 1-2 grid steps (3-6 km)
# due to bistatic delay ambiguity across altitude layers. We try all 4
# standard layers and pick the result with minimum rms_delay — this always
# selects the layer where the measured delays best match the grid geometry,
# which is the true altitude layer.
_SOLVER_ALT_LAYERS_KM = [3.0, 6.0, 9.0, 12.0]

# Reject solver results whose RMS delay residual exceeds this value.
# With altitude pinned to the CORRECT layer, a clean solve produces
# rms_delay < 1 µs. We keep 5 µs as the upper cutoff (generous for noisy
# measurements) — the multi-altitude selection above handles the filtering
# by picking the minimum rms result.
_SOLVER_RMS_DELAY_MAX_US = 5.0


def _solve_best_altitude(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Run solve_multinode at each standard altitude layer; return best result.

    The association's initial_guess["alt_km"] may be wrong by several km due
    to bistatic delay ambiguity across altitude layers. Trying all 4 standard
    layers (3, 6, 9, 12 km) and picking the one with minimum rms_delay ensures
    we use the altitude where the measured delays best match the geometry —
    i.e., the true altitude layer.
    """
    if "initial_guess" not in s_in:
        # Legacy / test path: no altitude to vary; fall back to single call.
        return solve_fn(s_in, node_cfgs)

    base_guess = s_in["initial_guess"]
    best_result = None
    best_rms = float("inf")
    last_exc: BaseException | None = None

    for alt_km in _SOLVER_ALT_LAYERS_KM:
        s_try = dict(s_in)
        s_try["initial_guess"] = dict(base_guess, alt_km=alt_km)
        try:
            result = solve_fn(s_try, node_cfgs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        if result and result.get("success"):
            rms = result.get("rms_delay", float("inf")) or float("inf")
            if rms < best_rms:
                best_rms = rms
                best_result = result

    # All attempts raised → re-raise so the caller can count the failure.
    if best_result is None and last_exc is not None:
        raise last_exc from last_exc

    return best_result


def _process_solver_item(item: tuple, solve_fn) -> dict | None:
    """Process a single solver queue entry. Returns the solver result (or None).

    Extracted from the worker loop so the success/failure/latency bookkeeping
    can be unit-tested without spinning up daemon threads.
    """
    s_in, node_cfgs = item[0], item[1]
    enqueued_at: float | None = item[2] if len(item) > 2 else None
    try:
        result = _solve_best_altitude(s_in, node_cfgs, solve_fn)
    except Exception:
        state.task_error_counts["solver"] += 1
        state.solver_failures += 1
        logging.exception("Multinode solver failed")
        result = None
    if result and result.get("success"):
        rms_delay = result.get("rms_delay", 0) or 0
        if rms_delay > _SOLVER_RMS_DELAY_MAX_US:
            logging.debug(
                "Solver result rejected: rms_delay=%.1f µs > %.1f µs threshold "
                "(n_nodes=%d, lat=%.3f, lon=%.3f)",
                rms_delay, _SOLVER_RMS_DELAY_MAX_US,
                result.get("n_nodes", 0), result.get("lat", 0), result.get("lon", 0),
            )
            state.solver_failures += 1
            return result
        state.solver_successes += 1
        with state.solver_latency_lock:
            state.solver_total_solved += 1
        if enqueued_at is not None:
            latency = time.time() - enqueued_at
            with state.solver_latency_lock:
                state.solver_last_latency_s = latency
                state.solver_total_latency_s += latency
            if latency > 30.0:
                logging.warning("Solver latency high: %.1fs for %d-node candidate",
                                latency, s_in.get("n_nodes", 0))
                from services.alerting import send_alert
                send_alert(
                    "solver_latency_high",
                    f"Solver latency {latency:.1f}s — pipeline may be falling behind",
                    {"latency_s": round(latency, 1), "n_nodes": s_in.get("n_nodes", 0)},
                )
        state.task_last_success["solver"] = time.time()
        for nid in result.get("contributing_node_ids", []):
            state.node_analytics.record_calibration_point(
                nid, result["lat"], result["lon"]
            )
        key = f"mn-{result['timestamp_ms']}-{result['lat']:.3f}"
        state.multinode_tracks[key] = result
    return result


def _run_solver_worker():
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread."""
    from retina_geolocator.multinode_solver import solve_multinode
    while True:
        try:
            item = state.solver_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        _process_solver_item(item, solve_multinode)


def start_solver_workers():
    """Start N daemon threads that continuously drain the solver queue."""
    for i in range(_N_SOLVER_WORKERS):
        t = threading.Thread(
            target=_run_solver_worker, daemon=True, name=f"solver-{i}",
        )
        t.start()
    logging.info("Started %d multinode solver worker(s)", _N_SOLVER_WORKERS)
