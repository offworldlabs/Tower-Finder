"""Analytics, nodes, overlaps pre-computation — runs every 30 s."""

import asyncio
import concurrent.futures
import hashlib
import logging
import math
import time

import numpy as np
import orjson

from config.constants import (
    ANALYTICS_REFRESH_INTERVAL_S,
    YAGI_BEAM_WIDTH_DEG,
    YAGI_MAX_RANGE_KM,
)
from config.constants import (
    DELAY_MATCH_THRESHOLD_US as _DELAY_MATCH_THRESHOLD_US,
)
from core import state
from services.geo import bearing_deg, bistatic_delay_us, haversine_km, point_in_beam
from services.geo import valid_latlon as _valid_latlon
from services.id_utils import multinode_hex_from_key

_analytics_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="analytics-bg",
)


def _percentile(vals: list, pct: float) -> float:
    """Return the pct-th percentile of a list using numpy. Returns 0.0 for empty input."""
    if not vals:
        return 0.0
    return float(np.percentile(vals, pct))


# Coverage digest each node's overlap grids were last built under.  A polygon
# that tightens after registration does not retroactively tighten the grids, so
# without this the constraint would only ever take effect on a restart.
_COVERAGE_DIGESTS: dict[str, tuple] = {}

# The raw digest is per-bin p85 at 0.1 km resolution, so while calibration
# points accumulate it moves with nearly every point and "only acts when the
# digest moves" degenerated into rebuilding every node's grids on every cycle
# (py-spy on staging: the analytics thread at ~23% of process CPU with the
# solver queue starving behind it, 50k pair rebuilds since boot).  Two dampers:
# compare at 2 km granularity — sub-bucket drift cannot meaningfully change
# which 3 km association grid cells the constraint excludes — and re-check a
# node at most every 150 s, which also skips the per-bin p85 recompute for
# nodes inside the window.  A node's first build is never delayed.
_COVERAGE_DIGEST_QUANT_KM = 2.0
_COVERAGE_RECHECK_MIN_S = 150.0
_COVERAGE_NEXT_CHECK: dict[str, float] = {}


# ── Disappearance detector (FOV_MODE shadow/active) ─────────────────────────
#
# node_id -> {hex -> ts last detected}.  A hex enters when the node's own
# tracker has it detected (the was-detected precondition: an aircraft this
# node has never detected can never produce a negative event, however long
# it goes unseen — the feedback-loop guard, so the detector can never talk
# itself into shrinking a node's coverage from mere silence).  It leaves the
# instant an event is emitted for it (one event per disappearance) or after
# _FOV_DISAPPEAR_MEMORY_S without being redetected.
_DETECTED_RECENTLY: dict[str, dict[str, float]] = {}

# Shrink itself needs >= 3 events spanning >= 10 min
# (empirical_coverage.FOV_NEG_EVENTS_TO_SHRINK / FOV_NEG_MIN_SPAN_S), so
# remembering a hex for less than that buys nothing — but 90 s gives room
# for an ordinary detection gap (an SNR dip, a rotor-blade flash) without
# minting a spurious "disappearance" for a target that never left.
_FOV_DISAPPEAR_MEMORY_S = 90.0
# An ADS-B fix older than this cannot say where the aircraft is NOW with any
# confidence — recording an event against a stale position would file it
# under the wrong bearing bin, corrupting the very evidence FOV_MODE relies
# on to converge toward the true obstruction.
_FOV_EVENT_MAX_ADSB_AGE_S = 30.0
# Bounds one node's worst-case contribution to one 30 s cycle.  An outage or
# a mass ADS-B glitch must mint a bounded burst, not an unbounded one — the
# per-bin FOV_NEG_MAX_PER_BIN FIFO caps the accumulated damage, but this
# caps how fast any one cycle can do damage.
_FOV_MAX_EVENTS_PER_NODE_CYCLE = 5
# A node that has not been heard from recently is not "failing to detect" —
# it is dark, and its remembered hexes must not be scored as disappearances
# just because nothing is arriving to redetect them.  NodeMetrics.last_heartbeat
# (record_heartbeat) is the closest existing per-node liveness timestamp —
# already the codebase's own gate for this exact question (see
# NodeReputation.evaluate_heartbeat) — so this reuses it rather than adding a
# new per-frame timestamp to NodeMetrics for one caller.
_FOV_NODE_LIVENESS_MAX_AGE_S = 60.0


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    _COVERAGE_DIGESTS.clear()
    _COVERAGE_NEXT_CHECK.clear()
    _DETECTED_RECENTLY.clear()


def _quantize_digest(digest: tuple) -> tuple:
    """Quantize a coverage_digest() entry for change detection.

    Two shapes reach here: the shrink-only prior's per-bin ``float | None``
    (constraint_digest, FOV_MODE off) and the learned FOV's per-bin
    ``(state_code, limit) | None`` (fov_digest, shadow/active).  The tuple
    form is compared with the state half taken verbatim — a bin opening
    (e.g. "closed" -> "prior") must trigger a rebuild even when its rounded
    limit does not move in the same tick, because a closed bin excludes
    association entirely while an open one, however small its limit, does
    not — and the limit half quantized same as the float form, so a
    tightened-vs-widened polygon and an opened-vs-closed bin damp rebuilds
    on the same 2 km grain.
    """
    out = []
    for v in digest:
        if v is None:
            out.append(None)
        elif isinstance(v, tuple):
            state_code, limit_km = v
            out.append((state_code, round(limit_km / _COVERAGE_DIGEST_QUANT_KM)))
        else:
            out.append(round(v / _COVERAGE_DIGEST_QUANT_KM))
    return tuple(out)


def _refresh_disappearance_detector(node_id: str, now: float,
                                    rx_lat: float, rx_lon: float,
                                    detected_hexes: set[str]) -> None:
    """One node's disappearance-event pass for one 30 s cycle.

    Callers gate this on state.FOV_MODE != "off" — it runs in BOTH shadow
    and active (warming the negative-evidence stream so an active flip does
    not start cold), never in off.

    Refreshes _DETECTED_RECENTLY[node_id] from detected_hexes, applies the
    liveness guard, then for every remembered hex now absent looks for a
    fresh ADS-B fix inside the node's CURRENT learned FOV and records one
    event for it — capped at _FOV_MAX_EVENTS_PER_NODE_CYCLE, one event per
    disappearance (the hex is dropped from memory once scored either way:
    emitted, or aged out past _FOV_DISAPPEAR_MEMORY_S with nothing to score
    it against).
    """
    memory = _DETECTED_RECENTLY.setdefault(node_id, {})
    for hex_code in detected_hexes:
        memory[hex_code] = now
    if not memory:
        return

    metrics = state.node_analytics.metrics.get(node_id)
    live = metrics is not None and (now - metrics.last_heartbeat) <= _FOV_NODE_LIVENESS_MAX_AGE_S
    if not live:
        # Neither grown (a dark tracker yields no detected_hexes) nor scored
        # here — a dark node's dead tracks must not shrink its FOV.  Entries
        # still expire on their own via _FOV_DISAPPEAR_MEMORY_S once the
        # node comes back.
        return

    fov = state.node_analytics.learned_fov_for(node_id)
    if fov is None:
        return

    emitted = 0
    for hex_code, last_seen in list(memory.items()):
        if hex_code in detected_hexes:
            continue   # still there
        if now - last_seen > _FOV_DISAPPEAR_MEMORY_S:
            memory.pop(hex_code, None)   # too stale to say anything
            continue
        if emitted >= _FOV_MAX_EVENTS_PER_NODE_CYCLE:
            continue   # cycle cap -- reconsidered next cycle
        entry = state.adsb_aircraft.get(hex_code)
        if entry is None:
            continue   # no current position to test or record against
        lat, lon = entry.get("lat"), entry.get("lon")
        if lat is None or lon is None:
            continue
        fix_age_s = now - entry.get("last_seen_ms", 0) / 1000.0
        if fix_age_s > _FOV_EVENT_MAX_ADSB_AGE_S:
            continue
        brg = bearing_deg(rx_lat, rx_lon, lat, lon)
        dist_km = haversine_km(rx_lat, rx_lon, lat, lon)
        if not fov.contains(brg, dist_km):
            continue   # not predicted detectable here right now
        if state.node_analytics.record_negative_event(node_id, lat, lon):
            state.bump_counter("fov_neg_events")
            emitted += 1
            memory.pop(hex_code, None)


