"""The combined aircraft.json builder — sections, caches, assembly.

Extracted from frame_processor.py.  This module owns the 1 Hz feed: merging
per-node pipelines, the default pipeline, multinode solves and ADS-B into one
tar1090-compatible payload.  The per-track machinery lives in
services.track_gates; shared helpers in services.feed_helpers; stale-store GC
in services.feed_gc.
"""

import math
import os
import time

from retina_tracker.track import TrackState

from config.constants import (
    ARC_REFRESH_S,
    GT_REFRESH_S,
    MN_N2_MIN_SOLVES,
    MN_ONESHOT_TTL_S,
    STALE_TRACK_S,
)
from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from services import track_filter
from services.feed_gc import prune_stale_stores
from services.feed_helpers import (
    append_track_history,
    dedup_aircraft,
    resolve_ground_truth_hex,
)
from services.geo import offset_latlon_m
from services.id_utils import (
    multinode_hex_from_key,
    normalize_hex_key,
    passive_track_hex,
)
from services.track_gates import (
    _build_single_node_arc,
    _single_node_arc_cache,
    track_entry,
)


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only.

    The wall-clock feed caches are the sharp edge: two tests calling
    build_combined_aircraft_json within ARC_REFRESH_S / GT_REFRESH_S of each
    other used to receive the PREVIOUS test's detection_arcs and
    ground_truth verbatim.
    """
    global _cached_pending_arcs, _cached_detecting_nodes, _arcs_last_ts
    global _cached_gt_snapshot, _cached_gt_meta, _gt_last_ts
    _cached_pending_arcs = []
    _cached_detecting_nodes = {}
    _arcs_last_ts = 0.0
    _cached_gt_snapshot = {}
    _cached_gt_meta = {}
    _gt_last_ts = 0.0


def multinode_to_aircraft(key: str, r: dict) -> dict:
    speed_ms = math.sqrt(r["vel_east"] ** 2 + r["vel_north"] ** 2)
    heading = math.degrees(math.atan2(r["vel_east"], r["vel_north"])) % 360
    # No velocity-plausibility flag: supersonic targets are in scope, so a
    # high solved speed is reported as-is.  Anomaly flags come from the
    # contributing tracker tracks, stamped onto the result by the solver
    # (_collect_track_anomalies) — a dark anomalous target must not go
    # quiet the moment it graduates from arc to solve.
    _mn_hex = multinode_hex_from_key(key)
    _is_anom = bool(r.get("is_anomalous"))
    _anom_types = r.get("anomaly_types") or []
    with state.anomaly_lock:
        if _is_anom:
            state.anomaly_hexes.add(_mn_hex)
        else:
            # The discard also clears any hex left by an earlier release.
            # (No GT-meta guard needed: mn* hexes are solver-minted and never
            # appear in ground_truth_meta.)
            state.anomaly_hexes.discard(_mn_hex)
    _vel_untrusted = bool(r.get("vel_untrusted"))
    # VEL_TRUST_MODE, read per call like TRACK_DR_SOURCE: "off" (default)
    # changes nothing — the flag rides along but gs/track stay populated.
    # "active" additionally drops gs/track from an untrusted entry, since a
    # solve-velocity vector this unreliable (median vector error 81 vs
    # 13 m/s unflagged) is worse than not showing a heading/speed at all.
    _vel_trust_mode = (os.getenv("VEL_TRUST_MODE") or "off").strip().lower()
    entry = {
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
        "is_anomalous": _is_anom,
        "anomaly_types": sorted(_anom_types),
        "max_velocity_ms": round(
            max(speed_ms, r.get("max_velocity_ms", 0.0) or 0.0), 1
        ),
    }
    if _vel_untrusted:
        entry["vel_untrusted"] = True
        if _vel_trust_mode == "active":
            del entry["gs"]
            del entry["track"]
    return entry


# How often to recompute detection arcs and GT snapshot (seconds).
# Iterating 915 pipelines × 5000 tracks every second is the #1 GIL hog;
# caching at 5 s cuts that penalty by 4/5.
_ARC_REFRESH_S = ARC_REFRESH_S
_GT_REFRESH_S = GT_REFRESH_S
_cached_pending_arcs: list[dict] = []
_cached_detecting_nodes: dict[str, list[str]] = {}
_arcs_last_ts: float = 0.0
_cached_gt_snapshot: dict = {}
_cached_gt_meta: dict = {}
_gt_last_ts: float = 0.0


def build_combined_aircraft_json(default_pipeline: PassiveRadarPipeline) -> dict:
    """Merge per-node pipelines, default pipeline, multinode, ADS-B into one feed."""
    now = time.time()
    seen_hex: set[str] = set()
    _touched_arc_keys: set[tuple[str, str | None]] = set()
    aircraft: list[dict] = []

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
            entry = track_entry(ac_hex, track, cfg, now, _touched_arc_keys)
            if entry is not None:
                aircraft.append(entry)
        for k in stale_geo:
            state.active_geo_aircraft.pop(k, None)
            state.track_last_emit.pop(k, None)
            state.track_gate_hold.pop(k, None)
            state.track_arc_motion.pop(k, None)
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
        entry = track_entry(ac_hex, track, default_pipeline.config, now, _touched_arc_keys)
        if entry is not None:
            aircraft.append(entry)

    # 3. Multi-node solver
    stale_mn = []
    for key, r in list(state.multinode_tracks.items()):
        age_s = now - r.get("timestamp_ms", 0) / 1000
        if age_s > 60:
            stale_mn.append(key)
            continue
        # Display gates below are NOT staleness — a gated entry stays in
        # state.multinode_tracks so the next solve can confirm it (n=2) or
        # supersede it, and only the age_s > 60 branch above discards its
        # anomaly hex.  A one-shot solve renders nothing at all: a 2-node
        # track needs a second solve to prove it isn't a mirror-point ghost,
        # and a 3+-node one-shot gets a short preview window instead of the
        # full 60 s entry lifetime before it either confirms or expires.
        solve_count = int(r.get("solve_count") or 1)
        if r.get("n_nodes") == 2 and solve_count < MN_N2_MIN_SOLVES:
            continue
        if r.get("n_nodes", 0) >= 3 and solve_count == 1 and age_s > MN_ONESHOT_TTL_S:
            continue
        ac = multinode_to_aircraft(key, r)
        # Dead-reckon position using solver velocity (vel_east/vel_north in
        # m/s), capped at 30 s: beyond that a velocity error dominates any
        # solve accuracy (a 15 m/s error is already 450 m of drift at the
        # cap), so an old solve holds its last dead-reckoned point until the
        # 60 s entry expiry rather than drifting further.
        ts_fix = r.get("timestamp_ms", 0) / 1000.0
        elapsed = min(now - ts_fix, 30.0)
        vel_east_m_s = r.get("vel_east", 0.0)
        vel_north_m_s = r.get("vel_north", 0.0)
        # TRACK_DR_SOURCE, read per call like TRACK_SMOOTHER: "kf" (default)
        # dead-reckons with the display filter's LEARNED velocity when one
        # exists — the solved velocity this block used to trust was measured
        # (2026-08-09, n=93) at median 127 m/s vector error, i.e. ~3.8 km of
        # drift at the 30 s cap below, worse than the solve error itself.
        # "solve" restores the old behaviour (rollback, env only).  The KF
        # accessor returns None whenever the KF never saw this key (smoother
        # in ewma/off mode, first solve, TTL-swept) so the fallback below is
        # also the natural off-path, not a separate mode.
        if (os.getenv("TRACK_DR_SOURCE", "kf") or "kf").strip().lower() != "solve":
            _lv = track_filter.learned_velocity(key)
            if _lv is not None:
                vel_east_m_s, vel_north_m_s = _lv[0], _lv[1]
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

    # 4/4b. Stale-store GC — services.feed_gc.
    prune_stale_stores(now)

    # 5. Pending detection arcs from tracker tracks not yet geolocated.
    # These arcs appear immediately on each detection without waiting for
    # M-of-N promotion + LM solver convergence.
    # Recompute only every _ARC_REFRESH_S seconds — iterating 915 pipelines
    # × ~5000 tracks per second is a GIL-heavy operation that starves frame
    # workers.  Cached arcs are good enough for the 1-Hz map refresh.
    global _cached_pending_arcs, _cached_detecting_nodes, _arcs_last_ts
    if now - _arcs_last_ts >= _ARC_REFRESH_S:
        _arcs_last_ts = now
        pending_arcs = []
        # hex → nodes currently holding a promoted track for it.  The feed's
        # aircraft entries are one-per-hex (last writer wins), so this is the
        # only place the full per-node fan-out is visible — the debug map's
        # "detected by" list reads it.
        detecting: dict[str, set[str]] = {}
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
                _ae = None
                if track.adsb_hex:
                    _nid = node_cfg.get("node_id")
                    if _nid:
                        detecting.setdefault(
                            normalize_hex_key(track.adsb_hex), set()
                        ).add(_nid)
                    _ae = state.adsb_aircraft.get(normalize_hex_key(track.adsb_hex))
                    # ADS-B tracks used to `continue` here (no pending arc) —
                    # the single-node measurement is real regardless of the
                    # transponder, and skipping it left ADS-B-correlated
                    # aircraft with no arc at all once dedup collapsed their
                    # per-node entries.  Every promoted track now emits its
                    # measured-delay arc.
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
                    # hex + delay_us make the entry ingestible by the
                    # frontend arc buffer, which keys by (hex, node,
                    # quantized delay).  delay_us rounding must match the
                    # per-aircraft track_entry emission so the same
                    # measurement arriving via both channels collides onto
                    # one buffer key instead of double-drawing.
                    "hex": normalize_hex_key(track.adsb_hex)
                    if track.adsb_hex else passive_track_hex(track.id),
                    "delay_us": round(delay_us, 3),
                    "doppler_hz": round(latest.get("doppler", 0), 2),
                    "alt_baro": _ae.get("alt_baro") if _ae else None,
                    "target_class": getattr(track, "target_class", None),
                })
        _cached_pending_arcs = pending_arcs
        _cached_detecting_nodes = {
            h: sorted(nids) for h, nids in detecting.items()
        }
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
        "detecting_nodes": _cached_detecting_nodes,
        "ground_truth": _cached_gt_snapshot,
        "ground_truth_meta": _cached_gt_meta,
        "anomaly_hexes": sorted(state.anomaly_hexes),
    }
