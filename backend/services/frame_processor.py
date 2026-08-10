"""Detection frame processing + aircraft JSON builder.

Contains the synchronous per-frame pipeline that runs in a thread pool
and the combined aircraft.json builder used by the flush task.
"""

import logging
import math
import threading
import time
from collections import defaultdict

from retina_analytics.association import associate_detections_to_adsb
from retina_tracker.track import TrackState

from config.constants import (
    ADSB_VIEW_TAG_FRESH_N,
    ARCHIVE_BATCH_MAX,
    ARCHIVE_FLUSH_INTERVAL_S,
    N2_TRACK_HISTORY_MAX,
)
from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from services.geo import (
    valid_latlon,
)
from services.id_utils import normalize_hex_key as _normalize_hex_key
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
# One flush per node at a time.  The snapshot → write → truncate cycle is not
# atomic under _archive_buffer_lock (the disk write must happen outside it),
# so two concurrent flushers for the same node — a frame worker hitting the
# batch-max trigger and the background flush task — would both write the same
# N frames (duplicate Parquet rows) and then truncate twice, discarding up to
# N frames that arrived during the first write.
_archive_inflight: set[str] = set()


def _flush_archive_node(node_id: str):
    """Write buffered frames for one node to disk in a single call.

    Frames are retained in the buffer until the disk write succeeds — this
    way a transient write failure (disk full, permissions) doesn't silently
    drop data on the floor. The buffer is capped at _ARCHIVE_BUFFER_HARD_CAP
    to prevent unbounded growth if the disk stays unhealthy.
    """
    with _archive_buffer_lock:
        if node_id in _archive_inflight:
            # Another thread is mid-cycle for this node; its truncation
            # accounts exactly for the frames it wrote, and ours will be
            # picked up by the next flush.
            return
        _archive_inflight.add(node_id)
        frames = list(_archive_buffer.get(node_id, []))
    if not frames:
        with _archive_buffer_lock:
            _archive_inflight.discard(node_id)
        return
    try:
        archive_detections(node_id, frames)
    except Exception:
        # Write failed — keep frames buffered for the next cycle, but enforce
        # a memory cap so a sustained outage can't OOM the process.
        with _archive_buffer_lock:
            _archive_inflight.discard(node_id)
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
        _archive_inflight.discard(node_id)
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

