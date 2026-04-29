"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import math
import os
import queue
import threading
import time
from collections import deque

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
    """Return True iff (lat, lon) is within the node's detection beam.

    Beam azimuth priority:
      1. Explicit ``beam_azimuth_deg`` in the config.
      2. Derived as (bearing from RX to TX) + 90° — the broadside direction for
         a Yagi antenna, matching the formula used in InterNodeAssociator.register_node.
      3. Skip the bearing check entirely if TX position is also missing.
    """
    rx_lat = float(node_cfg.get("rx_lat") or node_cfg.get("lat") or 0)
    rx_lon = float(node_cfg.get("rx_lon") or node_cfg.get("lon") or 0)
    max_range = float(node_cfg.get("max_range_km") or 50)
    if _haversine_km(rx_lat, rx_lon, lat, lon) > max_range:
        return False
    # Determine beam azimuth.
    if "beam_azimuth_deg" in node_cfg:
        beam_az: float | None = float(node_cfg["beam_azimuth_deg"])
    elif node_cfg.get("tx_lat") and node_cfg.get("tx_lon"):
        tx_lat = float(node_cfg["tx_lat"])
        tx_lon = float(node_cfg["tx_lon"])
        beam_az = (_bearing_deg_geo(rx_lat, rx_lon, tx_lat, tx_lon) + 90.0) % 360.0
    else:
        beam_az = None  # unknown beam direction — skip bearing check
    if beam_az is None:
        return True
    beam_w = float(node_cfg.get("beam_width_deg") or 41)
    bearing = _bearing_deg_geo(rx_lat, rx_lon, lat, lon)
    angle_diff = abs((bearing - beam_az + 180) % 360 - 180)
    return angle_diff <= beam_w / 2


_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

# Altitude layers (km) tried when n_nodes ≥ 3.  For an overdetermined system
# (3+ delay equations, 2 unknowns after altitude pinning) only the correct
# altitude layer yields rms_delay ≈ 0; wrong layers give rms > 0, so picking
# the minimum selects the true altitude.  Layers match the association grid
# (5, 7, 9, 11) so that the correct altitude is always ≤ 1 km from a layer for
# commercial aviation (cruise altitude 5–12 km).  The 5 km layer covers
# aircraft at 3–7 km that were previously unserved by the [7,9,11] set;
# beam-coverage checks handle any TX-ghost artefacts from low altitude layers.
_SOLVER_ALT_LAYERS_KM = [5.0, 7.0, 9.0, 11.0]

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

# Reject n=2 solver results whose position moved more than this many km from
# the initial_guess supplied by the association layer.
#
# For n=2 (exactly-determined position), the LM solver can converge to the
# false bistatic ellipse intersection (the mirror point) instead of the true
# one.  The beam-coverage check above catches mirror points that land outside
# a node's detection beam; this check catches the remainder.
#
# The association grid step is 3 km, so the initial_guess is within ~3 km of
# the true aircraft position (the delay-residual-minimising grid point is
# always close to the real bistatic intersection).  A good solve therefore
# stays within a few km of the initial_guess.  Mirror points are typically
# 15–50 km from the true position, meaning they are ≥12 km from an
# initial_guess that was placed near the truth.
#
# Threshold: with the ADS-B position override in find_associations(),
# the initial_guess is within ~100 m of the true aircraft position.
# Displacement from initial_guess therefore approximates the position error.
# With σ_delay = 0.1 µs the displacement = GDOP × 0.1 × 0.3 km.
# 2.0 km → GDOP ≤ 67 (reasonable bistatic geometry).
# Mirror-point ghosts land 15–50 km away and are safely rejected.
_N2_MAX_DISPLACEMENT_KM = 2.0


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
    """Altitude sweep for n≥3: pick by minimum rms_delay.

    If the initial_guess already carries an ADS-B altitude (not one of the fixed
    grid layers), include it in the sweep so the correct exact altitude is tried.
    """
    ig_alt = s_in.get("initial_guess", {}).get("alt_km")
    if ig_alt is not None and ig_alt not in _SOLVER_ALT_LAYERS_KM:
        layers = sorted(set(_SOLVER_ALT_LAYERS_KM + [round(float(ig_alt), 3)]))
    else:
        layers = _SOLVER_ALT_LAYERS_KM
    return _sweep_altitudes(s_in, node_cfgs, solve_fn, layers, "rms_delay")


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


# ── Multi-epoch EWMA position smoother (n=2) ─────────────────────────────────
# For n=2 TDOA, each solve has position error σ_pos = GDOP × σ_delay.  By
# accumulating K successive solver positions for the same aircraft (identified
# by ICAO hex) and dead-reckoning earlier positions forward to the current
# solve time using ADS-B velocity, we average K independent noise realisations
# and reduce the effective σ_pos by 1/√K.
#
# K=3 frames (every ~40 s) reduces mean error by ~√3 = 1.73×.
# Dead-reckoning uses ADS-B ground-speed + track (always available in
# simulation; state.adsb_aircraft is populated by the frame processor for every
# ADS-B-tagged detection).  If ADS-B velocity is missing the smoother returns
# the raw solver result unchanged.

