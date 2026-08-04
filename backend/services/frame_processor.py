"""Detection frame processing + aircraft JSON builder.

Contains the synchronous per-frame pipeline that runs in a thread pool
and the combined aircraft.json builder used by the flush task.
"""

import logging
import math
import threading
import time
from collections import defaultdict, deque

from retina_tracker.track import TrackState

from config.constants import (
    ARC_MOTION_MAX_SPEED_MS,
    ARC_REFRESH_S,
    ARCHIVE_BATCH_MAX,
    ARCHIVE_FLUSH_INTERVAL_S,
    DISPLAY_STALE_TRACK_S,
    GATE_MAX_HOLD_S,
    GT_DISPLAY_STALE_S,
    GT_REFRESH_S,
    IMPLAUSIBLE_SPEED_MS,
    N2_TRACK_HISTORY_MAX,
    STALE_TRACK_S,
    TRAIL_STALE_S,
)
from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from services.geo import (
    C_KM_US,
    bearing_deg,
    enu_km,
    haversine_km,
    offset_latlon,
    offset_latlon_m,
)
from services.id_utils import multinode_hex_from_key
from services.storage import archive_detections

# ── Archive batching ──────────────────────────────────────────────────────────
# Instead of writing every frame to disk immediately (slow I/O in the hot path),
# collect frames in memory and flush them periodically from a background task.
_archive_buffer: dict[str, list[dict]] = defaultdict(list)
_archive_buffer_lock = threading.Lock()
_ARCHIVE_FLUSH_INTERVAL = ARCHIVE_FLUSH_INTERVAL_S
_ARCHIVE_BATCH_MAX = ARCHIVE_BATCH_MAX
# Hard cap on buffer growth when writes fail repeatedly. Beyond this we drop
# the oldest frames to bound memory; data loss is preferable to OOM.
_ARCHIVE_BUFFER_HARD_CAP = ARCHIVE_BATCH_MAX * 2


def _flush_archive_node(node_id: str):
    """Write buffered frames for one node to disk in a single call.

    Frames are retained in the buffer until the disk write succeeds — this
    way a transient write failure (disk full, permissions) doesn't silently
    drop data on the floor. The buffer is capped at _ARCHIVE_BUFFER_HARD_CAP
    to prevent unbounded growth if the disk stays unhealthy.
    """
    with _archive_buffer_lock:
        frames = list(_archive_buffer.get(node_id, []))
    if not frames:
        return
    try:
        archive_detections(node_id, frames)
    except Exception:
        # Write failed — keep frames buffered for the next cycle, but enforce
        # a memory cap so a sustained outage can't OOM the process.
        with _archive_buffer_lock:
            buf = _archive_buffer.get(node_id, [])
            if len(buf) > _ARCHIVE_BUFFER_HARD_CAP:
                dropped = len(buf) - _ARCHIVE_BUFFER_HARD_CAP
                _archive_buffer[node_id] = buf[-_ARCHIVE_BUFFER_HARD_CAP:]
                logging.warning(
                    "Archive flush failing for %s; dropped %d oldest frames "
                    "(buffer capped at %d)",
                    node_id, dropped, _ARCHIVE_BUFFER_HARD_CAP,
                )
            else:
                logging.warning(
                    "Archive flush failed for %s (%d frames retained)",
                    node_id, len(buf),
                )
        return
    # Write succeeded — drop the prefix we just persisted, keeping any frames
    # that arrived during the write (which were appended after our snapshot).
    n_written = len(frames)
    with _archive_buffer_lock:
        buf = _archive_buffer.get(node_id, [])
        remaining = buf[n_written:]
        if remaining:
            _archive_buffer[node_id] = remaining
        else:
            _archive_buffer.pop(node_id, None)


def flush_all_archive_buffers():
    """Flush every node's buffered frames. Called from the background task."""
    with _archive_buffer_lock:
        node_ids = list(_archive_buffer.keys())
    for nid in node_ids:
        _flush_archive_node(nid)


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_hex_key(hex_code: str) -> str:
    return str(hex_code or "").strip().lower()


# Was a flat-earth approximation at 111.0 km/deg — 0.175% short of the
# spherical form used by every gate this feeds.
position_distance_km = haversine_km


# ── Aircraft de-duplication ───────────────────────────────────────────────────
# Preference when several entries describe one aircraft. A multinode fix is
# constrained by >=2 receivers; a single-node ellipse arc is only an ambiguity
# locus, so it loses.
_DEDUP_SOURCE_RANK = {
    "multinode_solve": 0,
    "solver_adsb_seed": 1,
    "solver_single_node": 2,
    "single_node_ellipse_arc": 3,
}
# Deliberately tighter than the 6 km solver association gate. Observed duplicate
# spread is ~1 km, while real IFR separation is 5 nm (~9.3 km) laterally or
# 1000 ft vertically — so this collapses duplicates with margin to spare while
# staying well clear of merging two genuinely separate aircraft. Under-merging
# leaves a cosmetic double; over-merging would hide real traffic.
_DEDUP_PROXIMITY_KM = 3.0
_DEDUP_ALT_GATE_FT = 2000.0


def _looks_like_same_aircraft(a: dict, b: dict) -> bool:
    """Proximity + altitude test for entries with no shared identity."""
    for k in ("lat", "lon"):
        if not isinstance(a.get(k), (int, float)) or not isinstance(b.get(k), (int, float)):
            return False
    if position_distance_km(a["lat"], a["lon"], b["lat"], b["lon"]) > _DEDUP_PROXIMITY_KM:
        return False
    alt_a, alt_b = a.get("alt_baro"), b.get("alt_baro")
    if isinstance(alt_a, (int, float)) and isinstance(alt_b, (int, float)):
        if abs(alt_a - alt_b) > _DEDUP_ALT_GATE_FT:
            return False
    return True


def dedup_aircraft(aircraft: list[dict]) -> list[dict]:
    """Collapse entries that describe the same physical aircraft.

    Two entries can describe one aircraft and never share a `hex`: a multinode
    solve is named mn<sha>, a single-node arc by ICAO or pr<id>. The `seen_hex`
    set in the builder is exact string equality, so it can never merge them —
    which is why one target could render as several icons.

    Grouped by ground_truth_hex where available, else by proximity. The
    ground-truth path is simulation-only (resolve_ground_truth_hex reads
    state.ground_truth_trails), so the proximity fallback is what carries real
    hardware.
    """
    groups: list[list[dict]] = []
    by_truth: dict[str, int] = {}

    for ac in aircraft:
        gt = ac.get("ground_truth_hex")
        if gt:
            idx = by_truth.get(gt)
            if idx is None:
                by_truth[gt] = len(groups)
                groups.append([ac])
            else:
                groups[idx].append(ac)
            continue
        for g in groups:
            if _looks_like_same_aircraft(ac, g[0]):
                g.append(ac)
                break
        else:
            groups.append([ac])

    out: list[dict] = []
    for g in groups:
        if len(g) == 1:
            out.append(g[0])
            continue
        g.sort(key=lambda x: _DEDUP_SOURCE_RANK.get(x.get("position_source"), 99))
        winner = g[0]
        # Carry every contributing node onto the survivor so node filtering
        # (aircraft_flush.filter_payload_to_nodes) and the frontend's
        # node-highlight still see this aircraft under each node that saw it.
        nodes: list[str] = []
        for m in g:
            for nid in [m.get("node_id"), *(m.get("contributing_node_ids") or [])]:
                if nid and nid not in nodes:
                    nodes.append(nid)
        if nodes:
            winner["contributing_node_ids"] = nodes
        out.append(winner)

    return out


def append_track_history(hex_code: str, lat: float, lon: float, alt_ft: float, ts: float):
    """Append a position to the rolling track history for a hex."""
    if hex_code not in state.track_histories:
        state.track_histories[hex_code] = deque(maxlen=state.TRACK_HISTORY_MAX)
    hist = state.track_histories[hex_code]
    if hist:
        dlat = abs(hist[-1][0] - lat)
        dlon = abs(hist[-1][1] - lon)
        if dlat < 0.00005 and dlon < 0.00005:
            return
    hist.append([round(lat, 6), round(lon, 6), round(alt_ft, 0), round(ts, 1)])


