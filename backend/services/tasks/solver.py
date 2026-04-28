"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import math
import os
import queue
import threading
import time

from core import state

# ── Beam-coverage geometry helpers ────────────────────────────────────────────
# Used to reject solver results whose position falls outside the detection beam
# of a contributing node (ghost-solution disambiguation for n=2 bistatic pairs).

_R_EARTH_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return _R_EARTH_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg_geo(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _in_node_beam(lat: float, lon: float, node_cfg: dict) -> bool:
    """Return True iff (lat, lon) is within the node's detection beam."""
    rx_lat = float(node_cfg.get("rx_lat") or 0)
    rx_lon = float(node_cfg.get("rx_lon") or 0)
    max_range = float(node_cfg.get("max_range_km") or 50)
    if _haversine_km(rx_lat, rx_lon, lat, lon) > max_range:
        return False
    beam_az = float(node_cfg.get("beam_azimuth_deg") or 0)
    beam_w = float(node_cfg.get("beam_width_deg") or 41)
    bearing = _bearing_deg_geo(rx_lat, rx_lon, lat, lon)
    angle_diff = abs((bearing - beam_az + 180) % 360 - 180)
    return angle_diff <= beam_w / 2


_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

# Altitude layers (km) tried when n_nodes ≥ 3.  For an overdetermined system
# (3+ delay equations, 2 unknowns after altitude pinning) only the correct
# altitude layer yields rms_delay ≈ 0; wrong layers give rms > 0, so picking
# the minimum selects the true altitude.  Layers match the association grid
# (7, 9, 11) so that the correct altitude is always ≤ 1 km from a layer for
# commercial aviation (cruise altitude 7–12 km).
_SOLVER_ALT_LAYERS_KM = [7.0, 9.0, 11.0]

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


def _sweep_altitudes(s_in: dict, node_cfgs: dict, solve_fn,
                     layers_km: list[float], metric: str) -> dict | None:
    """Try each altitude layer; return the result with lowest value of `metric`.

    Args:
        metric: Solver output key to minimise across layers.  Currently always
                'rms_delay' (used by n≥3 where the overdetermined system gives
                rms≈0 at the correct altitude).
    """
    base_guess = s_in["initial_guess"]
    best_result: dict | None = None
    best_rms = float("inf")
    last_exc: BaseException | None = None

    for alt_km in layers_km:
        s_try = dict(s_in)
        s_try["initial_guess"] = dict(base_guess, alt_km=alt_km)
        try:
            result = solve_fn(s_try, node_cfgs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        if result and result.get("success"):
            rms_raw = result.get(metric)
            rms = float("inf") if rms_raw is None else float(rms_raw)
            logging.debug(
                "altitude sweep: z=%.1fkm %s=%.3f (best so far=%.3f)",
                alt_km, metric, rms, best_rms,
            )
            if rms < best_rms:
                best_rms = rms
                best_result = result

    if best_result is None and last_exc is not None:
        raise last_exc

    return best_result


def _solve_best_altitude(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude sweep for n≥3: pick by minimum rms_delay."""
    return _sweep_altitudes(s_in, node_cfgs, solve_fn, _SOLVER_ALT_LAYERS_KM, "rms_delay")


def _solve_best_altitude_n2(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude solve for n=2: use the initial_guess altitude from association.

    For n=2 the solver state [x, y, vx, vy, vz] with altitude fixed is:
    - Exactly determined by the 2 delay equations for (x, y)
    - Underdetermined for (vx, vy, vz): 2 Doppler equations, 3 unknowns

    Both rms_delay and rms_doppler are ≈0 at every altitude layer (the solver
    always finds a zero-residual solution within bounds).  Neither metric can
    discriminate altitude.

    The initial_guess.alt_km from association.py is set to the delay-residual
    weighted mean of all candidate altitudes in the group.  When the correct
    altitude layer has smaller delay residuals it is upweighted; when all layers
    tie (high altitude ambiguity), the mean falls back to ≈(7+9+11)/3 = 9 km,
    which covers the typical commercial aviation cruise band (7–12 km).
    """
    return solve_fn(s_in, node_cfgs)



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
    age_s = time.time() - enqueued_at if enqueued_at is not None else 0.0
    if enqueued_at is not None and age_s > _SOLVER_MAX_QUEUE_AGE_S:
        logging.debug(
            "Solver: dropping stale item (age=%.1fs > %.1fs, n_nodes=%d)",
            age_s,
            _SOLVER_MAX_QUEUE_AGE_S,
            s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0,
        )
        return None
    n_nodes = s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0
    try:
        if "initial_guess" not in s_in:
            result = solve_fn(s_in, node_cfgs)
        elif n_nodes >= 3:
            result = _solve_best_altitude(s_in, node_cfgs, solve_fn)
        else:
            result = _solve_best_altitude_n2(s_in, node_cfgs, solve_fn)
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
        # Reject solutions outside the beam coverage of contributing nodes.
        # For n=2 the solver has two geometric solutions (two bistatic ellipse
        # intersections); the ghost intersection typically falls outside one of
        # the node beams.  This check rejects it without needing Doppler data.
        # Skipped when node_cfgs lacks beam info (cfg is None) — safe fallback.
        contributing_ids = result.get("contributing_node_ids", [])
        if contributing_ids and isinstance(node_cfgs, dict):
            for nid in contributing_ids:
                cfg = node_cfgs.get(nid)
                if cfg and not _in_node_beam(result["lat"], result["lon"], cfg):
                    logging.debug(
                        "Solver result rejected: outside beam of node %s "
                        "(lat=%.3f lon=%.3f beam_az=%.0f beam_w=%.0f range_km=%.0f)",
                        nid, result["lat"], result["lon"],
                        float(cfg.get("beam_azimuth_deg") or 0),
                        float(cfg.get("beam_width_deg") or 41),
                        float(cfg.get("max_range_km") or 50),
                    )
                    state.solver_failures += 1
                    return None
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