# Re-exported for existing importers; the definition moved to id_utils so
# modules that must not import frame_processor (tcp_handler) can share it.
normalize_hex_key = _normalize_hex_key


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only.

    Covers only what this module still owns after the feed split — the
    archive buffer and the profiling accumulators.  The feed caches, gates
    and helpers have their own hooks (services.aircraft_feed,
    services.track_gates, services.feed_helpers), all called by conftest.
    """
    global _prof_cpu, _prof_wall, _prof_n
    global _prof_analytics, _prof_assoc, _prof_pipeline, _prof_archive
    with _archive_buffer_lock:
        _archive_buffer.clear()
        _archive_inflight.clear()
    with _prof_lock:
        _prof_cpu = _prof_wall = 0.0
        _prof_analytics = _prof_assoc = _prof_pipeline = _prof_archive = 0.0
        _prof_n = 0



# ── Node configs helper ──────────────────────────────────────────────────────

def get_node_configs() -> dict[str, dict]:
    configs = {}
    with state.connected_nodes_lock:
        snapshot = list(state.connected_nodes.items())
    for nid, info in snapshot:
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
            # Beam/range geometry: track_gates builds display arcs from
            # pipeline.config, so dropping these regressed every node with a
            # non-broadside azimuth to a default 41-degree broadside wedge
            # (12 of 15 fleet nodes; up to 178 degrees off) — arcs clipped to
            # the wrong sector or suppressed outright.
            "beam_azimuth_deg": cfg.get("beam_azimuth_deg"),
            "beam_width_deg": cfg.get("beam_width_deg"),
            "max_range_km": cfg.get("max_range_km"),
            "max_bistatic_range_km": cfg.get("max_bistatic_range_km"),
        }
        pipeline = PassiveRadarPipeline(pipeline_cfg)
        state.node_pipelines[node_id] = pipeline
        return pipeline

    return default_pipeline


# ── Per-frame processing (runs in thread pool) ───────────────────────────────

# Lightweight profiling: track thread-CPU vs wall-clock to distinguish
# actual work from GIL-wait.  Logs every 1000 frames (~45 s at 22 fps).
# Guarded by _prof_lock: up to FRAME_WORKERS threads run process_one_frame
# concurrently, and unlocked `+=` on these lost updates — _prof_n
# undercounted, inflating every per-frame millisecond in the PERF line.
_prof_lock = threading.Lock()
_prof_cpu = 0.0
_prof_wall = 0.0
_prof_n = 0
# Sub-phase accumulators (thread CPU time)
_prof_analytics = 0.0
_prof_assoc = 0.0
_prof_pipeline = 0.0
_prof_archive = 0.0


def _view_adsb_hex(track, hist) -> str | None:
    """The ADS-B hex this view should export as its seeding tag, or None.

    Each rule below independently forces None — fail toward dark on any
    doubt, the same discipline ADSB_SEED_MODE applies everywhere else:

    - zero or more than one distinct hex across hist's "adsb" entries
      (never tagged, or mid-window ambiguity/swap) → None;
    - track.adsb_hex set and disagreeing with that hex → None (the
      tracker's own identity call overrides a stale/short-lived detection
      tag);
    - none of the newest ADSB_VIEW_TAG_FRESH_N entries carries a tag → None
      (see ADSB_VIEW_TAG_FRESH_N's identity-swap rationale in
      config.constants — a single untagged newest frame is a receiver
      hiccup, N consecutive is the swap signature).

    hist is get_recent_detections output, oldest-first; each entry's
    "adsb" key is a dict (node/backend-correlated) or None.
    """
    hexes = {
        normalize_hex_key(h["adsb"]["hex"])
        for h in hist
        if isinstance(h.get("adsb"), dict) and h["adsb"].get("hex")
    }
    if len(hexes) != 1:
        return None
    hexn = next(iter(hexes))
    if track.adsb_hex and normalize_hex_key(track.adsb_hex) != hexn:
        return None
    if not any(
        isinstance(h.get("adsb"), dict) and h["adsb"].get("hex")
        for h in hist[-ADSB_VIEW_TAG_FRESH_N:]
    ):
        return None
    return hexn


def confirmed_track_views(tracker, history_n: int = N2_TRACK_HISTORY_MAX) -> list[dict]:
    """A tracker's confirmed tracks, in the shape submit_tracks takes.

    TENTATIVE tracks are excluded, the same filter the arc builder applies: they
    have too little history to fit and may yet be deleted, and admitting them
    would put the clutter rejection back onto the association layer, which is
    most of what track-level association buys.  COASTING is kept for the same
    reason arcs keep it — at 22 fps a single missed frame flips ACTIVE →
    COASTING and the next flips it back.

    Shared with scripts/association_bench.py (which carried a near-verbatim
    copy) so the bench feeds association exactly what production does.
    """
    views = []
    for tr in tracker.tracks:
        if tr.state_status == TrackState.TENTATIVE:
            continue
        hist = tr.get_recent_detections(history_n)
        if len(hist) < 2:
            continue
        views.append({
            "track_id": tr.id or f"tmp-{id(tr)}",
            "history": [{"t_s": h["timestamp"] / 1000.0,
                         "delay_us": h["delay"],
                         "doppler_hz": h["doppler"],
                         "snr": h["snr"]} for h in hist],
            "adsb_hex": _view_adsb_hex(tr, hist),
        })
    return views


def _node_track_views(pipeline: PassiveRadarPipeline) -> list[dict]:
    return confirmed_track_views(pipeline.tracker)


def process_one_frame(node_id: str, frame: dict, default_pipeline: PassiveRadarPipeline):
    """CPU-heavy frame processing — never runs on the event loop."""
    global _prof_cpu, _prof_wall, _prof_n
    global _prof_analytics, _prof_assoc, _prof_pipeline, _prof_archive
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
    _d_analytics = time.thread_time() - _t1

    # The node's own tracker runs first now.  Association used to see the raw
    # detection frame, which at n=2 is untestable — two nodes give 4
    # measurements against 6 unknowns, so a cross pairing between two real
    # aircraft leaves the same zero residual a real target does.  Pairing
    # *confirmed tracks* instead removes clutter before association (M-of-N),
    # collapses the candidate count from Na x Nb detections to Ta x Tb tracks,
    # and supplies the time history the constant-velocity fit needs.
    _t3 = time.thread_time()
    # Predictive ADS-B tagging for a node with no receiver of its own.
    # Never overwrites a node-provided list — the node's own correlation is
    # authoritative, and an absent list is the only case where the backend
    # fills in.  Active-only: attaching feeds the tracker's own ADS-B
    # association directly, so there is no inert way to shadow this.
    if state.ADSB_SEED_MODE == "active" and not frame.get("adsb"):
        _geo = state.node_associator.node_geometries.get(node_id)
        if _geo is not None:
            _tags = associate_detections_to_adsb(
                _geo, frame.get("delay", []), frame.get("doppler", []),
                state._adsb_for_seeding(), frame.get("timestamp", 0))
            if _tags is not None:
                frame["adsb"] = _tags
                state.bump_counter("adsb_seed_frames_autotagged")
    pipeline = get_or_create_node_pipeline(node_id, default_pipeline)
    pipeline.process_frame(frame)
    _d_pipeline = time.thread_time() - _t3

    _t2 = time.thread_time()
    _ts_ms_assoc = frame.get("timestamp", 0)
    # Track-level association.  The detection-level path it replaced now lives
    # in retina_analytics.detection_association, reachable only from the
    # offline bench, which keeps it as the A/B baseline.
    _track_views = _node_track_views(pipeline)
    # Feed the per-node distinct-track counters — total_tracks /
    # geolocated_tracks were exported (and read by the admin API) but never
    # written anywhere.
    state.node_analytics.record_node_tracks(
        node_id,
        (v["track_id"] for v in _track_views),
        list(pipeline.geolocated_tracks.keys()),
    )
    round_ = state.node_associator.submit_tracks_round(
        node_id, _track_views, _ts_ms_assoc,
    )
    # anchored_inputs (top-down claiming, ASSOC_CLAIM_MODE=active) and
    # adsb_inputs (ADS-B seeding, ADSB_SEED_MODE=active) are already in
    # solver-input shape — see _claim_round / _adsb_seed_round — so they
    # join the bottom-up pairs' formatted output directly.  Both empty in
    # off/shadow mode.
    solver_inputs = (
        (state.node_associator.format_track_pairs_for_solver(round_.pairs)
         if round_.pairs else [])
        + round_.anchored_inputs
        + round_.adsb_inputs
    )
    if solver_inputs:
        node_cfgs = get_node_configs()
        for s_in in solver_inputs:
            if s_in["n_nodes"] < 2:
                continue
            try:
                state.solver_queue.put_nowait((s_in, node_cfgs, time.time()))
            except Exception:
                state.bump_counter("solver_queue_drops")
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
    _d_assoc = time.thread_time() - _t2

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
            # Normalised for the same reason as the sim ingest path: readers
            # that dedupe/cross-reference lowercase the key first.
            _hex = normalize_hex_key(_ae.get("hex") or _ae.get("icao"))
            _lat = _ae.get("lat")
            _lon = _ae.get("lon")
            if not _hex or not valid_latlon(_lat, _lon):
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

    # Time the archive append + conditional flush — the phase that actually
    # hits disk.  This timer used to wrap an empty region (its body had moved
    # to analytics_refresh_task) and printed save=0.0 forever.
    _t4 = time.thread_time()
    with _archive_buffer_lock:
        _archive_buffer[node_id].append(frame)
        _should_flush = len(_archive_buffer[node_id]) >= _ARCHIVE_BATCH_MAX
    if _should_flush:
        _flush_archive_node(node_id)
    _d_archive = time.thread_time() - _t4

    _dt_cpu = time.thread_time() - _t0_cpu
    _dt_wall = time.monotonic() - _t0_wall
    # One locked update per frame; log fields are snapshotted under the same
    # lock so the printed averages are self-consistent.
    with _prof_lock:
        _prof_cpu += _dt_cpu
        _prof_wall += _dt_wall
        _prof_analytics += _d_analytics
        _prof_assoc += _d_assoc
        _prof_pipeline += _d_pipeline
        _prof_archive += _d_archive
        _prof_n += 1
        _log_now = _prof_n % 1000 == 0
        if _log_now:
            _ac = _prof_cpu / _prof_n * 1000
            _aw = _prof_wall / _prof_n * 1000
            _idle = (1 - _ac / _aw) * 100 if _aw > 0 else 0
            _a_an = _prof_analytics / _prof_n * 1000
            _a_as = _prof_assoc / _prof_n * 1000
            _a_pp = _prof_pipeline / _prof_n * 1000
            _a_sv = _prof_archive / _prof_n * 1000
            _n_snap = _prof_n
    if _log_now:
        logging.warning(
            "PERF: %d frames  cpu=%.1f wall=%.1f idle%%=%.0f  "
            "[analytics=%.1f assoc=%.1f pipeline=%.1f archive=%.1f]ms",
            _n_snap, _ac, _aw, _idle, _a_an, _a_as, _a_pp, _a_sv,
        )


# ── Back-compat re-exports ────────────────────────────────────────────────────
# The feed builder, track gates, GC and shared helpers moved to their own
# modules (services.aircraft_feed / track_gates / feed_gc / feed_helpers).
# These bindings keep every existing import site working; the mutable objects
# (e.g. _single_node_arc_cache) are only
# ever mutated in place by their owners, so shared bindings stay in sync.
from services.aircraft_feed import (  # noqa: E402,F401
    build_combined_aircraft_json,
    multinode_to_aircraft,
)
from services.feed_helpers import (  # noqa: E402,F401
    _estimate_velocity_from_motion,
    _estimate_velocity_ms_from_motion,
    _looks_like_same_aircraft,
    _record_arc_motion,
    append_track_history,
    dedup_aircraft,
    position_distance_km,
    resolve_ground_truth_hex,
)
from services.track_gates import (  # noqa: E402,F401
    _bearing_deg,
    _build_single_node_arc,
    _cached_single_node_arc,
    _enu_to_lla,
    _record_accuracy_sample,
    _single_node_arc_cache,
    fresh_adsb,
    track_entry,
)