# Minimum displacement (metres) before a new arc-motion sample is appended.
# Smaller than this counts as "same position" — sub-100 m drift on a stationary
# arc midpoint isn't a real velocity signal.
_ARC_MOTION_MIN_M = 100.0
# Number of distinct positions retained per track.  Long enough to span a few
# bistatic frames (~40 s apart) so velocity estimation can average over
# 60–120 s windows without holding state forever.
_ARC_MOTION_LOG_MAX = 6


def _record_arc_motion(ac_hex: str, lat: float, lon: float, ts: float) -> None:
    """Log distinct emitted positions for later velocity estimation.

    Only entries that have moved > _ARC_MOTION_MIN_M from the last logged
    position are appended — repeated emits at the same arc midpoint between
    detections would otherwise drown out real motion.
    """
    log = state.track_arc_motion.get(ac_hex)
    if log:
        plat, plon, _pts = log[-1]
        if haversine_km(lat, lon, plat, plon) * 1000.0 < _ARC_MOTION_MIN_M:
            return
    else:
        log = []
        state.track_arc_motion[ac_hex] = log
    log.append((lat, lon, ts))
    if len(log) > _ARC_MOTION_LOG_MAX:
        log.pop(0)


# Per-track throttle for the implausible-velocity warning.  The feed rebuilds
# several times a second, so an unthrottled warning emits the same line dozens
# of times for one aircraft and buries the very signal it exists to surface.
# One line per track per interval keeps each distinct offender visible.
_IMPLAUSIBLE_LOG_INTERVAL_S = 30.0
_implausible_last_log: dict[str, float] = {}


def _should_log_implausible(ac_hex: str, now: float) -> bool:
    last = _implausible_last_log.get(ac_hex, 0.0)
    if now - last < _IMPLAUSIBLE_LOG_INTERVAL_S:
        return False
    _implausible_last_log[ac_hex] = now
    # Bound the dict to the live fleet; entries outlive their tracks otherwise.
    if len(_implausible_last_log) > 512:
        cutoff = now - _IMPLAUSIBLE_LOG_INTERVAL_S * 10
        for k in [k for k, v in _implausible_last_log.items() if v < cutoff]:
            del _implausible_last_log[k]
    return True


def _estimate_velocity_ms_from_motion(
    ac_hex: str, lat: float, lon: float, now: float,
) -> tuple[float, float] | None:
    """Return (vel_east_ms, vel_north_ms) inferred from arc-midpoint motion.

    Searches the per-track motion log for the oldest sample 15–120 s old
    that's > 200 m from the current position; uses that displacement to
    derive an averaged east/north velocity.  Returns None if no sample is
    recent-enough-but-old-enough or the displacement is too small to trust
    or the implied speed exceeds an 800 kt sanity bound.
    """
    log = state.track_arc_motion.get(ac_hex)
    if not log:
        return None
    for plat, plon, pts in log:
        dt = now - pts
        if dt < 15:
            continue  # too recent — noise dominates
        if dt > 120:
            continue  # too old — aircraft may have manoeuvred
        east_m, north_m = (c * 1000.0 for c in enu_km(plat, plon, lat, lon))
        dist_m = math.hypot(east_m, north_m)
        if dist_m < 200:
            continue  # essentially still — can't infer velocity
        # Sanity bound.  This was 411 m/s (799 kt), which accepted a few km of
        # arc-midpoint jitter over 15 s as a legitimate 500-800 kt reading —
        # not a sanity bound so much as a formality.  Rejections are counted
        # rather than silently absorbed so the rate stays visible.
        if dist_m / dt > ARC_MOTION_MAX_SPEED_MS:
            state.arc_velocity_rejects += 1
            continue
        return east_m / dt, north_m / dt
    return None


def _estimate_velocity_from_motion(
    ac_hex: str, lat: float, lon: float, now: float,
) -> tuple[float, float] | None:
    """Return (gs_knots, track_deg) inferred from arc-midpoint motion, or None.

    Thin wrapper over _estimate_velocity_ms_from_motion that converts to the
    knots/degrees form used by the emitted aircraft entry.
    """
    ms = _estimate_velocity_ms_from_motion(ac_hex, lat, lon, now)
    if ms is None:
        return None
    vel_e_ms, vel_n_ms = ms
    gs_kn = math.hypot(vel_e_ms, vel_n_ms) * 1.94384
    bearing_deg = math.degrees(math.atan2(vel_e_ms, vel_n_ms)) % 360.0
    return round(gs_kn, 1), round(bearing_deg, 1)


def resolve_ground_truth_hex(
    ac_hex: str, lat: float, lon: float, max_distance_km: float = 8.0,
) -> str | None:
    """Find the best ground-truth hex for a solved aircraft."""
    norm_hex = normalize_hex_key(ac_hex)
    if norm_hex and norm_hex in state.ground_truth_trails and state.ground_truth_trails[norm_hex]:
        return norm_hex

    best_hex = None
    best_distance = max_distance_km
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        last = trail[-1]
        dist = position_distance_km(lat, lon, last[0], last[1])
        if dist <= best_distance:
            best_distance = dist
            best_hex = gt_hex
    return best_hex


# ── Node configs helper ──────────────────────────────────────────────────────

def get_node_configs() -> dict[str, dict]:
    configs = {}
    for nid, info in list(state.connected_nodes.items()):
        cfg = info.get("config")
        if cfg:
            configs[nid] = cfg
    return configs


# ── Per-node pipeline factory ─────────────────────────────────────────────────

def get_or_create_node_pipeline(
    node_id: str, default_pipeline: PassiveRadarPipeline,
) -> PassiveRadarPipeline:
    pipeline = state.node_pipelines.get(node_id)
    if pipeline is not None:
        return pipeline

    cfg = state.connected_nodes.get(node_id, {}).get("config", {})
    if cfg.get("rx_lat") and cfg.get("tx_lat"):
        pipeline_cfg = {
            "node_id": node_id,
            "Fs": cfg.get("fs_hz", cfg.get("Fs", 2_000_000)),
            "FC": cfg.get("fc_hz", cfg.get("FC", 195_000_000)),
            "rx_lat": cfg["rx_lat"],
            "rx_lon": cfg["rx_lon"],
            "rx_alt_ft": cfg.get("rx_alt_ft", 900),
            "tx_lat": cfg["tx_lat"],
            "tx_lon": cfg["tx_lon"],
            "tx_alt_ft": cfg.get("tx_alt_ft", 1200),
            "doppler_min": cfg.get("doppler_min", -300),
            "doppler_max": cfg.get("doppler_max", 300),
            "min_doppler": cfg.get("min_doppler", 15),
        }
        pipeline = PassiveRadarPipeline(pipeline_cfg)
        state.node_pipelines[node_id] = pipeline
        return pipeline

    return default_pipeline


# ── Per-frame processing (runs in thread pool) ───────────────────────────────

# Lightweight profiling: track thread-CPU vs wall-clock to distinguish
# actual work from GIL-wait.  Logs every 1000 frames (~45 s at 22 fps).
_prof_cpu = 0.0
_prof_wall = 0.0
_prof_n = 0
# Sub-phase accumulators (thread CPU time)
_prof_analytics = 0.0
_prof_assoc = 0.0
_prof_pipeline = 0.0
_prof_save = 0.0


def _node_track_views(pipeline: PassiveRadarPipeline) -> list[dict]:
    """This node's confirmed tracks, in the shape submit_tracks takes.

    TENTATIVE tracks are excluded, the same filter the arc builder applies: they
    have too little history to fit and may yet be deleted, and admitting them
    would put the clutter rejection back onto the association layer, which is
    most of what track-level association buys.  COASTING is kept for the same
    reason arcs keep it — at 22 fps a single missed frame flips ACTIVE →
    COASTING and the next flips it back.
    """
    views = []
    for tr in pipeline.tracker.tracks:
        if tr.state_status == TrackState.TENTATIVE:
            continue
        hist = tr.get_recent_detections(N2_TRACK_HISTORY_MAX)
        if len(hist) < 2:
            continue
        views.append({
            "track_id": tr.id or f"tmp-{id(tr)}",
            "history": [{"t_s": h["timestamp"] / 1000.0,
                         "delay_us": h["delay"],
                         "doppler_hz": h["doppler"],
                         "snr": h["snr"]} for h in hist],
        })
    return views