# Per-hex rolling buffer: hex → deque of (lat, lon, timestamp_s)
_MN_POS_HISTORY: dict[str, deque] = {}
_MN_POS_HISTORY_LOCK = threading.Lock()
_MN_HISTORY_K = 3   # number of past frames to average (including current)
_MN_DR_MAX_AGE_S = 160.0  # discard history entries older than 4 frame intervals


def _ewma_smooth_n2(result: dict, adsb_hex: str | None) -> dict:
    """Apply dead-reckoned multi-epoch averaging to an n=2 solver result.

    Dead-reckon previous solve positions to the current solve timestamp using
    ADS-B ground-speed and track, then return the simple mean of the
    dead-reckoned history and the current solve.  Thread-safe via lock.

    Returns the original result dict if hex is unknown, ADS-B velocity is
    unavailable, or there is no prior history yet.
    """
    if not adsb_hex:
        return result

    r_lat = result["lat"]
    r_lon = result["lon"]
    r_ts = result.get("timestamp_ms", 0) / 1000.0

    # Retrieve ADS-B velocity for dead-reckoning.
    adsb = state.adsb_aircraft.get(adsb_hex)
    if not adsb:
        with _MN_POS_HISTORY_LOCK:
            _MN_POS_HISTORY.setdefault(adsb_hex, deque(maxlen=_MN_HISTORY_K)).append(
                (r_lat, r_lon, r_ts)
            )
        return result

    gs_knots = float(adsb.get("gs", 0) or 0)
    track_deg = float(adsb.get("track", 0) or 0)
    gs_kms = gs_knots * 0.514444 / 1000.0   # knots → km/s
    # Geographic track: 0° = North, 90° = East.
    vel_north_kms = gs_kms * math.cos(math.radians(track_deg))
    vel_east_kms  = gs_kms * math.sin(math.radians(track_deg))

    with _MN_POS_HISTORY_LOCK:
        hist = _MN_POS_HISTORY.setdefault(adsb_hex, deque(maxlen=_MN_HISTORY_K))

        # Dead-reckon each past position forward to the current solve time
        # and collect valid points (not too stale, not too far after dr).
        cos_lat = math.cos(math.radians(r_lat)) or 1e-9
        km_per_deg_lat = 111.32
        km_per_deg_lon = 111.32 * cos_lat
        positions: list[tuple[float, float]] = [(r_lat, r_lon)]

        for prev_lat, prev_lon, prev_ts in hist:
            dt = r_ts - prev_ts
            if dt <= 0 or dt > _MN_DR_MAX_AGE_S:
                continue  # skip future or stale entries
            dr_lat = prev_lat + vel_north_kms * dt / km_per_deg_lat
            dr_lon = prev_lon + vel_east_kms  * dt / km_per_deg_lon
            positions.append((dr_lat, dr_lon))

        # Push current position before averaging (so it is included next time).
        hist.append((r_lat, r_lon, r_ts))

    if len(positions) < 2:
        return result  # no usable history yet—return raw solve

    avg_lat = sum(p[0] for p in positions) / len(positions)
    avg_lon = sum(p[1] for p in positions) / len(positions)

    smoothed = dict(result)
    smoothed["lat"] = round(avg_lat, 6)
    smoothed["lon"] = round(avg_lon, 6)
    logging.debug(
        "n=2 EWMA: hex=%s K=%d raw=(%.4f,%.4f) → smooth=(%.4f,%.4f)",
        adsb_hex, len(positions), r_lat, r_lon, avg_lat, avg_lon,
    )
    return smoothed


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
        # For n=2: reject if the solution drifted more than _N2_MAX_DISPLACEMENT_KM
        # from the initial_guess.  Mirror-point convergences (the false bistatic
        # ellipse intersection not caught by the beam filter) move the solution
        # 15–50 km from the initial_guess, while good solves stay within ~5 km.
        if n_nodes == 2 and "initial_guess" in s_in:
            _ig = s_in["initial_guess"]
            _ig_lat = _ig.get("lat")
            _ig_lon = _ig.get("lon")
            if _ig_lat and _ig_lon:
                _disp_km = _haversine_km(
                    float(_ig_lat), float(_ig_lon),
                    result["lat"], result["lon"],
                )
                if _disp_km > _N2_MAX_DISPLACEMENT_KM:
                    logging.debug(
                        "n=2 result rejected: %.1f km from initial_guess "
                        "(lat=%.3f lon=%.3f) — likely mirror-point convergence",
                        _disp_km, result["lat"], result["lon"],
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
        # For n=2 solves with a known ADS-B hex, apply dead-reckoned multi-epoch
        # EWMA smoothing to reduce single-frame measurement noise (reduces mean
        # position error by ~√K where K is the number of history frames used).
        if n_nodes == 2:
            _adsb_hex = s_in.get("adsb_hex") if isinstance(s_in, dict) else None
            result = _ewma_smooth_n2(result, _adsb_hex)
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