def _refresh_coverage_constraints() -> int:
    """Rebuild overlap grids for nodes whose observed coverage has changed.

    Runs on the analytics cadence, not the frame path: each rebuild costs one
    grid computation per neighbour.  Only acts when a node's constraint digest
    moves a 2 km bucket, re-checked at most every _COVERAGE_RECHECK_MIN_S per
    node, so a settled fleet pays nothing and an accumulating one pays a
    bounded rate instead of a rebuild per calibration point.
    """
    rebuilt = 0
    now = time.monotonic()
    for node_id in list(state.node_associator.node_geometries):
        if (node_id in _COVERAGE_DIGESTS
                and now < _COVERAGE_NEXT_CHECK.get(node_id, 0.0)):
            continue
        digest = state.node_analytics.coverage_digest(node_id)
        if digest is None:
            continue
        _COVERAGE_NEXT_CHECK[node_id] = now + _COVERAGE_RECHECK_MIN_S
        digest = _quantize_digest(digest)
        if _COVERAGE_DIGESTS.get(node_id) == digest:
            continue
        # Record before rebuilding: a failure mid-rebuild should not spin.
        _COVERAGE_DIGESTS[node_id] = digest
        pairs = state.node_associator.rebuild_zones_for(node_id)
        if pairs:
            rebuilt += 1
            state.coverage_rebuild_nodes += 1
            state.coverage_rebuilds += pairs
    return rebuilt