def process_one_frame(node_id: str, frame: dict, default_pipeline: PassiveRadarPipeline):
    """CPU-heavy frame processing — never runs on the event loop."""
    global _prof_cpu, _prof_wall, _prof_n
    global _prof_analytics, _prof_assoc, _prof_pipeline, _prof_save
    _t0_wall = time.monotonic()
    _t0_cpu = time.thread_time()

    # Deferred signature verification (moved off the event loop)
    if frame.pop("_needs_sig_verify", False):
        det_node_id = frame.get("node_id") or frame.get("_node_id") or node_id
        sig_valid = False
        if det_node_id in state.node_identities:
            sig_valid = state.sig_verifier.verify_packet(
                det_node_id, frame.get("payload_hash", ""), frame.get("signature", ""),
            )
        frame["_signing_mode"] = frame.get("signing_mode", "unknown")
        frame["_signature_valid"] = sig_valid
        if not sig_valid and det_node_id in state.node_identities:
            logging.warning("Invalid signature on detection from %s", det_node_id)

    _t1 = time.thread_time()
    state.node_analytics.record_detection_frame(node_id, frame)
    _prof_analytics += time.thread_time() - _t1

    # The node's own tracker runs first now.  Association used to see the raw
    # detection frame, which at n=2 is untestable — two nodes give 4
    # measurements against 6 unknowns, so a cross pairing between two real
    # aircraft leaves the same zero residual a real target does.  Pairing
    # *confirmed tracks* instead removes clutter before association (M-of-N),
    # collapses the candidate count from Na x Nb detections to Ta x Tb tracks,
    # and supplies the time history the constant-velocity fit needs.
    _t3 = time.thread_time()
    pipeline = get_or_create_node_pipeline(node_id, default_pipeline)
    pipeline.process_frame(frame)
    _prof_pipeline += time.thread_time() - _t3

    _t2 = time.thread_time()
    _ts_ms_assoc = frame.get("timestamp", 0)
    # Track-level association.  The detection-level path it replaced now lives
    # in retina_analytics.detection_association, reachable only from the
    # offline bench, which keeps it as the A/B baseline.
    pairs = state.node_associator.submit_tracks(
        node_id, _node_track_views(pipeline), _ts_ms_assoc,
    )
    solver_inputs = (state.node_associator.format_track_pairs_for_solver(pairs)
                     if pairs else [])
    if solver_inputs:
        node_cfgs = get_node_configs()
        for s_in in solver_inputs:
            if s_in["n_nodes"] < 2:
                continue
            try:
                state.solver_queue.put_nowait((s_in, node_cfgs, time.time()))
            except Exception:
                state.solver_queue_drops += 1
                if state.solver_queue_drops % 100 == 1:
                    logging.warning(
                        "Solver queue full — dropped %d candidates total",
                        state.solver_queue_drops,
                    )
                    from services.alerting import send_alert
                    send_alert(
                        "solver_queue_drops",
                        f"Solver queue full — {state.solver_queue_drops} candidates dropped",
                        {"total_drops": state.solver_queue_drops},
                    )
    _prof_assoc += time.thread_time() - _t2

    # ADS-B extraction: TCP handler runs _apply_synthetic_adsb for synth nodes
    # before queuing.  For non-TCP sources (e.g. blah2_bridge) the adsb list
    # arrives here still unextracted — store those positions now so the
    # verification and accuracy pipelines can reference them.
    _adsb_list = frame.get("adsb")
    if _adsb_list:
        _ts_ms = int(time.time() * 1000)
        for _ae in _adsb_list:
            if not isinstance(_ae, dict):
                continue
            _hex = _ae.get("hex") or _ae.get("icao")
            _lat = _ae.get("lat", 0)
            _lon = _ae.get("lon", 0)
            if not _hex or not _lat or not _lon:
                continue
            if not math.isfinite(_lat) or not math.isfinite(_lon):
                continue
            state.adsb_aircraft[_hex] = {
                "hex": _hex,
                "flight": _ae.get("flight", ""),
                "lat": _lat,
                "lon": _lon,
                "alt_baro": _ae.get("alt_baro", 0),
                "gs": _ae.get("gs", 0),
                "track": _ae.get("track", 0),
                "last_seen_ms": _ts_ms,
            }
        state.aircraft_dirty = True

    _t4 = time.thread_time()
    # maybe_auto_save moved to analytics_refresh_task to avoid blocking
    # frame workers during 915-file coverage-map save.
    _prof_save += time.thread_time() - _t4

    with _archive_buffer_lock:
        _archive_buffer[node_id].append(frame)
        _should_flush = len(_archive_buffer[node_id]) >= _ARCHIVE_BATCH_MAX
    if _should_flush:
        _flush_archive_node(node_id)

    _dt_cpu = time.thread_time() - _t0_cpu
    _dt_wall = time.monotonic() - _t0_wall
    _prof_cpu += _dt_cpu
    _prof_wall += _dt_wall
    _prof_n += 1
    if _prof_n % 1000 == 0:
        _ac = _prof_cpu / _prof_n * 1000
        _aw = _prof_wall / _prof_n * 1000
        _idle = (1 - _ac / _aw) * 100 if _aw > 0 else 0
        _a_an = _prof_analytics / _prof_n * 1000
        _a_as = _prof_assoc / _prof_n * 1000
        _a_pp = _prof_pipeline / _prof_n * 1000
        _a_sv = _prof_save / _prof_n * 1000
        logging.warning(
            "PERF: %d frames  cpu=%.1f wall=%.1f idle%%=%.0f  "
            "[analytics=%.1f assoc=%.1f pipeline=%.1f save=%.1f]ms",
            _prof_n, _ac, _aw, _idle, _a_an, _a_as, _a_pp, _a_sv,
        )

# ── Multi-node result → tar1090-compatible dict ──────────────────────────────

def multinode_to_aircraft(key: str, r: dict) -> dict:
    speed_ms = math.sqrt(r["vel_east"] ** 2 + r["vel_north"] ** 2)
    heading = math.degrees(math.atan2(r["vel_east"], r["vel_north"])) % 360
    # "supersonic" claims the *aircraft* is going Mach 1.  On this fleet that
    # claim has never once been true — the simulator's fastest aircraft is
    # 268 m/s — so every one of these has really been a bad velocity estimate,
    # which is a different fact about a different thing.  Report it as one:
    # `implausible_velocity` marks the estimate as untrustworthy, and only
    # genuinely supersonic *and* plausible motion keeps the old label.
    _mn_anomaly_types = []
    _mn_implausible = speed_ms > IMPLAUSIBLE_SPEED_MS
    if _mn_implausible:
        _mn_anomaly_types.append("implausible_velocity")
        state.implausible_velocity_count += 1
        # Log the geometry needed to tell weak Doppler observability from a
        # mis-association.  Without contributing_node_ids and rms_doppler
        # there is nothing to troubleshoot from.
        # Keyed on `key` rather than _mn_hex, which is not computed until below;
        # both are stable per-track identities so the throttle is equivalent.
        if _should_log_implausible(key, time.time()):
            logging.warning(
                "Implausible velocity: %.0f m/s (%.0f kt) n_nodes=%d "
                "rms_doppler=%.1f Hz rms_delay=%.1f µs vel_e=%.1f vel_n=%.1f "
                "lat=%.3f lon=%.3f nodes=%s",
                speed_ms, speed_ms * 1.94384, r.get("n_nodes", 0),
                r.get("rms_doppler", 0) or 0, r.get("rms_delay", 0) or 0,
                r.get("vel_east", 0), r.get("vel_north", 0),
                r.get("lat", 0), r.get("lon", 0),
                ",".join(r.get("contributing_node_ids", [])) or "?",
            )
    _mn_is_anom = bool(_mn_anomaly_types)
    _mn_hex = multinode_hex_from_key(key)
    with state.anomaly_lock:
        if _mn_is_anom:
            state.anomaly_hexes.add(_mn_hex)
        else:
            state.anomaly_hexes.discard(_mn_hex)
    return {
        "hex": _mn_hex,
        "type": "multinode_solve",
        "flight": f"MN{r['n_nodes']}N",
        "alt_baro": round(r["alt_m"] / 0.3048),
        "alt_geom": round(r["alt_m"] / 0.3048),
        "gs": round(speed_ms * 1.94384, 1),
        "track": round(heading, 1),
        "lat": round(r["lat"], 5),
        "lon": round(r["lon"], 5),
        # Real age of the solve, not 0 — see the matching note on the tracker
        # path.  Guarded because timestamp_ms is absent in some fixtures.
        "seen": round(max(0.0, time.time() - r["timestamp_ms"] / 1000.0), 1)
        if r.get("timestamp_ms")
        else 0,
        "messages": r["n_measurements"],
        "rssi": -round(1.0 / max(r.get("rms_delay", 1), 0.01), 1),
        "multinode": True,
        "n_nodes": r["n_nodes"],
        "contributing_node_ids": r.get("contributing_node_ids", []),
        "rms_delay": round(r["rms_delay"], 3),
        "rms_doppler": round(r["rms_doppler"], 2),
        "position_source": "multinode_solve",
        "is_anomalous": _mn_is_anom,
        "anomaly_types": _mn_anomaly_types,
        "max_velocity_ms": round(speed_ms, 1),
    }


