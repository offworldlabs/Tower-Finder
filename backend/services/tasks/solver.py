"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import os
import queue
import threading
import time

from core import state

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

# Altitude layers (km) tried when n_nodes ≥ 3.  For an overdetermined system
# (3+ delay equations, 2 unknowns after altitude pinning) only the correct
# altitude layer yields rms_delay ≈ 0; wrong layers give rms > 0, so picking
# the minimum selects the true altitude.  With n=2, bistatic mirror ambiguity
# means rms=0 at every layer for two different positions, so the sweep is
# counterproductive — fall back to the association-provided altitude.
_SOLVER_ALT_LAYERS_KM = [3.0, 6.0, 9.0, 12.0]

# Reject solver results whose RMS delay residual exceeds this value.
# For n≥3 nodes with altitude pinned (overdetermined: 3 equations, 2 unknowns),
# a true association converges with rms_delay ≈ measurement_noise ≈ 1-2 µs.
# False associations (delay measurements from different aircraft) produce
# inconsistent equations → rms_delay = 3-10 µs.
# For n=2, rms=0 at BOTH the true and mirror positions (exactly determined),
# so the threshold can't distinguish mirror from truth — keep generous.
# A single threshold of 3.0 µs cleans up false n≥3 associations while
# letting all n=2 results through (n=2 mirrors always have rms ≈ 0).
_SOLVER_RMS_DELAY_MAX_US = 3.0

# Reject solver results whose RMS Doppler residual exceeds this value.
# Physics: for FM illuminators (fc ≈ 98–108 MHz, λ ≈ 2.8–3.1 m), the maximum
# bistatic Doppler for any real aircraft is 2 × v_max / λ ≈ 2 × 300 / 3.06 ≈ 196 Hz.
# For a true n-node association the solver fits velocity to n Doppler equations;
# with n=2 the system is exactly determined → rms_doppler ≈ 0 regardless.
# With n≥3 it is overdetermined → rms_doppler reflects measurement noise (< 20 Hz).
# False associations (delays/Dopplers from different aircraft) leave large, physically
# unrealisable Doppler residuals (observed: 248 Hz for confirmed false associations).
# Threshold at 200 Hz = max bistatic Doppler + 2% margin; only rejects impossible cases.
_SOLVER_RMS_DOPPLER_MAX_HZ = 200.0


def _solve_best_altitude(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Try each altitude layer; return the result with lowest rms_delay.

    Only called for n_nodes >= 3 where the system is overdetermined and the
    correct altitude uniquely minimises rms_delay.
    """
    base_guess = s_in["initial_guess"]
    best_result: dict | None = None
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
            logging.debug(
                "altitude sweep: z=%.1fkm rms=%.3fµs (best so far=%.3fµs)",
                alt_km, rms, best_rms,
            )
            if rms < best_rms:
                best_rms = rms
                best_result = result

    if best_result is None and last_exc is not None:
        raise last_exc from last_exc

    return best_result



# Maximum age (seconds) of a solver queue item before it is discarded without
# solving.  Items older than this are already stale — the multinode_tracks
# expiry is 60 s, and a solve itself can take a few seconds — so spending CPU
# on them can never produce a visible result.  Raising this number allows a
# deeper backlog but increases latency; lowering it drops items too aggressively.
_SOLVER_MAX_QUEUE_AGE_S = 45.0


def _process_solver_item(item: tuple, solve_fn) -> dict | None:
    """Process a single solver queue entry. Returns the solver result (or None).

    Extracted from the worker loop so the success/failure/latency bookkeeping
    can be unit-tested without spinning up daemon threads.
    """
    s_in, node_cfgs = item[0], item[1]
    enqueued_at: float | None = item[2] if len(item) > 2 else None
    # Discard items that have been waiting too long in the queue.  By the time
    # they are solved, the result's timestamp_ms will be > 60 s old and the
    # entry will be immediately pruned from multinode_tracks — wasting CPU.
    if enqueued_at is not None and time.time() - enqueued_at > _SOLVER_MAX_QUEUE_AGE_S:
        logging.debug(
            "Solver: dropping stale item (age=%.1fs > %.1fs, n_nodes=%d)",
            time.time() - enqueued_at,
            _SOLVER_MAX_QUEUE_AGE_S,
            s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0,
        )
        return None
    n_nodes = s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0
    try:
        if n_nodes >= 3 and "initial_guess" in s_in:
            result = _solve_best_altitude(s_in, node_cfgs, solve_fn)
        else:
            result = solve_fn(s_in, node_cfgs)
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
        rms_doppler = result.get("rms_doppler", 0) or 0
        if rms_doppler > _SOLVER_RMS_DOPPLER_MAX_HZ:
            logging.debug(
                "Solver result rejected: rms_doppler=%.1f Hz > %.1f Hz threshold "
                "(n_nodes=%d, lat=%.3f, lon=%.3f) — physically unrealisable Doppler",
                rms_doppler, _SOLVER_RMS_DOPPLER_MAX_HZ,
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