def _refresh_analytics_and_nodes():
    """Heavy work: recompute analytics, nodes, and overlaps → store as bytes."""
    from services.tcp_handler import is_synthetic_node

    # Analytics
    analytics_data = {
        "nodes": state.node_analytics.get_all_summaries(),
        "cross_node": state.node_analytics.get_cross_node_analysis(),
    }
    state.latest_analytics_bytes = orjson.dumps(analytics_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Real-only variant: strip synthetic nodes so map.retina.fm never receives them
    with state.connected_nodes_lock:
        real_node_ids = {nid for nid, info in state.connected_nodes.items() if not info.get("is_synthetic", True)}
    analytics_real_data = {
        "nodes": {k: v for k, v in analytics_data["nodes"].items() if k in real_node_ids},
        "cross_node": analytics_data["cross_node"],
    }
    state.latest_analytics_real_bytes = orjson.dumps(analytics_real_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Nodes — snapshot once to avoid RuntimeError from concurrent TCP handler mutations
    with state.connected_nodes_lock:
        _nodes_snapshot = list(state.connected_nodes.items())
    nodes_data = {
        "nodes": {
            nid: {
                "status": info.get("status"),
                "name": info.get("config", {}).get("name", nid),
                "config_hash": info.get("config_hash"),
                "last_heartbeat": info.get("last_heartbeat"),
                "peer": info.get("peer"),
                "is_synthetic": info.get("is_synthetic", is_synthetic_node(nid)),
                "capabilities": info.get("capabilities", {}),
                "frequency": (
                    info.get("config", {}).get("FC")
                    or info.get("config", {}).get("fc_hz")
                    or info.get("config", {}).get("frequency")
                ),
                "sample_rate": (info.get("config", {}).get("Fs") or info.get("config", {}).get("fs_hz")),
                "location": {
                    "rx_lat": info.get("config", {}).get("rx_lat"),
                    "rx_lon": info.get("config", {}).get("rx_lon"),
                    "rx_alt_ft": info.get("config", {}).get("rx_alt_ft"),
                    "tx_lat": info.get("config", {}).get("tx_lat"),
                    "tx_lon": info.get("config", {}).get("tx_lon"),
                    "tx_alt_ft": info.get("config", {}).get("tx_alt_ft"),
                },
            }
            for nid, info in _nodes_snapshot
        },
        "connected": sum(1 for _, n in _nodes_snapshot if n.get("status") not in ("disconnected",)),
        "total": len(_nodes_snapshot),
        "synthetic": sum(1 for _, n in _nodes_snapshot if n.get("is_synthetic")),
    }
    state.latest_nodes_bytes = orjson.dumps(nodes_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Overlaps — only include zones with actual overlap to keep payload small
    overlaps_data = {
        "overlaps": [z for z in state.node_associator.get_overlap_summary() if z["has_overlap"]],
        "registered_nodes": list(state.node_associator.node_geometries.keys()),
    }
    state.latest_overlaps_bytes = orjson.dumps(overlaps_data, option=orjson.OPT_SERIALIZE_NUMPY)

    # Solver-vs-ADS-B accuracy statistics
    _refresh_accuracy_stats()

    # Per-node missed detection analysis
    try:
        _refresh_missed_detections(_nodes_snapshot)
    except Exception:
        logging.exception("_refresh_missed_detections failed")

    # Per-node solver verification (real nodes only — synthetic ones have no truth)
    for _nid in real_node_ids:
        try:
            _refresh_node_verification(_nid)
        except Exception:
            logging.exception("_refresh_node_verification failed for %s", _nid)
    # Drop nodes that are no longer connected so the payload can't go stale
    for _nid in set(state.latest_node_verification_bytes) - real_node_ids:
        state.latest_node_verification_bytes.pop(_nid, None)

    # MLAT (multinode) solver verification
    try:
        _refresh_mlat_verification()
    except Exception:
        logging.exception("_refresh_mlat_verification failed")

    # Overlap grids follow a coverage polygon that has tightened
    try:
        _refresh_coverage_constraints()
    except Exception:
        logging.exception("_refresh_coverage_constraints failed")

    # Synthetic chain-of-custody entries for connected nodes that lack them
    _ensure_custody_data()
    # Evict PassiveRadarPipeline instances for long-disconnected nodes to free RAM
    _evict_stale_pipelines(_nodes_snapshot)


# ── Missed detection analysis ─────────────────────────────────────────────────


def _bistatic_angle_deg(
    ac_lat: float, ac_lon: float,
    tx_lat: float, tx_lon: float,
    rx_lat: float, rx_lon: float,
) -> float:
    """Bistatic angle (degrees) at the aircraft for a single TX-RX pair.

    Uses the law of cosines on the (TX, aircraft, RX) triangle.  Returns 180
    for degenerate cases where the aircraft is essentially on top of a node.
    """
    a = haversine_km(ac_lat, ac_lon, tx_lat, tx_lon)  # aircraft → TX
    b = haversine_km(ac_lat, ac_lon, rx_lat, rx_lon)  # aircraft → RX
    c = haversine_km(tx_lat, tx_lon, rx_lat, rx_lon)  # baseline
    if a < 0.01 or b < 0.01:
        return 180.0
    cos_beta = (a * a + b * b - c * c) / (2.0 * a * b)
    cos_beta = max(-1.0, min(1.0, cos_beta))
    return math.degrees(math.acos(cos_beta))


# Was a byte-for-byte duplicate of frame_processor's, which was itself a copy
# of the library's.  Aliased rather than renamed at the ~12 call sites below
# because tests import it from this module by name.
_bearing_deg = bearing_deg


def _aircraft_in_beam(
    ac_lat: float,
    ac_lon: float,
    rx_lat: float,
    rx_lon: float,
    beam_azimuth_deg: float,
    beam_width_deg: float,
    max_range_km: float,
    tx_lat: float | None = None,
    tx_lon: float | None = None,
    max_bistatic_range_km: float | None = None,
) -> bool:
    """Return True if an aircraft at (ac_lat, ac_lon) is inside the node beam.

    Delegates to the shared gate.  The range rule must match the one the node
    actually detects under, or the missed-detection statistic measures the
    mismatch instead of the node: a node limited on bistatic range but scored
    against a monostatic circle counts every aircraft near the RX but far from
    the TX as "missed", which pushed the fleet-wide miss rate past the 70 %
    health threshold.  One implementation is how that stays matched.
    """
    return point_in_beam(
        ac_lat, ac_lon,
        rx_lat=rx_lat, rx_lon=rx_lon,
        tx_lat=tx_lat, tx_lon=tx_lon,
        beam_azimuth_deg=beam_azimuth_deg,
        beam_width_deg=beam_width_deg,
        max_range_km=max_range_km,
        max_bistatic_range_km=max_bistatic_range_km,
    )


def _detected_hexes_for(node_id: str) -> set[str]:
    """Lowercase ADS-B hexes the node's own tracker currently detects.

    "Detected" means associated a real detection on the latest frame; a
    coasting track is precisely NOT a detection, and counting it both
    delayed disappearance events and credited miss-rate "detected" into
    space the node cannot see.  Independent of the ADS-B in-range
    comparison — read by the miss-rate calculation below and, under
    FOV_MODE shadow/active, by the disappearance detector.
    """
    pipeline = state.node_pipelines.get(node_id)
    detected: set[str] = set()
    if pipeline:
        for track in pipeline.tracker.tracks:
            # getattr default keeps stub tracks in tests harmless; a raw
            # tracker Track always has n_missed.
            if getattr(track, "n_missed", 0) != 0:
                continue
            # The newest associated detection's own ADS-B tag outranks the
            # track's adsb_hex: the latter goes stale on identity swaps onto
            # untagged targets and would report the departed aircraft as
            # still detected.  Fall back to the track hex only when the
            # detection carries no tag (real nodes whose correlation lags).
            hex_val = None
            _get_recent = getattr(track, "get_recent_detections", None)
            if callable(_get_recent):
                _recent = _get_recent(n=1)
                if _recent:
                    hex_val = ((_recent[0].get("adsb") or {}).get("hex"))
            if not hex_val:
                hex_val = getattr(track, "adsb_hex", None)
            if hex_val:
                detected.add(hex_val.lower())
    return detected


def _refresh_missed_detections(nodes_snapshot: list):
    """Compare ADS-B ground truth against each node's beam geometry.

    For each active node, count how many ADS-B aircraft are within its
    detection zone but were NOT detected by the node's tracker.
    Results stored in ``state.latest_missed_detections``.
    """
    now = time.time()
    # Snapshot current ADS-B positions (both from node frames and external)
    adsb_snapshot: list[tuple[str, float, float]] = []
    seen_hexes: set[str] = set()  # lowercase, for O(1) dedup against external cache
    for hex_code, entry in list(state.adsb_aircraft.items()):
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 120:
            continue
        adsb_snapshot.append((hex_code, lat, lon))
        seen_hexes.add(hex_code.lower())

    for hex_code, entry in list(state.external_adsb_cache.items()):
        lat = entry.get("lat")
        lon = entry.get("lon")
        if lat is None or lon is None:
            continue
        # Avoid duplicates — O(1) set lookup, case-insensitive
        if hex_code.lower() in seen_hexes:
            continue
        adsb_snapshot.append((hex_code, lat, lon))
        seen_hexes.add(hex_code.lower())

    result: dict[str, dict] = {}

    for nid, info in nodes_snapshot:
        if info.get("status") == "disconnected":
            continue
        cfg = info.get("config", {})
        rx_lat = cfg.get("rx_lat")
        rx_lon = cfg.get("rx_lon")
        tx_lat = cfg.get("tx_lat")
        tx_lon = cfg.get("tx_lon")
        if not all((rx_lat, rx_lon, tx_lat, tx_lon)):
            continue

        beam_width = float(cfg.get("beam_width_deg") or YAGI_BEAM_WIDTH_DEG)
        max_range = float(cfg.get("max_range_km") or YAGI_MAX_RANGE_KM)
        beam_azimuth = cfg.get("beam_azimuth_deg")
        if beam_azimuth is None:
            # Default: Yagi sits perpendicular to the RX→TX baseline, so the
            # boresight is rotated +90° from the direct RX→TX bearing.
            # Nodes with a different antenna orientation should set beam_azimuth_deg
            # explicitly in their config to avoid incorrect missed-detection counts.
            beam_azimuth = (_bearing_deg(rx_lat, rx_lon, tx_lat, tx_lon) + 90.0) % 360.0

        # Score against the same range rule the node detects under; see
        # _aircraft_in_beam. Absent key → monostatic, unchanged for hardware.
        max_bistatic = cfg.get("max_bistatic_range_km")
        max_bistatic = float(max_bistatic) if max_bistatic is not None else None

        # FOV_MODE active only: score in_range against the learned FOV
        # instead of the theoretical wedge — off and shadow keep today's
        # _aircraft_in_beam verbatim, including in shadow (the display and
        # this payload stay theoretical until active proves out; shadow only
        # instruments the solver gate, see solver.py).
        fov = (state.node_analytics.learned_fov_for(nid)
               if state.FOV_MODE == "active" else None)

        # Aircraft within this node's beam
        in_range: list[str] = []
        for hex_code, ac_lat, ac_lon in adsb_snapshot:
            if fov is not None:
                ok = fov.contains(
                    _bearing_deg(rx_lat, rx_lon, ac_lat, ac_lon),
                    haversine_km(rx_lat, rx_lon, ac_lat, ac_lon),
                )
            else:
                ok = _aircraft_in_beam(
                    ac_lat, ac_lon, rx_lat, rx_lon, beam_azimuth, beam_width, max_range,
                    tx_lat=tx_lat, tx_lon=tx_lon, max_bistatic_range_km=max_bistatic,
                )
            if ok:
                in_range.append(hex_code)

        # Which of these did the node actually detect?  Independent of
        # in_range (a pure read of the node's own tracker) — computed here,
        # ahead of the in_range early-return, ONLY under shadow/active so the
        # disappearance detector below can use it even on a cycle where
        # nothing is currently in theoretical/learned range.  Off leaves this
        # whole block unreached, same as before FOV_MODE existed.
        detected_hexes = None
        if state.FOV_MODE != "off":
            detected_hexes = _detected_hexes_for(nid)
            try:
                _refresh_disappearance_detector(nid, now, rx_lat, rx_lon, detected_hexes)
            except Exception:
                logging.exception("_refresh_disappearance_detector failed for %s", nid)

        if not in_range:
            result[nid] = {
                "in_range": 0,
                "detected": 0,
                "missed": 0,
                "miss_rate": 0.0,
                "missed_aircraft": [],
            }
            continue

        if detected_hexes is None:
            detected_hexes = _detected_hexes_for(nid)

        in_range_set = set(h.lower() for h in in_range)
        detected_in_range = in_range_set & detected_hexes
        missed = in_range_set - detected_hexes

        # Build details for missed aircraft (limit to 20 for payload size)
        # Pre-build a hex→(lat,lon) dict for O(1) lookup instead of O(n²) scan.
        adsb_by_hex = {h.lower(): (lat, lon) for h, lat, lon in adsb_snapshot}
        missed_details = []
        for hex_code in list(missed)[:20]:
            if hex_code in adsb_by_hex:
                lat, lon = adsb_by_hex[hex_code]
                dist = haversine_km(rx_lat, rx_lon, lat, lon)
                missed_details.append(
                    {
                        "hex": hex_code,
                        "lat": round(lat, 5),
                        "lon": round(lon, 5),
                        "dist_km": round(dist, 1),
                    }
                )

        n_in_range = len(in_range_set)
        n_detected = len(detected_in_range)
        n_missed = len(missed)

        result[nid] = {
            "in_range": n_in_range,
            "detected": n_detected,
            "missed": n_missed,
            "miss_rate": round(n_missed / n_in_range, 3) if n_in_range > 0 else 0.0,
            "missed_aircraft": missed_details,
        }

    state.latest_missed_detections = result


def _refresh_accuracy_stats():
    """Compute solver-vs-ADS-B accuracy from the rolling sample buffer."""
    samples = list(state.accuracy_samples)
    if not samples:
        state.latest_accuracy_bytes = orjson.dumps({"n_samples": 0})
        return

    errors = [s["error_km"] for s in samples]
    errors.sort()
    n = len(errors)

    by_source: dict[str, list[float]] = {}
    for s in samples:
        by_source.setdefault(s["position_source"], []).append(s["error_km"])

    source_stats = {}
    for src, errs in by_source.items():
        errs.sort()
        sn = len(errs)
        source_stats[src] = {
            "n_samples": sn,
            "mean_km": round(sum(errs) / sn, 4),
            "median_km": round(_percentile(errs, 50), 4),
            "p95_km": round(_percentile(errs, 95), 4),
            "max_km": round(errs[-1], 4),
        }

    result = {
        "n_samples": n,
        "mean_km": round(sum(errors) / n, 4),
        "median_km": round(_percentile(errors, 50), 4),
        "p95_km": round(_percentile(errors, 95), 4),
        "max_km": round(errors[-1], 4),
        "by_source": source_stats,
        "velocity": _velocity_accuracy(),
    }
    state.latest_accuracy_bytes = orjson.dumps(result)


# Matching radius for pairing a solve to a ground-truth aircraft when comparing
# speed.  Deliberately tighter than resolve_ground_truth_hex's 8 km display
# radius: a loose match here would blame one aircraft's speed on another's
# solve and make the statistic meaningless.
_VELOCITY_MATCH_KM = 3.0


def _velocity_accuracy() -> dict:
    """Compare solved ground speed against simulated truth.

    Position accuracy has been measured for a long time; velocity has not, and
    velocity is where the errors are.  Staging showed 11 of 42 tracks reporting
    above the simulator's fastest aircraft (522 kt), peaking at 824 kt, while
    the *median* was accurate — a heavy tail rather than a bias, which is
    exactly the shape a spot-check hides and a percentile makes obvious.

    Returns {} when no ground truth exists (real deployments), so this costs
    nothing outside simulation.
    """
    truth = []
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        meta = state.ground_truth_meta.get(gt_hex) or {}
        speed = meta.get("speed_ms")
        if not speed:
            continue
        last = trail[-1]
        # Heading rides along for the direction-error stats below; a truth
        # aircraft with no heading (meta not carrying one) still counts for
        # the speed-ratio stats, it just never contributes a heading sample.
        truth.append((last[0], last[1], float(speed), meta.get("heading")))
    if not truth:
        return {}

    truth_max = max(t[2] for t in truth)
    ratios: list[float] = []
    heading_errs: list[float] = []
    vector_errs: list[float] = []
    over = 0
    for r in list(state.multinode_tracks.values()):
        lat, lon = r.get("lat"), r.get("lon")
        if lat is None or lon is None:
            continue
        vel_east = r.get("vel_east", 0.0)
        vel_north = r.get("vel_north", 0.0)
        solved = math.hypot(vel_east, vel_north)
        best, best_heading, best_d = None, None, _VELOCITY_MATCH_KM
        for t_lat, t_lon, t_speed, t_heading in truth:
            d = haversine_km(lat, lon, t_lat, t_lon)
            if d < best_d:
                best, best_heading, best_d = t_speed, t_heading, d
        if not best:
            continue
        ratios.append(solved / best)
        if solved > truth_max:
            over += 1
        # Heading is undefined near hover and the solve needs a direction to
        # compare against — same floors as solver.py's per-solve heading_err.
        if best >= 20.0 and best_heading is not None and solved > 1.0:
            solved_heading = math.degrees(math.atan2(vel_east, vel_north)) % 360
            heading_errs.append(
                abs((solved_heading - best_heading + 180.0) % 360.0 - 180.0)
            )
            gt_ve = best * math.sin(math.radians(best_heading))
            gt_vn = best * math.cos(math.radians(best_heading))
            vector_errs.append(math.hypot(vel_east - gt_ve, vel_north - gt_vn))

    out = {
        "n_matched": len(ratios),
        "truth_max_ms": round(truth_max, 1),
        # Counters are cumulative since boot; they flag rate, not instant state.
    }
    if ratios:
        ratios.sort()
        out.update({
            # Solved speed / truth speed. 1.0 is perfect; p95 is the number
            # that moves when velocity observability degrades.
            "ratio_median": round(_percentile(ratios, 50), 3),
            "ratio_p95": round(_percentile(ratios, 95), 3),
            "ratio_max": round(ratios[-1], 3),
            # Solves faster than any aircraft the simulator actually flies.
            # Should be 0; currently is not.
            "n_faster_than_any_truth": over,
        })
    if heading_errs:
        out.update({
            "heading_err_median_deg": round(_percentile(heading_errs, 50), 1),
            "heading_err_p95_deg": round(_percentile(heading_errs, 95), 1),
            "vector_err_median_ms": round(_percentile(vector_errs, 50), 1),
            "vector_err_p95_ms": round(_percentile(vector_errs, 95), 1),
        })
    return out


def _refresh_node_verification(node_id: str):
    """Compare one node's detections to ADS-B truth via bistatic delay matching."""
    node_tracks = []
    now = time.time()
    with state.geo_aircraft_lock:
        _geo_snapshot = list(state.active_geo_aircraft.items())
    for ac_hex, (track, cfg) in _geo_snapshot:
        if not isinstance(cfg, dict) or cfg.get("node_id") != node_id:
            continue
        wall_ts = getattr(track, "wall_clock_ts", 0)
        if (now - wall_ts) > 120:
            continue
        node_tracks.append((ac_hex, track, cfg))

    if not node_tracks:
        state.latest_node_verification_bytes[node_id] = orjson.dumps(
            {
                "node_id": node_id,
                "n_tracks": 0,
                "n_matched": 0,
                "tracks": [],
            },
            option=orjson.OPT_SERIALIZE_NUMPY,
        )
        return

    adsb_candidates: list[tuple[str, dict]] = []
    seen_adsb_hexes: set[str] = set()

    for adsb_hex, entry in list(state.adsb_aircraft.items()):
        if not _valid_latlon(entry.get("lat"), entry.get("lon")):
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            continue
        adsb_candidates.append((adsb_hex, entry))
        seen_adsb_hexes.add(adsb_hex)

    for adsb_hex, entry in list(state.external_adsb_cache.items()):
        if not _valid_latlon(entry.get("lat"), entry.get("lon")):
            continue
        if adsb_hex not in seen_adsb_hexes:
            adsb_candidates.append((adsb_hex, entry))
            seen_adsb_hexes.add(adsb_hex)

    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if gt_hex in seen_adsb_hexes or not trail:
            continue
        try:
            last = trail[-1]
            if len(last) < 4 or (now - last[3]) > 60:
                continue
            # Trail index 2 is METRES (routes/test.py packs alt_m) — keep it
            # under the metric key so the consumer below doesn't re-convert it
            # as feet.  Speed comes from the push metadata when present; when
            # it is absent there is no speed truth, and the match is excluded
            # from velocity-error stats rather than compared against 0.
            candidate = {
                "lat": last[0],
                "lon": last[1],
                "alt_m": last[2],
            }
            meta = state.ground_truth_meta.get(gt_hex) or {}
            if meta.get("speed_ms") is not None:
                candidate["velocity"] = float(meta["speed_ms"])
            adsb_candidates.append((gt_hex, candidate))
            seen_adsb_hexes.add(gt_hex)
        except Exception:
            continue

    matches = []
    pos_errors = []
    vel_errors = []
    alt_errors = []
    matched_adsb_hexes: set = set()
    matched_detections = []

    for ac_hex, track, cfg in node_tracks:
        measured_delay_us = getattr(track, "latest_delay_us", None)
        if not measured_delay_us or measured_delay_us <= 0:
            continue

        tx_lat = cfg.get("tx_lat") or 0.0
        tx_lon = cfg.get("tx_lon") or 0.0
        rx_lat = cfg.get("rx_lat") or 0.0
        rx_lon = cfg.get("rx_lon") or 0.0
        if not tx_lat or not rx_lat:
            continue

        solver_lat = getattr(track, "lat", 0.0) or 0.0
        solver_lon = getattr(track, "lon", 0.0) or 0.0
        solver_vel_e = getattr(track, "vel_east", 0.0) or 0.0
        solver_vel_n = getattr(track, "vel_north", 0.0) or 0.0
        solver_speed = math.sqrt(solver_vel_e**2 + solver_vel_n**2)
        solver_alt_m = getattr(track, "alt_m", 0.0) or 0.0

        best_adsb_hex = None
        best_adsb = None
        best_delay_err = _DELAY_MATCH_THRESHOLD_US

        for adsb_hex_c, adsb in adsb_candidates:
            if adsb_hex_c in matched_adsb_hexes:
                continue
            expected_delay = bistatic_delay_us(
                tx_lat,
                tx_lon,
                rx_lat,
                rx_lon,
                adsb["lat"],
                adsb["lon"],
            )
            delay_err = abs(measured_delay_us - expected_delay)
            if delay_err < best_delay_err:
                best_delay_err = delay_err
                best_adsb_hex = adsb_hex_c
                best_adsb = adsb

        if best_adsb is None:
            continue

        matched_adsb_hexes.add(best_adsb_hex)
        truth_lat = best_adsb["lat"]
        truth_lon = best_adsb["lon"]
        # Dual schema, keyed on which fields exist (not on truthiness — 0 ft
        # and 0 kt are legitimate values): live ADS-B entries carry
        # alt_baro (ft) / gs (kt); external-cache and ground-truth candidates
        # carry alt_m / velocity (m/s).  A candidate with neither speed field
        # has no speed truth and is excluded from velocity stats.
        _alt_ft = best_adsb.get("alt_baro")
        _alt_m = best_adsb.get("alt_m")
        _gs_kt = best_adsb.get("gs")
        _vel_ms = best_adsb.get("velocity")
        truth_alt_m = (
            float(_alt_ft) * 0.3048
            if _alt_ft is not None
            else float(_alt_m) if _alt_m is not None else None
        )
        truth_gs_ms = (
            float(_gs_kt) * 0.514444
            if _gs_kt is not None
            else float(_vel_ms) if _vel_ms is not None else None
        )

        err_km = haversine_km(solver_lat, solver_lon, truth_lat, truth_lon)

        vel_err = abs(solver_speed - truth_gs_ms) if truth_gs_ms is not None else None
        alt_err = abs(solver_alt_m - truth_alt_m) if truth_alt_m is not None else None

        pos_errors.append(err_km)
        if vel_err is not None:
            vel_errors.append(vel_err)
        if alt_err is not None:
            alt_errors.append(alt_err)

        matches.append(
            {
                "hex": ac_hex,
                "matched_adsb_hex": best_adsb_hex,
                "delay_match_us": round(best_delay_err, 2),
                "measured_delay_us": round(measured_delay_us, 2),
                "solver_lat": round(solver_lat, 6),
                "solver_lon": round(solver_lon, 6),
                "truth_lat": round(truth_lat, 6),
                "truth_lon": round(truth_lon, 6),
                "position_error_km": round(err_km, 3),
                "solver_speed_ms": round(solver_speed, 1),
                "truth_speed_ms": round(truth_gs_ms, 1) if truth_gs_ms is not None else None,
                "velocity_error_ms": round(vel_err, 1) if vel_err is not None else None,
                "solver_alt_m": round(solver_alt_m, 0),
                "truth_alt_m": round(truth_alt_m, 0) if truth_alt_m is not None else None,
                "altitude_error_m": round(alt_err, 0) if alt_err is not None else None,
            }
        )
        matched_detections.append((truth_lat, truth_lon, best_adsb_hex))

    area = state.node_analytics.detection_areas.get(node_id)
    if area:
        for det_lat, det_lon, det_hex in matched_detections:
            area.record_verified_detection(det_lat, det_lon, det_hex)

    pos_errors.sort()
    vel_errors.sort()
    alt_errors.sort()
    n = len(matches)

    result = {
        "node_id": node_id,
        "n_tracks": len(node_tracks),
        "n_matched": n,
        "position": {
            "mean_km": round(sum(pos_errors) / n, 3) if n else 0,
            "median_km": round(_percentile(pos_errors, 50), 3),
            "p95_km": round(_percentile(pos_errors, 95), 3),
            "max_km": round(pos_errors[-1], 3) if pos_errors else 0,
        },
        "velocity": {
            # Denominator is the matches that HAD speed truth, not all matches.
            "n": len(vel_errors),
            "mean_ms": round(sum(vel_errors) / len(vel_errors), 1) if vel_errors else 0,
            "median_ms": round(_percentile(vel_errors, 50), 1),
            "p95_ms": round(_percentile(vel_errors, 95), 1),
        },
        "altitude": {
            "n": len(alt_errors),
            "mean_m": round(sum(alt_errors) / len(alt_errors), 0) if alt_errors else 0,
            "median_m": round(_percentile(alt_errors, 50), 0),
            "p95_m": round(_percentile(alt_errors, 95), 0),
        },
        "tracks": matches[:50],
    }
    state.latest_node_verification_bytes[node_id] = orjson.dumps(result, option=orjson.OPT_SERIALIZE_NUMPY)


# ── MLAT (multinode) solver verification ─────────────────────────────────────

# Maximum age of a multinode solve result to include in verification (seconds).
_MLAT_SOLVE_MAX_AGE_S = 120
# Maximum distance between a solve result and a ground-truth point to count
# as a match.  12 km handles 2-node solves with marginal geometry.
_MLAT_MATCH_THRESHOLD_KM = 12.0
# Maximum altitude difference (metres) between solver and truth for a valid
# match.  With ADS-B altitude injection the solver altitude is exact (< 50 m
# error); a candidate truth aircraft whose altitude differs by more than this
# gate is a different aircraft that happens to be within the position window.
# 3000 m = 10 000 ft accommodates the full 2 km altitude-layer gap plus noise.
_MLAT_ALT_GATE_M = 3000.0
# Solve results within this radius are considered solver cycles for the same
# aircraft.  Only one representative per cluster enters the matching loop so
# that a single aircraft with multiple solver cycles does not inflate n_solves.
_MLAT_CLUSTER_KM = 12.0


def _stats_block(errors: list[float]) -> dict:
    """Build a {n_samples, mean_km, median_km, p95_km, max_km} block."""
    n = len(errors)
    if not n:
        return {"n_samples": 0}
    sorted_errs = sorted(errors)
    return {
        "n_samples": n,
        "mean_km": round(sum(sorted_errs) / n, 4),
        "median_km": round(_percentile(sorted_errs, 50), 4),
        "p95_km": round(_percentile(sorted_errs, 95), 4),
        "max_km": round(sorted_errs[-1], 4),
    }


def _per_aircraft_aggregate(samples: list[dict]) -> dict:
    """Per-aircraft view: collapse each (hex, n_nodes) into a single mean error.

    The raw stats over `mlat_samples` are biased toward whichever aircraft
    happens to be in the air at the time — a single difficult target solved
    every 30 s for an hour contributes ~120 samples, drowning out the rest.
    Deduplicating by `hex` (mean error per aircraft, per node-count) gives
    one data point per aircraft track, so an N=5 bucket dominated by one bad
    plane no longer looks like five independent bad solves.
    """
    by_hex: dict[tuple[str, int], list[float]] = {}
    for s in samples:
        hex_id = s.get("hex") or ""
        if not hex_id:
            continue
        key = (hex_id, int(s["n_nodes"]))
        by_hex.setdefault(key, []).append(s["error_km"])

    per_aircraft_errors: list[float] = []
    by_nodes: dict[int, list[float]] = {}
    for (_hex, nc), errs in by_hex.items():
        ac_mean = sum(errs) / len(errs)
        per_aircraft_errors.append(ac_mean)
        by_nodes.setdefault(nc, []).append(ac_mean)

    out = _stats_block(per_aircraft_errors)
    out["n_aircraft"] = len({h for h, _ in by_hex})
    out["by_node_count"] = {
        str(nc): _stats_block(errs) for nc, errs in sorted(by_nodes.items())
    }
    return out


def _refresh_mlat_accuracy_stats() -> None:
    """Compute rolling MLAT solver accuracy from the mlat_samples deque.

    Mirrors _refresh_accuracy_stats() for single-node solves, but broken
    down by node count (2-node vs 3-node etc.) instead of position_source.
    Emits both the raw per-solve view and a per-aircraft (deduplicated) view.
    Written to state.latest_mlat_accuracy_bytes, served by /api/test/mlat-accuracy.
    """
    samples = list(state.mlat_samples)
    if not samples:
        state.latest_mlat_accuracy_bytes = orjson.dumps(
            {"n_samples": 0, "computed_at": round(time.time(), 1)}
        )
        return

    errors = [s["error_km"] for s in samples]
    errors.sort()
    n = len(errors)

    by_nodes: dict[int, list[float]] = {}
    for s in samples:
        by_nodes.setdefault(int(s["n_nodes"]), []).append(s["error_km"])

    node_stats = {}
    for nc, errs in sorted(by_nodes.items()):
        sorted_errs = sorted(errs)
        sn = len(sorted_errs)
        node_stats[str(nc)] = {
            "n_samples": sn,
            "mean_km": round(sum(sorted_errs) / sn, 4),
            "median_km": round(_percentile(sorted_errs, 50), 4),
            "p95_km": round(_percentile(sorted_errs, 95), 4),
            "max_km": round(sorted_errs[-1], 4),
        }

    # Good-geometry filter: exclude solves where the aircraft was nearly
    # between TX and RX (high bistatic angle → bad GDOP).  Threshold 150° is
    # chosen to match the analytical crossover where single-ellipse cross-error
    # exceeds ~0.6 km/µs.  Samples without a bistatic angle (older entries
    # recorded before this field was added) are included unfiltered.
    _GOOD_GEOM_THRESH_DEG = 150.0
    good_geom_errors = sorted(
        s["error_km"]
        for s in samples
        if (s.get("max_bistatic_deg") or 0.0) < _GOOD_GEOM_THRESH_DEG
    )
    ng = len(good_geom_errors)
    good_geom_stats: dict = (
        {
            "n_samples": ng,
            "mean_km": round(sum(good_geom_errors) / ng, 4),
            "median_km": round(_percentile(good_geom_errors, 50), 4),
            "p95_km": round(_percentile(good_geom_errors, 95), 4),
            "max_km": round(good_geom_errors[-1], 4),
            "bistatic_angle_threshold_deg": _GOOD_GEOM_THRESH_DEG,
        }
        if ng
        else {"n_samples": 0, "bistatic_angle_threshold_deg": _GOOD_GEOM_THRESH_DEG}
    )

    # Normal-only filter: exclude anomalous/spoofed aircraft whose ADS-B reports
    # a fake position.  The solver correctly converges near the fake position,
    # but ground-truth comparison uses the real position → large apparent error
    # that does NOT reflect solver accuracy.  The "normal_only" section shows
    # clean solver accuracy for non-anomalous aircraft (production-realistic).
    normal_errors = sorted(
        s["error_km"] for s in samples if not s.get("is_anomalous")
    )
    nn = len(normal_errors)
    by_nodes_normal: dict[int, list[float]] = {}
    for s in samples:
        if not s.get("is_anomalous"):
            by_nodes_normal.setdefault(int(s["n_nodes"]), []).append(s["error_km"])
    node_stats_normal = {}
    for nc, errs in sorted(by_nodes_normal.items()):
        sorted_errs = sorted(errs)
        sn = len(sorted_errs)
        node_stats_normal[str(nc)] = {
            "n_samples": sn,
            "mean_km": round(sum(sorted_errs) / sn, 4),
            "median_km": round(_percentile(sorted_errs, 50), 4),
            "p95_km": round(_percentile(sorted_errs, 95), 4),
            "max_km": round(sorted_errs[-1], 4),
        }
    normal_stats: dict = (
        {
            "n_samples": nn,
            "mean_km": round(sum(normal_errors) / nn, 4),
            "median_km": round(_percentile(normal_errors, 50), 4),
            "p95_km": round(_percentile(normal_errors, 95), 4),
            "max_km": round(normal_errors[-1], 4),
            "by_node_count": node_stats_normal,
        }
        if nn
        else {"n_samples": 0}
    )

    # Per-aircraft view (dedup by hex). The raw section above counts each
    # solve independently — if the same plane is tracked over 60 verification
    # cycles it contributes 60 samples to the bucket. The per-aircraft block
    # collapses those into one mean per (hex, n_nodes) so per-N statistics
    # reflect "how the solver performs across the fleet" rather than "how
    # many cycles a single difficult target hung around for".
    per_aircraft = _per_aircraft_aggregate(samples)
    per_aircraft_normal = _per_aircraft_aggregate(
        [s for s in samples if not s.get("is_anomalous")]
    )

    state.latest_mlat_accuracy_bytes = orjson.dumps(
        {
            "n_samples": n,
            # Staleness markers: computed_at says when this payload was built;
            # newest_sample_ts_ms says how old the underlying evidence is.  A
            # fresh computed_at with an old newest_sample_ts_ms means the
            # pipeline is alive but no new solves are being verified.
            "computed_at": round(time.time(), 1),
            "newest_sample_ts_ms": max((s.get("ts") or 0) for s in samples),
            "mean_km": round(sum(errors) / n, 4),
            "median_km": round(_percentile(errors, 50), 4),
            "p95_km": round(_percentile(errors, 95), 4),
            "max_km": round(errors[-1], 4),
            "by_node_count": node_stats,
            "good_geometry": good_geom_stats,
            "normal_only": normal_stats,
            "per_aircraft": per_aircraft,
            "per_aircraft_normal": per_aircraft_normal,
        }
    )


def _refresh_mlat_verification():
    """Compare multinode solve results to ground-truth trails pushed by the fleet orchestrator.

    Matching is proximity-based (no adsb_hex in the solver result): for each
    fresh multinode solve we find the closest ground-truth trail point and
    record the lateral, altitude, and speed errors.

    Results are written to state.latest_mlat_verification_bytes and exposed
    via GET /api/test/mlat-verification.
    """
    now = time.time()

    # --- Build truth sources --------------------------------------------------
    n_truth_gt = 0
    n_truth_live_adsb = 0
    n_truth_external = 0

    # Ground-truth trails: kept as a {hex → (trail_list, meta)} snapshot for
    # time-matched lookup in the matching loop.  Aircraft positions are pushed
    # every 2 s and stored in a deque of up to 120 points (240 s of history).
    # Matching uses the trail point whose timestamp is closest to the solver's
    # timestamp_ms so that aircraft movement between capture and verification
    # time does not inflate position errors.
    gt_trails_snapshot: dict[str, tuple[list, dict]] = {}
    seen_gt_hexes: set[str] = set()
    for gt_hex, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        last = trail[-1]
        # Only include trails whose most-recent point is fresh (orchestrator
        # still running for this aircraft).
        if len(last) < 4 or (now - last[3]) > 60:
            continue
        gt_trails_snapshot[gt_hex] = (list(trail), state.ground_truth_meta.get(gt_hex, {}))
        seen_gt_hexes.add(gt_hex)
        n_truth_gt += 1

    # Fallback pools (current snapshot only — no trail history):
    # truth_pool: list of (hex, lat, lon, alt_m, speed_ms, object_type, is_anomalous)
    adsb_truth_pool: list[tuple] = []
    seen_truth_hexes: set[str] = set(seen_gt_hexes)

    # Fallback 1: live ADS-B entries not already covered by ground-truth trails.
    # Solve results are kept up to _MLAT_SOLVE_MAX_AGE_S = 120 s because solves
    # are infrequent (one per 40 s frame interval) — a 60 s window would discard
    # most valid results before a second truth point arrives.
    for adsb_hex, entry in list(state.adsb_aircraft.items()):
        if adsb_hex in seen_truth_hexes:
            continue
        if entry.get("lat") is None or entry.get("lon") is None:
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            continue
        # tar1090 convention: alt_baro is the string "ground" for aircraft on
        # the ground — a bare multiply raised TypeError and killed the whole
        # refresh cycle, leaving /api/test/mlat-accuracy permanently empty.
        _gs_raw = entry.get("gs", 0) or 0
        _alt_raw = entry.get("alt_baro", 0) or 0
        gs_ms = (_gs_raw if isinstance(_gs_raw, (int, float)) else 0.0) * 0.514444
        alt_m = (_alt_raw if isinstance(_alt_raw, (int, float)) else 0.0) * 0.3048
        adsb_truth_pool.append(
            (
                adsb_hex,
                entry["lat"],
                entry["lon"],
                float(alt_m),
                float(gs_ms),
                "aircraft",
                False,
            )
        )
        seen_truth_hexes.add(adsb_hex)
        n_truth_live_adsb += 1

    # Fallback 2: OpenSky / external ADS-B snapshot — same pattern as
    # _refresh_node_verification().  Useful when the live ADS-B injector
    # is in its rate-limit backoff window (up to 300 s).
    for adsb_hex, entry in list(state.external_adsb_cache.items()):
        if not _valid_latlon(entry.get("lat"), entry.get("lon")):
            continue
        if adsb_hex not in seen_truth_hexes:
            # external_adsb_cache schema is {lat, lon, alt_m, velocity,
            # heading} (periodic.py) — NOT the tar1090 gs/alt_baro schema.
            # Reading gs/alt_baro here zeroed every external truth entry.
            gs_ms = float(
                entry["velocity"]
                if entry.get("velocity") is not None
                else (entry.get("gs") or 0) * 0.514444
            )
            alt_m = float(
                entry["alt_m"]
                if entry.get("alt_m") is not None
                else (entry.get("alt_baro") or 0) * 0.3048
            )
            adsb_truth_pool.append(
                (
                    adsb_hex,
                    entry["lat"],
                    entry["lon"],
                    float(alt_m),
                    float(gs_ms),
                    "aircraft",
                    False,
                )
            )
            seen_truth_hexes.add(adsb_hex)
            n_truth_external += 1

    # --- Walk multinode solve results -----------------------------------------
    # Count fresh solves BEFORE the truth-pool check so n_solves is always honest
    # even when we have no truth data to match against.
    # Snapshot node configs (tx_lat/tx_lon/rx_lat/rx_lon) keyed by node_id so
    # we can compute the bistatic angle at each solver position without touching
    # state.connected_nodes inside the per-solve loop.
    with state.connected_nodes_lock:
        _cfg_items = list(state.connected_nodes.items())
    node_cfg_snap: dict[str, dict] = {
        nid: info.get("config", {})
        for nid, info in _cfg_items
        if isinstance(info, dict)
    }

    mn_snapshot = list(state.multinode_tracks.items())
    fresh_solves = []
    for key, r in mn_snapshot:
        ts_ms = r.get("timestamp_ms", 0)
        age_s = now - ts_ms / 1000.0
        if age_s > _MLAT_SOLVE_MAX_AGE_S or age_s < 0:
            continue
        if not _valid_latlon(r.get("lat"), r.get("lon")):
            continue
        fresh_solves.append((key, r))

    n_solver_cycles = len(fresh_solves)

    if not gt_trails_snapshot and not adsb_truth_pool:
        # Still refresh the rolling accuracy payload — otherwise
        # /api/test/mlat-accuracy silently serves numbers frozen at the moment
        # the truth feed stopped, with nothing marking them stale.
        _refresh_mlat_accuracy_stats()
        state.latest_mlat_verification_bytes = orjson.dumps(
            {
                "computed_at": round(now, 1),
                "skip_reason": "no_truth_candidates",
                "n_solves": n_solver_cycles,
                "n_solver_cycles": n_solver_cycles,
                "n_unique_aircraft": n_solver_cycles,
                "n_matched": 0,
                "match_rate_pct": 0.0,
                "match_threshold_km": _MLAT_MATCH_THRESHOLD_KM,
                "n_truth_candidates": 0,
                "truth_sources": {"ground_truth": 0, "live_adsb": 0, "external_adsb": 0},
                "position": {"mean_km": 0, "median_km": 0, "p95_km": 0, "max_km": 0},
                "velocity": {"mean_ms": 0, "median_ms": 0, "p95_ms": 0},
                "altitude": {"mean_m": 0, "median_m": 0, "p95_m": 0},
                "by_node_count": {},
                "tracks": [],
                "unmatched": {"n": n_solver_cycles, "nearest_truth": {"mean_km": None, "median_km": None, "p95_km": None}, "tracks": []},
            },
            option=orjson.OPT_SERIALIZE_NUMPY,
        )
        return

    # Greedy best-match assignment: pre-sort fresh_solves by distance to nearest
    # truth so the globally-closest (solver, truth) pair is always matched first.
    # Without this sort, when two solver cycles from different node pairs both
    # resolve near the same aircraft (e.g. one at 3 km error, one at 10 km), the
    # dict-insertion-order winner claims the truth even if it is the worse result,
    # leaving the better result unmatched and recording the inflated error.
    def _min_truth_dist_km(kv: tuple) -> float:
        _r = kv[1]
        _slat = float(_r.get("lat", 0))
        _slon = float(_r.get("lon", 0))
        _sts = _r.get("timestamp_ms", 0) / 1000.0
        _best = float(_MLAT_MATCH_THRESHOLD_KM)
        for _gt_hex, (_trail, _meta) in gt_trails_snapshot.items():
            _cl = min(_trail, key=lambda p: abs(p[3] - _sts))
            if abs(_cl[3] - _sts) > _MLAT_SOLVE_MAX_AGE_S + 30:
                continue
            _best = min(_best, haversine_km(_slat, _slon, _cl[0], _cl[1]))
        for _te in adsb_truth_pool:
            _best = min(_best, haversine_km(_slat, _slon, _te[1], _te[2]))
        return _best

    fresh_solves.sort(key=_min_truth_dist_km)

    # Greedy assignment after best-first sort: each truth hex is now claimed by
    # the closest solver result, preventing worse duplicates from displacing it.
    matches: list[dict] = []
    unmatched: list[dict] = []
    unmatched_nearest_km: list[float] = []
    pos_errors: list[float] = []
    vel_errors: list[float] = []
    alt_errors: list[float] = []
    matched_truth_hexes: set[str] = set()
    by_node_count: dict[int, list[float]] = {}

    # Each fresh solve is an individual candidate; n_unique_aircraft is computed
    # after matching by counting: matched aircraft + unmatched solves whose
    # nearest truth hex is not already claimed by a matched solve.
    for key, r in fresh_solves:
        solver_lat = float(r["lat"])
        solver_lon = float(r["lon"])
        solver_alt_m = float(r.get("alt_m", 0) or 0)
        solver_vel_e = float(r.get("vel_east", 0) or 0)
        solver_vel_n = float(r.get("vel_north", 0) or 0)
        solver_speed_ms = math.sqrt(solver_vel_e**2 + solver_vel_n**2)
        n_nodes = int(r.get("n_nodes", 0))
        solver_ts = r.get("timestamp_ms", 0) / 1000.0

        best_truth: tuple | None = None
        best_dist_km = _MLAT_MATCH_THRESHOLD_KM

        # 1a. Identity-first match — within match threshold only.
        #     When the solver carries an adsb_hex, prefer binding to that
        #     aircraft's trail over a proximity guess (disambiguates between
        #     two aircraft that are close together). The threshold check is
        #     intentional: a solve claiming hex H but landing beyond _MLAT_MATCH_THRESHOLD_KM (12 km) from H's
        #     trail is more plausibly a misclaim (wrong-frame association,
        #     spoofed-init convergence) than a real measurement of H. Letting
        #     unbounded errors through made post-PR-71 normal_only stats
        #     6-7× worse than they were before — we need the threshold floor
        #     to prevent that. Spoofed-target solves outside threshold fall
        #     to proximity fallback, then to unmatched (same as before).
        solver_hex = (r.get("adsb_hex") or "").strip().lower() or None
        if solver_hex and solver_hex in gt_trails_snapshot and solver_hex not in matched_truth_hexes:
            trail, meta = gt_trails_snapshot[solver_hex]
            closest = min(trail, key=lambda p: abs(p[3] - solver_ts))
            if abs(closest[3] - solver_ts) <= _MLAT_SOLVE_MAX_AGE_S + 30:
                t_alt_m = float(closest[2])
                # Altitude gate still applies — a >5 km altitude mismatch
                # against a hex's recent trail is a strong signal of stale
                # data or aircraft confusion regardless of identity claim.
                if not (solver_alt_m > 100 and t_alt_m > 100
                        and abs(solver_alt_m - t_alt_m) > _MLAT_ALT_GATE_M):
                    dist_km = haversine_km(
                        solver_lat, solver_lon, closest[0], closest[1],
                    )
                    if dist_km < best_dist_km:
                        speed_ms = float(meta.get("speed_ms", 0) or 0)
                        best_truth = (
                            solver_hex,
                            closest[0],
                            closest[1],
                            t_alt_m,
                            speed_ms,
                            meta.get("object_type", "aircraft"),
                            bool(meta.get("is_anomalous", False)),
                        )
                        best_dist_km = dist_km

        # 1b. Proximity fallback — runs when identity match was missing or
        #     out of threshold. The proximity loop iterates over all hexes
        #     except the one we already matched (in 1a), to avoid double-
        #     counting if it landed within threshold.
        if best_truth is None:
            for gt_hex, (trail, meta) in gt_trails_snapshot.items():
                if gt_hex in matched_truth_hexes:
                    continue
                closest = min(trail, key=lambda p: abs(p[3] - solver_ts))
                # Skip if the closest point is too far in time (trail too sparse)
                if abs(closest[3] - solver_ts) > _MLAT_SOLVE_MAX_AGE_S + 30:
                    continue
                # Altitude gate: skip if truth altitude known and differs too much.
                # Prevents matching solver result to a different nearby aircraft.
                t_alt_m = float(closest[2])
                if (solver_alt_m > 100 and t_alt_m > 100
                        and abs(solver_alt_m - t_alt_m) > _MLAT_ALT_GATE_M):
                    continue
                dist_km = haversine_km(solver_lat, solver_lon, closest[0], closest[1])
                if dist_km < best_dist_km:
                    best_dist_km = dist_km
                    speed_ms = float(meta.get("speed_ms", 0) or 0)
                    best_truth = (
                        gt_hex,
                        closest[0],
                        closest[1],
                        t_alt_m,
                        speed_ms,
                        meta.get("object_type", "aircraft"),
                        bool(meta.get("is_anomalous", False)),
                    )

        # 2. ADS-B fallback (current position — no trail history).
        if best_truth is None:
            for truth_entry in adsb_truth_pool:
                truth_hex, t_lat, t_lon, t_alt, t_speed, t_type, t_anom = truth_entry
                if truth_hex in matched_truth_hexes:
                    continue
                # Altitude gate (same logic as ground-truth loop above).
                if (solver_alt_m > 100 and t_alt > 100
                        and abs(solver_alt_m - t_alt) > _MLAT_ALT_GATE_M):
                    continue
                dist_km = haversine_km(solver_lat, solver_lon, t_lat, t_lon)
                if dist_km < best_dist_km:
                    best_dist_km = dist_km
                    best_truth = truth_entry

        if best_truth is None:
            # Diagnostic pass: find nearest truth at any distance (no threshold).
            # Also record nearest_hex so we can determine post-loop whether this
            # unmatched solve is a duplicate cycle for an already-matched aircraft
            # or a genuinely new aircraft position.
            nearest_km = float("inf")
            nearest_hex: str | None = None
            for gt_hex2, (trail2, _meta2) in gt_trails_snapshot.items():
                closest2 = min(trail2, key=lambda p: abs(p[3] - solver_ts))
                if abs(closest2[3] - solver_ts) > _MLAT_SOLVE_MAX_AGE_S + 30:
                    continue
                d = haversine_km(solver_lat, solver_lon, closest2[0], closest2[1])
                if d < nearest_km:
                    nearest_km = d
                    nearest_hex = gt_hex2
            for truth_entry2 in adsb_truth_pool:
                t_hex2, t_lat2, t_lon2 = truth_entry2[0], truth_entry2[1], truth_entry2[2]
                d = haversine_km(solver_lat, solver_lon, t_lat2, t_lon2)
                if d < nearest_km:
                    nearest_km = d
                    nearest_hex = t_hex2
            nearest_km_val = round(nearest_km, 1) if nearest_km < float("inf") else None
            if nearest_km_val is not None:
                unmatched_nearest_km.append(nearest_km)
            unmatched.append(
                {
                    "solve_key": key,
                    "solver_lat": round(solver_lat, 6),
                    "solver_lon": round(solver_lon, 6),
                    "n_nodes": n_nodes,
                    "nearest_truth_km": nearest_km_val,
                    "nearest_truth_hex": nearest_hex,
                    "rms_delay": round(float(r.get("rms_delay", 0) or 0), 3),
                    "timestamp_ms": int(r.get("timestamp_ms", 0)),
                }
            )
            continue

        truth_hex, t_lat, t_lon, t_alt, t_speed, t_type, t_anom = best_truth
        matched_truth_hexes.add(truth_hex)

        pos_err = best_dist_km
        vel_err = abs(solver_speed_ms - t_speed)
        alt_err = abs(solver_alt_m - t_alt)

        # Max bistatic angle across all contributing nodes (worst-geometry pair).
        # A high angle (→ 180°) means the aircraft is nearly between TX and RX,
        # which degrades GDOP and inflates position error.
        max_bistatic_deg: float | None = None
        for cid in r.get("contributing_node_ids", []):
            cfg = node_cfg_snap.get(cid, {})
            t_tx_lat = cfg.get("tx_lat")
            t_tx_lon = cfg.get("tx_lon")
            t_rx_lat = cfg.get("rx_lat")
            t_rx_lon = cfg.get("rx_lon")
            if not all((t_tx_lat, t_tx_lon, t_rx_lat, t_rx_lon)):
                continue
            ang = _bistatic_angle_deg(
                solver_lat, solver_lon,
                float(t_tx_lat), float(t_tx_lon),
                float(t_rx_lat), float(t_rx_lon),
            )
            if max_bistatic_deg is None or ang > max_bistatic_deg:
                max_bistatic_deg = ang

        pos_errors.append(pos_err)
        vel_errors.append(vel_err)
        alt_errors.append(alt_err)
        by_node_count.setdefault(n_nodes, []).append(pos_err)

        matches.append(
            {
                "solve_key": key,
                "solver_hex": multinode_hex_from_key(key),
                "solver_lat": round(solver_lat, 6),
                "solver_lon": round(solver_lon, 6),
                "truth_lat": round(t_lat, 6),
                "truth_lon": round(t_lon, 6),
                "truth_hex": truth_hex,
                "position_error_km": round(pos_err, 3),
                "solver_alt_m": round(solver_alt_m, 0),
                "truth_alt_m": round(t_alt, 0),
                "altitude_error_m": round(alt_err, 0),
                "solver_speed_ms": round(solver_speed_ms, 1),
                "truth_speed_ms": round(t_speed, 1),
                "velocity_error_ms": round(vel_err, 1),
                "n_nodes": n_nodes,
                "rms_delay": round(float(r.get("rms_delay", 0) or 0), 3),
                "rms_doppler": round(float(r.get("rms_doppler", 0) or 0), 2),
                "object_type": t_type,
                "is_anomalous": t_anom,
                "max_bistatic_angle_deg": round(max_bistatic_deg, 1) if max_bistatic_deg is not None else None,
                "timestamp_ms": int(r.get("timestamp_ms", 0)),
            }
        )

    n_solves = n_solver_cycles
    n_matched = len(matches)

    # n_unique_aircraft: matched aircraft + unmatched solves whose nearest truth
    # is NOT already claimed (i.e. not a duplicate cycle for an already-matched
    # aircraft).  Two unmatched solves pointing at the same unclaimed truth hex
    # count as one aircraft.
    unmatched_unclaimed_hexes: set[str] = set()
    n_unmatched_no_nearby_truth = 0
    for u in unmatched:
        u_hex = u.get("nearest_truth_hex")
        if u_hex is None:
            n_unmatched_no_nearby_truth += 1
        elif u_hex not in matched_truth_hexes:
            unmatched_unclaimed_hexes.add(u_hex)
    n_unique_aircraft = n_matched + len(unmatched_unclaimed_hexes) + n_unmatched_no_nearby_truth
    pos_errors.sort()
    vel_errors.sort()
    alt_errors.sort()

    # Feed rolling sample buffer for trend monitoring (one sample per matched track)
    ts_now_ms = int(now * 1000)
    for m in matches:
        state.mlat_samples.append(
            {
                "hex": m["truth_hex"],
                "error_km": m["position_error_km"],
                "n_nodes": m["n_nodes"],
                "max_bistatic_deg": m["max_bistatic_angle_deg"],
                "is_anomalous": bool(m.get("is_anomalous")),
                "ts": ts_now_ms,
            }
        )

    _refresh_mlat_accuracy_stats()

    by_node_count_out = {
        str(k): {
            "n": len(errs),
            "mean_km": round(sum(errs) / len(errs), 3),
            "median_km": round(_percentile(sorted(errs), 50), 3),
        }
        for k, errs in sorted(by_node_count.items())
    }

    result = {
        "computed_at": round(now, 1),
        "n_solves": n_solves,
        "n_solver_cycles": n_solver_cycles,
        "n_unique_aircraft": n_unique_aircraft,
        "n_matched": n_matched,
        "match_rate_pct": round(100.0 * n_matched / n_unique_aircraft, 1) if n_unique_aircraft else 0.0,
        "match_threshold_km": _MLAT_MATCH_THRESHOLD_KM,
        "n_truth_candidates": n_truth_gt + n_truth_live_adsb + n_truth_external,
        "truth_sources": {
            "ground_truth": n_truth_gt,
            "live_adsb": n_truth_live_adsb,
            "external_adsb": n_truth_external,
        },
        "position": {
            "mean_km": round(sum(pos_errors) / n_matched, 3) if n_matched else 0,
            "median_km": round(_percentile(pos_errors, 50), 3),
            "p95_km": round(_percentile(pos_errors, 95), 3),
            "max_km": round(pos_errors[-1], 3) if pos_errors else 0,
        },
        "velocity": {
            "mean_ms": round(sum(vel_errors) / n_matched, 1) if n_matched else 0,
            "median_ms": round(_percentile(vel_errors, 50), 1),
            "p95_ms": round(_percentile(vel_errors, 95), 1),
        },
        "altitude": {
            "mean_m": round(sum(alt_errors) / n_matched, 0) if n_matched else 0,
            "median_m": round(_percentile(alt_errors, 50), 0),
            "p95_m": round(_percentile(alt_errors, 95), 0),
        },
        "by_node_count": by_node_count_out,
        "tracks": matches[:100],
        "unmatched": {
            "n": len(unmatched),
            "nearest_truth": {
                "mean_km": round(sum(unmatched_nearest_km) / len(unmatched_nearest_km), 1)
                if unmatched_nearest_km
                else None,
                "median_km": round(_percentile(sorted(unmatched_nearest_km), 50), 1)
                if unmatched_nearest_km
                else None,
                "p95_km": round(_percentile(sorted(unmatched_nearest_km), 95), 1)
                if unmatched_nearest_km
                else None,
            },
            "tracks": sorted(unmatched, key=lambda x: x.get("nearest_truth_km") or 999)[:50],
        },
    }
    state.latest_mlat_verification_bytes = orjson.dumps(result, option=orjson.OPT_SERIALIZE_NUMPY)


def _ensure_custody_data():
    """Auto-register connected nodes in chain-of-custody if they lack entries."""
    from datetime import datetime, timezone

    from retina_custody.models import NodeIdentity

    now_iso = datetime.now(timezone.utc).isoformat()
    hour_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    with state.connected_nodes_lock:
        _custody_snapshot = list(state.connected_nodes.items())
    for nid, info in _custody_snapshot:
        if info.get("status") == "disconnected":
            continue
        if nid not in state.node_identities:
            fingerprint = hashlib.sha256(nid.encode()).hexdigest()[:16]
            identity = NodeIdentity(
                node_id=nid,
                public_key_pem=f"-----SIM-KEY-{nid[-8:]}-----",
                public_key_fingerprint=fingerprint,
                serial_number=f"SIM-{nid[-6:]}",
                signing_mode="software",
                registered_at=now_iso,
            )
            state.node_identities[nid] = identity

        if nid not in state.chain_entries:
            state.chain_entries[nid] = []

        entries = state.chain_entries[nid]
        if len(entries) > 168:
            state.chain_entries[nid] = entries = entries[-168:]
        if not entries or entries[-1].get("hour_utc") != hour_utc:
            prev_hash = entries[-1].get("entry_hash", "0" * 64) if entries else "0" * 64
            content_hash = hashlib.sha256(f"{nid}:{hour_utc}".encode()).hexdigest()
            entry_hash = hashlib.sha256(f"{prev_hash}:{content_hash}".encode()).hexdigest()
            entries.append(
                {
                    "node_id": nid,
                    "hour_utc": hour_utc,
                    "prev_hash": prev_hash,
                    "content_hash": content_hash,
                    "entry_hash": entry_hash,
                    "_verified": True,
                    "_received_at": now_iso,
                }
            )

        if nid not in state.iq_commitments:
            state.iq_commitments[nid] = []
        if not state.iq_commitments[nid]:
            state.iq_commitments[nid].append(
                {
                    "node_id": nid,
                    "capture_id": f"iq-{nid[-8:]}-001",
                    "sha256": hashlib.sha256(f"iq:{nid}".encode()).hexdigest(),
                    "_received_at": now_iso,
                }
            )


def _evict_stale_pipelines(nodes_snapshot: list):
    """Remove PassiveRadarPipeline for nodes disconnected > 2 h."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stale = []
    for nid, info in nodes_snapshot:
        if info.get("status") != "disconnected":
            continue
        hb = info.get("last_heartbeat")
        if not hb:
            stale.append(nid)
            continue
        try:
            hb_time = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            if (now - hb_time).total_seconds() > 7200:
                stale.append(nid)
        except Exception:
            logging.debug("unparseable heartbeat timestamp for %s: %r", nid, hb)
    for nid in stale:
        state.node_pipelines.pop(nid, None)
    if stale:
        logging.debug("Evicted %d stale node pipelines", len(stale))


async def analytics_refresh_task():
    """Pre-compute analytics/nodes/overlaps every 30 s in a dedicated thread."""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(5)
    while True:
        try:
            await loop.run_in_executor(_analytics_executor, _refresh_analytics_and_nodes)
            await loop.run_in_executor(_analytics_executor, state.node_analytics.maybe_auto_save)
            from routes.admin import check_node_health

            check_node_health()
            logging.debug("Analytics refresh completed")
            state.task_last_success["analytics_refresh"] = time.time()
        except Exception:
            state.task_error_counts["analytics_refresh"] += 1
            logging.exception("Analytics refresh failed")
        await asyncio.sleep(ANALYTICS_REFRESH_INTERVAL_S)