# Aliases: tests import both names from this module, and the arc builder below
# reads more clearly with the short ones.  The bodies were duplicates of
# services.geo's — _enu_to_lla additionally used 111.32 and a cos_lat clamp of
# 0.1, which only differs above 84.3° latitude and would have misplaced a target
# there rather than admit the projection had run out.
_bearing_deg = bearing_deg


def _enu_to_lla(rx_lat: float, rx_lon: float, east_km: float, north_km: float) -> list[float]:
    lat, lon = offset_latlon(rx_lat, rx_lon, east_km, north_km)
    return [float(lat), float(lon)]


def _build_single_node_arc(
    track_or_delay,
    node_cfg: dict,
    *,
    target_lat: float | None = None,
    target_lon: float | None = None,
) -> list[list[float]] | None:
    """Build the bistatic-ambiguity arc for one detection.

    When `target_lat`/`target_lon` are provided (i.e. we know roughly where
    the aircraft actually is, via ADS-B or a prior solve), the arc is
    trimmed to a short segment around the known position. This makes the
    frontend trail of successive arcs readable as a chain of distinct
    blips tracing the target's path through the beam, instead of one fat
    smudge of overlapping full-beam loci.

    Without a known position (true unknowns like raw radar-only detections)
    the full beam-spanning locus is returned so the operator can see the
    full ambiguity.
    """
    if isinstance(track_or_delay, (int, float)):
        delay_us = track_or_delay
    else:
        delay_us = getattr(track_or_delay, "latest_delay_us", None)
    if delay_us is None or delay_us <= 0:
        return None

    rx_lat = node_cfg.get("rx_lat")
    rx_lon = node_cfg.get("rx_lon")
    tx_lat = node_cfg.get("tx_lat")
    tx_lon = node_cfg.get("tx_lon")
    if None in (rx_lat, rx_lon, tx_lat, tx_lon):
        return None

    beam_width_deg = float(node_cfg.get("beam_width_deg", 41.0) or 41.0)
    max_range_km = float(node_cfg.get("max_range_km", 50.0) or 50.0)
    beam_azimuth_deg = node_cfg.get("beam_azimuth_deg")
    if beam_azimuth_deg is None:
        beam_azimuth_deg = (_bearing_deg(rx_lat, rx_lon, tx_lat, tx_lon) + 90.0) % 360.0

    tx_east_km, tx_north_km = enu_km(rx_lat, rx_lon, tx_lat, tx_lon)
    baseline_km = math.hypot(tx_east_km, tx_north_km)
    differential_range_km = delay_us * C_KM_US

    def _differential_at(range_km: float, bearing_deg: float) -> float:
        bearing_rad = math.radians(bearing_deg)
        east_km = math.sin(bearing_rad) * range_km
        north_km = math.cos(bearing_rad) * range_km
        tx_dist_km = math.hypot(east_km - tx_east_km, north_km - tx_north_km)
        return tx_dist_km + range_km - baseline_km

    # Ceiling for the per-bearing binary search below.  Note this is a
    # *monostatic* RX-range bound, so a node's bistatic limit cannot be
    # substituted for it directly — doing so would silently truncate every arc.
    #
    # Derive it instead.  Writing D(r) for the differential range along a
    # bearing, the triangle inequality gives D(r) >= 2r - 2*baseline, so the
    # range satisfying a measured differential D is at most D/2 + baseline.
    # That is a tight ceiling that provably never clips the locus.
    max_bistatic_km = node_cfg.get("max_bistatic_range_km")
    if max_bistatic_km is not None:
        max_bistatic_km = float(max_bistatic_km)
        if differential_range_km > max_bistatic_km:
            # The measured delay puts the target beyond what this node can
            # physically detect — there is no arc to draw.
            return None
        search_max_km = differential_range_km / 2.0 + baseline_km
    else:
        search_max_km = max_range_km

    # When we have a known target position, centre the sweep on the bearing
    # from RX to that position and aim for an arc whose ground length is
    # roughly the same regardless of range — short enough to look like a
    # radar blip near the aircraft, long enough to remain visible at
    # continental zoom. Pure angular trimming (e.g. fixed 6°) collapses
    # below 1 pixel at low zooms; targeting an arc length in km solves it.
    if target_lat is not None and target_lon is not None:
        centre_bearing = _bearing_deg(rx_lat, rx_lon, target_lat, target_lon)
        target_range_km = max(
            haversine_km(rx_lat, rx_lon, target_lat, target_lon), 5.0)
        # ~25 km arc length keeps the blip visible at zoom ≥4 and short
        # enough to read as a chain at zoom ≥7 — verified on staging.
        BLIP_ARC_LENGTH_KM = 25.0
        sweep_width_deg = min(beam_width_deg, math.degrees(BLIP_ARC_LENGTH_KM / target_range_km))
        steps = 18
    else:
        centre_bearing = beam_azimuth_deg
        sweep_width_deg = beam_width_deg
        steps = 36

    half_sweep = sweep_width_deg / 2.0
    points: list[list[float]] = []
    for step in range(steps + 1):
        bearing_deg = centre_bearing - half_sweep + sweep_width_deg * (step / steps)
        # Stay within the antenna beam — drop bearings that fall outside.
        delta_from_axis = abs(((bearing_deg - beam_azimuth_deg + 180.0) % 360.0) - 180.0)
        if delta_from_axis > beam_width_deg / 2.0:
            continue
        lo = 0.0
        hi = search_max_km
        if _differential_at(hi, bearing_deg) < differential_range_km:
            continue
        for _ in range(32):
            mid = (lo + hi) / 2.0
            if _differential_at(mid, bearing_deg) < differential_range_km:
                lo = mid
            else:
                hi = mid
        bearing_rad = math.radians(bearing_deg)
        points.append(_enu_to_lla(
            rx_lat,
            rx_lon,
            hi * math.sin(bearing_rad),
            hi * math.cos(bearing_rad),
        ))

    if len(points) < 2:
        return None
    return points


# ── Combined aircraft.json builder ───────────────────────────────────────────

# How often to recompute detection arcs and GT snapshot (seconds).
# Iterating 915 pipelines × 5000 tracks every second is the #1 GIL hog;
# caching at 5 s cuts that penalty by 4/5.
_ARC_REFRESH_S = ARC_REFRESH_S
_GT_REFRESH_S = GT_REFRESH_S
_cached_pending_arcs: list[dict] = []
_arcs_last_ts: float = 0.0
_cached_gt_snapshot: dict = {}
_cached_gt_meta: dict = {}
_gt_last_ts: float = 0.0

# Per-track single-node arc memo.  _build_single_node_arc is a pure function
# of (delay_us, target position, node geometry); the flush loop rebuilds it
# every cycle even when the detection is unchanged, which is the compute the
# ~4s map pulse traces back to.  Key by (ac_hex, node_id) -- the identity the
# rest of the pipeline and the frontend already use -- with a fingerprint of
# the non-static inputs, so a miss happens exactly when a new detection changes
# them (invalidate on arrival, no timer).  node_cfg is treated as static per
# node_id.  The fingerprint is None-safe: latest_delay_us is None for ADS-B-only
# tracks and rounding None would raise.
_single_node_arc_cache: dict[tuple[str, str | None], tuple[tuple, list | None]] = {}


def _cached_single_node_arc(ac_hex, track, node_cfg, touched_keys):
    """Memoised _build_single_node_arc for the per-geolocated-track arc.

    Returns exactly what
        _build_single_node_arc(track, node_cfg,
                               target_lat=track.lat, target_lon=track.lon)
    returns, but recomputes only when the detection's inputs change.  The arc is
    a pure function of those inputs, so this is transparent to callers and the
    wire format.  ``touched_keys`` accumulates the keys visited this build so the
    caller can evict everything else, bounding the cache to the live fleet.
    """
    node_id = node_cfg.get("node_id")
    key = (ac_hex, node_id)
    touched_keys.add(key)

    delay_us = getattr(track, "latest_delay_us", None)
    fingerprint = (
        None if delay_us is None else round(delay_us, 3),
        round(track.lat, 6),
        round(track.lon, 6),
    )
    cached = _single_node_arc_cache.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    arc = _build_single_node_arc(
        track,
        node_cfg,
        target_lat=track.lat,
        target_lon=track.lon,
    )
    _single_node_arc_cache[key] = (fingerprint, arc)
    return arc


def _record_accuracy_sample(ac_hex: str, error_km: float, position_source: str, ts: float):
    """Append a solver-vs-ADS-B accuracy sample for the rolling accuracy API."""
    state.accuracy_samples.append({
        "hex": ac_hex,
        "error_km": round(error_km, 4),
        "position_source": position_source,
        "ts": round(ts, 1),
    })


def build_combined_aircraft_json(default_pipeline: PassiveRadarPipeline) -> dict:
    """Merge per-node pipelines, default pipeline, multinode, ADS-B into one feed."""
    now = time.time()
    seen_hex: set[str] = set()
    _touched_arc_keys: set[tuple[str, str | None]] = set()
    aircraft: list[dict] = []

    def _fresh_adsb(ac_hex: str):
        """Return ADS-B entry for ac_hex, or None if unavailable/stale.

        Checks state.adsb_aircraft first (live feed, < 60 s).  Falls back to
        state.external_adsb_cache (OpenSky snapshot) which the background
        poller already refreshes periodically — no additional staleness guard
        needed here.
        """
        entry = state.adsb_aircraft.get(ac_hex)
        if entry and now - entry.get("last_seen_ms", 0) / 1000 <= 60:
            return entry
        # Fallback: external ADS-B truth sourced from OpenSky Network.
        ext = state.external_adsb_cache.get(ac_hex)
        if ext and ext.get("lat") and ext.get("lon"):
            alt_m = ext.get("alt_m") or 0
            vel = ext.get("velocity") or 0
            hdg = ext.get("heading") or 0
            try:
                alt_ft_val = float(alt_m) / 0.3048
                gs_val = float(vel) * 1.94384
                hdg_val = float(hdg)
            except (TypeError, ValueError):
                alt_ft_val = 0.0
                gs_val = 0.0
                hdg_val = 0.0
            return {
                "hex": ac_hex,
                "lat": ext["lat"],
                "lon": ext["lon"],
                "alt_baro": round(alt_ft_val),
                "gs": round(gs_val, 1),
                "track": round(hdg_val, 1),
                "last_seen_ms": int(now * 1000),
            }
        return None

    def _track_entry(ac_hex, track, node_cfg):
        # Display staleness.  STALE_TRACK_S (120 s) governs when the *tracker*
        # forgets a track; it was also, implicitly, how long a track with no
        # fresh detections kept being painted on the map.  Because arc tracks
        # are not dead-reckoned, that meant a stationary icon for two minutes
        # while the real aircraft flew ~17 km on — measured on staging as every
        # arc track frozen for up to 141 s.  Stop rendering well before the
        # tracker forgets it, so the track survives for re-acquisition but is
        # not presented as a current fix.
        # Keyed on wall_clock_ts (when data last arrived), not pos_fix_ts (when
        # the position last moved).  passive_radar.py:529 advances pos_fix_ts
        # only when lat/lon actually changes, so a genuinely hovering drone —
        # still being detected every frame — would look stale and get dropped.
        # wall_clock_ts is also what "seen" means in the tar1090 schema.
        _track_ts = getattr(track, "wall_clock_ts", 0.0) or 0.0
        _track_age = (now - _track_ts) if _track_ts else 0.0
        if _track_age > DISPLAY_STALE_TRACK_S:
            return None

        # The solver output is always the primary position.  ADS-B data
        # enriches callsign / altitude / velocity but never overrides the
        # radar-derived position — that's the whole point of the system.
        adsb = _fresh_adsb(ac_hex)

        # Primary position: always from the LM solver
        solver_lat = round(track.lat, 6)
        solver_lon = round(track.lon, 6)
        lat = solver_lat
        lon = solver_lon

        # When ADS-B seeded the solver, label accordingly
        has_adsb = adsb and adsb.get("lat") and adsb.get("lon")
        position_source = "solver_adsb_seed" if has_adsb else "solver_single_node"

        # Altitude / speed / heading: use ADS-B when available (more precise
        # than the bistatic solver for these quantities), solver otherwise.
        # ADS-B alt_baro can be the string "ground"; coerce to float safely.
        def _num(v, fallback=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return float(fallback)

        alt_ft = _num(adsb.get("alt_baro", track.alt_ft) if adsb else track.alt_ft)
        gs = round(_num(adsb.get("gs", track.speed_knots) if adsb else track.speed_knots), 1)
        heading = round(_num(adsb.get("track", track.track_angle) if adsb else track.track_angle), 1)

        # Build ambiguity arc for every detection — including ADS-B-anchored
        # ones. The frontend uses these as a radar-style afterglow trail: each
        # WS update produces a new arc tagged with the current timestamp, old
        # arcs fade independently. Without arcs for ADS-B aircraft, the
        # synthetic fleet (where almost every target has ADS-B) gives the
        # impression that nodes are idle, which they're not.
        # Always centre on the bistatic-solved position (track.lat/lon), not
        # on the raw ADS-B GPS fix.  When the ADS-B fix is stale or sits
        # outside the beam (e.g. an aircraft near the TX tower), centering on
        # adsb.lat/lon sweeps the wrong beam sector, yields few or no valid
        # arc points, and the position falls back to an unstable raw solver
        # output that jumps tens of km between frames.  track.lat/lon is
        # always the bistatic-constrained estimate — correct sector, stable arc.
        ambiguity_arc = _cached_single_node_arc(
            ac_hex, track, node_cfg, _touched_arc_keys,
        )
        # raw_midpoint_lat/lon: the bistatic arc midpoint *before* any gates
        # or smoothing.  Captured for the arc-motion log so velocity
        # estimation is fed only true bistatic positions, not feedback from
        # the smoothing pipeline or gate-reverted last_emit values.
        raw_midpoint_lat = None
        raw_midpoint_lon = None
        if ambiguity_arc and position_source in ("solver_single_node", "solver_adsb_seed"):
            midpoint = ambiguity_arc[len(ambiguity_arc) // 2]
            lat = round(midpoint[0], 6)
            lon = round(midpoint[1], 6)
            raw_midpoint_lat = lat
            raw_midpoint_lon = lon
            position_source = "single_node_ellipse_arc"

            # Speed gate: if the new arc midpoint moved faster than 800 m/s
            # (~1560 kt) from the last EMITTED position, the tracker has likely
            # mis-associated this frame's measurement to a different target.
            # We compare against ``track_last_emit`` (refreshed every emit)
            # rather than ``track_histories`` (deduped at ~5 m), because dedup
            # can age the history timestamp 20-60 s for slow tracks and let a
            # 20 km mis-association come out under 800 m/s.
            # We revert the icon's lat/lon to the previous emit so a single
            # mis-associated frame doesn't yank the marker.  The arc itself is
            # still emitted: it represents this frame's bistatic measurement,
            # which the user reads as "radar saw something along this curve".
            # Nulling it left the testmap looking like every node was idle
            # whenever a single noisy frame came through.
            #
            # Both gates below are bounded by GATE_MAX_HOLD_S.  Reverting sets
            # track_last_emit to the reverted value with a fresh timestamp (see
            # the refresh below), so an unbounded gate compares every future
            # midpoint against a frozen reference and never releases — an
            # absorbing state.  Measured on staging before this bound: every
            # arc track pinned in place for up to 141 s while its aircraft flew
            # on, then a 19.6 km teleport when the gate finally cleared.  Once
            # the hold exceeds the bound we accept the measurement: a noisy
            # position is more honest than a confidently wrong stationary one.
            _hold = state.track_gate_hold.get(ac_hex)
            _hold_since = _hold[0] if _hold else None
            _hold_expired = (
                _hold_since is not None and (now - _hold_since) > GATE_MAX_HOLD_S
            )
            _gate_fired = False

            # Speed gate: if the new arc midpoint moved faster than 800 m/s
            # (~1560 kt) from the last EMITTED position, the tracker has likely
            # mis-associated this frame's measurement to a different target.
            # We compare against ``track_last_emit`` (refreshed every emit)
            # rather than ``track_histories`` (deduped at ~5 m), because dedup
            # can age the history timestamp 20-60 s for slow tracks and let a
            # 20 km mis-association come out under 800 m/s.
            # We revert the icon's lat/lon to the previous emit so a single
            # mis-associated frame doesn't yank the marker.  The arc itself is
            # still emitted: it represents this frame's bistatic measurement,
            # which the user reads as "radar saw something along this curve".
            # Nulling it left the testmap looking like every node was idle
            # whenever a single noisy frame came through.
            _last_emit = state.track_last_emit.get(ac_hex)
            if _last_emit:
                _prev_lat, _prev_lon, _prev_ts = _last_emit
                _dt = now - _prev_ts
                if 0 < _dt < 60:
                    _speed_ms = haversine_km(lat, lon, _prev_lat, _prev_lon) * 1000.0 / _dt
                    if _speed_ms > 800:
                        _gate_fired = True
                        if not _hold_expired:
                            lat = _prev_lat
                            lon = _prev_lon

            # RMS gate: persistently high rms_delay means the Kalman filter
            # cannot fit the current measurement — the track is in a
            # mis-association state and the icon's midpoint is unreliable.
            # Revert the icon to the last emitted position; keep the arc as
            # the honest "radar saw a detection along this curve" signal.
            # Threshold 7 µs: median rms is ~2 µs for clean tracks; bad
            # actors observed at 8–11 µs over dozens of consecutive frames.
            # Inside the outer `if ambiguity_arc and position_source in (...)`
            # block, so ambiguity_arc is guaranteed non-null here — only rms
            # needs checking.
            _rms = getattr(track, "rms_delay", 0.0) or 0.0
            if _rms > 7.0 and _last_emit:
                _gate_fired = True
                if not _hold_expired:
                    lat = _last_emit[0]
                    lon = _last_emit[1]

            # Maintain the hold window, and dead-reckon while held.  A held
            # position is otherwise *stationary* while the aircraft flies, which
            # is what turns a short suppression into an error that grows at the
            # target's own ground speed.  The blanket exclusion of arc tracks
            # from dead-reckoning (see the DR block below) is about the normal
            # path, where a fresh measurement lands every frame and competing
            # per-node updates made extrapolation oscillate.  While held we are
            # rejecting the measurement by definition, so the only alternative
            # to coasting is freezing.
            # The hold stores its own anchor (ts, lat, lon) rather than
            # extrapolating from track_last_emit: last_emit is rewritten with
            # the dead-reckoned value each frame, so coasting off it would
            # compound — advancing by the full elapsed hold every frame instead
            # of once.  Anchoring makes the offset a pure function of elapsed
            # hold time.
            if _gate_fired and not _hold_expired:
                if _hold is None:
                    _hold = (now, lat, lon)
                    state.track_gate_hold[ac_hex] = _hold
                _anchor_ts, _anchor_lat, _anchor_lon = _hold
                _vel_e = getattr(track, "vel_east", 0.0) or 0.0
                _vel_n = getattr(track, "vel_north", 0.0) or 0.0
                _hold_dt = min(now - _anchor_ts, GATE_MAX_HOLD_S)
                lat, lon = _anchor_lat, _anchor_lon
                if _hold_dt > 0.5 and (_vel_e != 0.0 or _vel_n != 0.0):
                    _dr_lat, _dr_lon = offset_latlon_m(
                        _anchor_lat, _anchor_lon,
                        east_m=_vel_e * _hold_dt, north_m=_vel_n * _hold_dt,
                    )
                    lat, lon = round(_dr_lat, 6), round(_dr_lon, 6)
            else:
                state.track_gate_hold.pop(ac_hex, None)
        elif not ambiguity_arc:
            # Arc is None.  Only suppress the track if there was a valid delay
            # measurement (delay > 0) but the arc still failed — which means
            # the solver position is outside the antenna beam and is unreliable.
            # If delay is 0/None the track has no bistatic measurement data and
            # should pass through rather than disappearing due to a missing arc.
            _arc_delay = getattr(track, "latest_delay_us", None)
            if _arc_delay and _arc_delay > 0:
                return None

        # Dead-reckon non-arc tracks (multinode, solver_adsb_seed) from the
        # last fix using stored velocity so positions update smoothly
        # between frame arrivals.  Arc tracks (single_node_ellipse_arc)
        # stay pinned to the bistatic-detected position — attempts to
        # extrapolate the arc midpoint between detections produced visible
        # oscillation because multiple node-pipelines can update the same
        # hex with conflicting track.lat values.  The frontend's own 60 fps
        # icon dead-reckoning still slides the visible aircraft forward
        # between emits using the gs / track fields.
        if position_source != "single_node_ellipse_arc":
            _vel_e = getattr(track, 'vel_east', 0.0) or 0.0
            _vel_n = getattr(track, 'vel_north', 0.0) or 0.0
            _pft = getattr(track, 'pos_fix_ts', 0.0) or getattr(track, 'wall_clock_ts', 0.0) or 0.0
            _dr_elapsed = min(now - _pft, 60.0)
            if _dr_elapsed > 0.5 and (_vel_e != 0.0 or _vel_n != 0.0):
                _dr_lat, _dr_lon = offset_latlon_m(
                    lat, lon,
                    east_m=_vel_e * _dr_elapsed, north_m=_vel_n * _dr_elapsed,
                )
                lat, lon = round(_dr_lat, 6), round(_dr_lon, 6)

        # Record arc-motion samples from the raw bistatic midpoint (kept
        # for the gs/heading estimator that drives the frontend's icon
        # dead-reckoning on tracks without ADS-B).
        if raw_midpoint_lat is not None:
            _record_arc_motion(ac_hex, raw_midpoint_lat, raw_midpoint_lon, now)

        # ── Position-jump anomaly ──────────────────────────────────────────
        # Flag a track that teleports — the solver mis-associated a far-away
        # measurement and the position leapt across the map.  The tracker's
        # own anomaly checks (supersonic, position_mismatch, identity_swap)
        # all require ADS-B truth, so non-ADS-B synthetic single-node tracks
        # had no jump detection at all; the speed gate above only reverts
        # arc-track positions (silently, no flag) and is skipped entirely
        # when dt ≥ 60 s, and multinode tracks have no gate.  This check is
        # universal: it compares the candidate position (the raw solver /
        # arc-midpoint position, *before* the arc-track revert hides it)
        # against the last emit and flags an implausible jump.
        #
        # state.track_last_emit still holds the PREVIOUS emit here — it is
        # refreshed on the line below.  We compare the FINAL emitted position
        # (post-revert), so an arc jump that the speed gate already caught
        # and reverted is NOT flagged — there's no visible teleport in that
        # case.  Only positions that actually leap on screen get flagged.
        _position_jump = False
        _prev_emit = state.track_last_emit.get(ac_hex)
        if _prev_emit:
            _pe_lat, _pe_lon, _pe_ts = _prev_emit
            _jdt = now - _pe_ts
            _jdist_m = haversine_km(lat, lon, _pe_lat, _pe_lon) * 1000.0
            # Trigger on either: a sustained-interval speed above Mach 3
            # (~1029 m/s — well past any real aircraft, so no false positives
            # on legitimate military supersonic traffic), or an absolute
            # single-emit leap over 30 km regardless of dt (covers the
            # reappear-after-gap case where dt is large and the implied speed
            # looks deceptively low).
            if (0 < _jdt < 120 and _jdist_m / _jdt > 1029.0) or _jdist_m > 30_000.0:
                _position_jump = True

        append_track_history(ac_hex, lat, lon, alt_ft, now)
        # Refresh the speed-gate reference on every emit (dedup-free) so the
        # next frame's gate sees the true emit-to-emit interval.
        state.track_last_emit[ac_hex] = [lat, lon, now]

        # gs/heading fallback for single-node arc tracks without ADS-B: the
        # LM solver routinely emits track.speed_knots in the 0–5 kt range
        # because doppler-only velocity is poorly constrained, and the
        # aircraft sits frozen on the map between sparse bistatic frames.
        # Derive a plausible gs/track from the arc-midpoint displacement
        # logged across recent emits — the only honest velocity signal
        # we have for those tracks.
        if gs < 10 and not has_adsb:
            _est = _estimate_velocity_from_motion(ac_hex, lat, lon, now)
            if _est is not None:
                gs, heading = _est

        # Record ADS-B-verified positions as calibration points for empirical coverage.
        if has_adsb:
            nid = node_cfg.get("node_id")
            adsb_lat = adsb.get("lat", 0)
            adsb_lon = adsb.get("lon", 0)
            # The *reported* position, not the solver's estimate.  This is a
            # characterisation of where the node can see, so it has to be built
            # from positions that are known independently of the thing being
            # characterised — the solve is what the coverage polygon is
            # eventually used to judge.  The previous code was gated on
            # has_adsb but stored solver_lat/solver_lon, so it inherited every
            # solver error while looking like ground truth.
            if nid and adsb_lat and adsb_lon:
                state.node_analytics.record_calibration_point(nid, adsb_lat, adsb_lon)
            if nid:
                # Track furthest verified detections per node (for detection range)
                area = state.node_analytics.detection_areas.get(nid)
                if area:
                    area.record_verified_detection(adsb_lat, adsb_lon, ac_hex)
            # Track accuracy: haversine(solver, adsb) per aircraft per update
            if adsb_lat and adsb_lon:
                err_km = position_distance_km(solver_lat, solver_lon, adsb_lat, adsb_lon)
                _record_accuracy_sample(ac_hex, err_km, position_source, now)

        rms_delay = round(getattr(track, "rms_delay", 0.0) or 0.0, 3)
        rms_doppler = round(getattr(track, "rms_doppler", 0.0) or 0.0, 2)

        # Anomaly propagation: GeolocatedTrack → state.anomaly_hexes.
        # Fold in the position-jump flag computed above so a teleporting
        # track is marked anomalous even when the tracker itself saw nothing
        # wrong (no ADS-B to contradict, doppler within Mach 1).
        _anom_types = set(getattr(track, "anomaly_types", set()))
        if _position_jump:
            _anom_types.add("position_jump")
        # Same velocity-plausibility marker as the multinode path.  Arc tracks
        # reach it by a different route — the LM solver's own speed_knots, or
        # velocity inferred from arc-midpoint displacement — but the statement
        # is identical: this speed is not one an aircraft produces, so the
        # estimate is untrustworthy.
        _implausible_v = (gs / 1.94384) > IMPLAUSIBLE_SPEED_MS if gs else False
        if _implausible_v:
            _anom_types.add("implausible_velocity")
            state.implausible_velocity_count += 1
            if _should_log_implausible(ac_hex, now):
                logging.warning(
                    "Implausible velocity: %.0f kt on %s hex=%s node=%s "
                    "rms_delay=%.1f µs adsb=%s lat=%.3f lon=%.3f",
                    gs, position_source, ac_hex, node_cfg.get("node_id"),
                    rms_delay or 0, bool(has_adsb), lat, lon,
                )
        _is_anom = (
            bool(getattr(track, "is_anomalous", False))
            or _position_jump
            or _implausible_v
        )
        _max_vel = getattr(track, "max_velocity_ms", 0.0)
        _flag_hex = ac_hex
        with state.anomaly_lock:
            if _is_anom:
                state.anomaly_hexes.add(_flag_hex)
            else:
                state.anomaly_hexes.discard(_flag_hex)

        return {
            "hex": ac_hex,
            "ground_truth_hex": resolve_ground_truth_hex(ac_hex, lat, lon),
            "type": "tisb_other",
            "flight": (track.adsb_hex or f"PR{abs(hash(track.track_id)) % 10000:04d}").strip(),
            "alt_baro": round(alt_ft),
            "alt_geom": round(alt_ft),
            "gs": gs,
            "track": heading,
            "lat": lat,
            "lon": lon,
            # Real age of the underlying fix, not 0.  Hardcoding 0 made a
            # frozen track claim to be fresh, so the frontend's
            # STALE_AIRCRAFT_MS and the health checks could never see it —
            # which is how a 120 s stationary icon went unnoticed.
            "seen": round(_track_age, 1),
            "messages": track.n_detections,
            "rssi": -10.0,
            "category": "A3",
            "multinode": False,
            "position_source": position_source,
            "ambiguity_arc": ambiguity_arc,
            "solver_lat": solver_lat,
            "solver_lon": solver_lon,
            "rms_delay": rms_delay,
            "rms_doppler": rms_doppler,
            "delay_us": round(getattr(track, "latest_delay_us", 0.0) or 0.0, 3),
            "doppler_hz": round(getattr(track, "latest_doppler_hz", 0.0) or 0.0, 2),
            "node_id": node_cfg.get("node_id"),
            "target_class": getattr(track, "target_class", None),
            "recent_positions": list(state.track_histories.get(ac_hex, [])),
            "is_anomalous": _is_anom,
            "anomaly_types": sorted(_anom_types) if _anom_types else [],
            "max_velocity_ms": round(_max_vel, 1),
        }

    # Staleness threshold: skip geolocated tracks not updated in the last
    # 120 s.  Without this, geolocated_tracks accumulates dead entries
    # between prune cycles and causes O(N_stale) work per flush — arc
    # computation, ground-truth resolution, history writes — all holding
    # the GIL and starving frame workers.
    _STALE_TRACK_S_LOCAL = STALE_TRACK_S

    # 1. Pre-aggregated geolocated aircraft (O(active) ≈ 275 instead of
    #    O(pipelines × tracks) ≈ 915 × 2.4).  Stale entries are pruned here.
    stale_geo = []
    with state.geo_aircraft_lock:
        for ac_hex, (track, cfg) in list(state.active_geo_aircraft.items()):
            if (now - getattr(track, 'wall_clock_ts', 0)) > _STALE_TRACK_S_LOCAL:
                stale_geo.append(ac_hex)
                continue
            if ac_hex in seen_hex:
                continue
            seen_hex.add(ac_hex)
            entry = _track_entry(ac_hex, track, cfg)
            if entry is not None:
                aircraft.append(entry)
        for k in stale_geo:
            state.active_geo_aircraft.pop(k, None)
            state.track_last_emit.pop(k, None)
            state.track_gate_hold.pop(k, None)
        with state.anomaly_lock:
            for k in stale_geo:
                state.anomaly_hexes.discard(k)

    # 2. Default pipeline — catch any tracks not in the pre-aggregated dict
    for track in list(default_pipeline.geolocated_tracks.values()):
        if (now - getattr(track, 'wall_clock_ts', 0)) > _STALE_TRACK_S_LOCAL:
            continue
        ac_hex = track.adsb_hex or track.hex_id
        if ac_hex in seen_hex:
            continue
        seen_hex.add(ac_hex)
        entry = _track_entry(ac_hex, track, default_pipeline.config)
        if entry is not None:
            aircraft.append(entry)

    # 3. Multi-node solver
    stale_mn = []
    for key, r in list(state.multinode_tracks.items()):
        age_s = now - r.get("timestamp_ms", 0) / 1000
        if age_s > 60:
            stale_mn.append(key)
            continue
        ac = multinode_to_aircraft(key, r)
        # Dead-reckon position using solver velocity (vel_east/vel_north in m/s)
        ts_fix = r.get("timestamp_ms", 0) / 1000.0
        elapsed = min(now - ts_fix, 60.0)
        vel_east_m_s = r.get("vel_east", 0.0)
        vel_north_m_s = r.get("vel_north", 0.0)
        if elapsed > 0.0 and (vel_east_m_s != 0.0 or vel_north_m_s != 0.0):
            _dr_lat, _dr_lon = offset_latlon_m(
                ac["lat"], ac["lon"],
                east_m=vel_east_m_s * elapsed, north_m=vel_north_m_s * elapsed,
            )
            ac["lat"], ac["lon"] = round(_dr_lat, 5), round(_dr_lon, 5)
        if ac["hex"] not in seen_hex:
            seen_hex.add(ac["hex"])
            append_track_history(ac["hex"], ac["lat"], ac["lon"], ac["alt_baro"], now)
            ac["recent_positions"] = list(state.track_histories.get(ac["hex"], []))
            ac["ground_truth_hex"] = resolve_ground_truth_hex(ac["hex"], ac["lat"], ac["lon"])
            aircraft.append(ac)
    for k in stale_mn:
        # Must use the same derivation as insertion (multinode_to_aircraft →
        # multinode_hex_from_key). This previously used an obsolete 4-char
        # format that never matched the mn<sha256[:10]> actually inserted, so
        # no multinode anomaly hex was ever evicted and anomaly_hexes grew
        # without bound — enough to trip the anomaly_flood health check.
        with state.anomaly_lock:
            state.anomaly_hexes.discard(multinode_hex_from_key(k))
        state.multinode_tracks.pop(k, None)

    # 4. ADS-B only — excluded from map per design.
    # Aircraft must have at least one radar detection to appear.
    # ADS-B data is used only as a solver seed and for enrichment
    # (callsign, altitude, velocity) of radar-detected aircraft.
    # Stale entries are still pruned to avoid unbounded memory growth.
    stale_adsb = []
    for hex_code, entry in list(state.adsb_aircraft.items()):
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            stale_adsb.append(hex_code)
    for k in stale_adsb:
        state.adsb_aircraft.pop(k, None)

    # 4b. Prune stale ground-truth trails and track histories to bound
    # memory and keep resolve_ground_truth_hex O(N) scans cheap.
    #
    # These two used to share one 300 s constant, but they want opposite
    # things.  A ground-truth entry is a *current position* pushed every 2 s;
    # once the pushes stop the aircraft has despawned, and holding it for
    # 300 s paints a stationary blue dot on the map for five minutes.  A track
    # history is the *trail behind* an aircraft, where a long tail is wanted.
    stale_gt = [
        h for h, trail in list(state.ground_truth_trails.items())
        if not trail or (now - trail[-1][3]) > GT_DISPLAY_STALE_S
    ]
    for h in stale_gt:
        state.ground_truth_trails.pop(h, None)
        state.ground_truth_meta.pop(h, None)
        with state.anomaly_lock:
            state.anomaly_hexes.discard(h)

    stale_th = [
        h for h, trail in list(state.track_histories.items())
        if not trail or (now - trail[-1][3]) > TRAIL_STALE_S
    ]
    for h in stale_th:
        state.track_histories.pop(h, None)
        # NOTE: do NOT prune track_last_emit here.  track_histories ages out
        # via the ~5 m dedup even when the track is still actively emitting
        # the same arc midpoint.  Clearing the speed-gate reference would
        # then let the next bad measurement leak through unchecked.
        # track_last_emit is pruned when the track itself goes stale
        # (see stale_geo cleanup above).

    # 5. Pending detection arcs from tracker tracks not yet geolocated.
    # These arcs appear immediately on each detection without waiting for
    # M-of-N promotion + LM solver convergence.
    # Recompute only every _ARC_REFRESH_S seconds — iterating 915 pipelines
    # × ~5000 tracks per second is a GIL-heavy operation that starves frame
    # workers.  Cached arcs are good enough for the 1-Hz map refresh.
    global _cached_pending_arcs, _arcs_last_ts
    if now - _arcs_last_ts >= _ARC_REFRESH_S:
        _arcs_last_ts = now
        pending_arcs = []
        for pipeline in list(state.node_pipelines.values()):
            node_cfg = pipeline.config
            for track in list(pipeline.tracker.tracks):
                # TENTATIVE tracks haven't been promoted via M-of-N yet — they
                # may still be flagged for deletion.  Skip them so spurious
                # short-lived tracks don't produce arcs.  ACTIVE and COASTING
                # tracks both produce arcs: at 22 fps a single missed frame
                # flips ACTIVE → COASTING and the next frame flips it back, so
                # filtering COASTING here would cause the arc to flicker at
                # ~11 Hz for any aircraft with intermittent detection.  The
                # natural lifecycle (track deleted after N_DELETE missed frames)
                # is what stops the arc when the aircraft truly leaves the beam.
                if track.state_status == TrackState.TENTATIVE:
                    continue
                if track.adsb_hex:
                    _ae = state.adsb_aircraft.get(track.adsb_hex)
                    if _ae and now - _ae.get("last_seen_ms", 0) / 1000 < 60:
                        continue
                meas = track.history.get("measurements")
                if not meas:
                    continue
                latest = next((m for m in reversed(meas) if m is not None), None)
                if latest is None:
                    continue
                delay_us = latest.get("delay", 0)
                if delay_us <= 0:
                    continue
                arc = _build_single_node_arc(delay_us, node_cfg)
                if not arc or len(arc) < 2:
                    continue
                pending_arcs.append({
                    "ambiguity_arc": arc,
                    "node_id": node_cfg.get("node_id"),
                    "doppler_hz": round(latest.get("doppler", 0), 2),
                    "target_class": getattr(track, "target_class", None),
                })
        _cached_pending_arcs = pending_arcs
    else:
        pending_arcs = _cached_pending_arcs

    # Ground-truth snapshot — recompute every _GT_REFRESH_S seconds.
    global _cached_gt_snapshot, _cached_gt_meta, _gt_last_ts
    if now - _gt_last_ts >= _GT_REFRESH_S:
        _gt_last_ts = now
        _cached_gt_snapshot = {
            h: list(trail)[-30:]
            for h, trail in list(state.ground_truth_trails.items())
            if trail
        }
        _cached_gt_meta = dict(state.ground_truth_meta)

    # Evict arc-cache entries for tracks not present this build, bounding the
    # cache to the live fleet with no timer: a plane is either in this snapshot
    # or it is not.  An empty touched set (no arc tracks this build) prunes all.
    for _stale_key in [k for k in _single_node_arc_cache if k not in _touched_arc_keys]:
        del _single_node_arc_cache[_stale_key]

    # Collapse multiple entries describing one aircraft. Runs last, after all
    # three sections have appended, so a multinode solve can displace a
    # single-node arc that was appended before it.
    aircraft = dedup_aircraft(aircraft)

    return {
        "now": now,
        "messages": len(aircraft),
        "aircraft": aircraft,
        "detection_arcs": pending_arcs,
        "ground_truth": _cached_gt_snapshot,
        "ground_truth_meta": _cached_gt_meta,
        "anomaly_hexes": sorted(state.anomaly_hexes),
    }
