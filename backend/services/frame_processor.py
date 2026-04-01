"""Detection frame processing + aircraft JSON builder.

Contains the synchronous per-frame pipeline that runs in a thread pool
and the combined aircraft.json builder used by the flush task.
"""

import logging
import math
import time
from collections import defaultdict, deque
from typing import Optional

from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from retina_tracker.track import TrackState
from services.storage import archive_detections

# ── Archive batching ──────────────────────────────────────────────────────────
# Instead of writing every frame to disk immediately (slow I/O in the hot path),
# collect frames in memory and flush them periodically from a background task.
_archive_buffer: dict[str, list[dict]] = defaultdict(list)
_ARCHIVE_FLUSH_INTERVAL = 30          # seconds between batch writes
_ARCHIVE_BATCH_MAX = 200              # flush if a node accumulates this many


def _flush_archive_node(node_id: str):
    """Write buffered frames for one node to disk in a single call."""
    frames = _archive_buffer.pop(node_id, [])
    if not frames:
        return
    try:
        archive_detections(node_id, frames)
    except Exception:
        logging.debug("Archive flush failed for %s (%d frames)", node_id, len(frames))


def flush_all_archive_buffers():
    """Flush every node's buffered frames. Called from the background task."""
    node_ids = list(_archive_buffer.keys())
    for nid in node_ids:
        _flush_archive_node(nid)


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_hex_key(hex_code: str) -> str:
    return str(hex_code or "").strip().lower()


def position_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = (lat1 - lat2) * 111.0
    dlon = (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.sqrt(dlat ** 2 + dlon ** 2)


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

def process_one_frame(node_id: str, frame: dict, default_pipeline: PassiveRadarPipeline):
    """CPU-heavy frame processing — never runs on the event loop."""

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

    state.node_analytics.record_detection_frame(node_id, frame)

    assoc = state.node_associator.submit_frame(node_id, frame, frame.get("timestamp", 0))
    if assoc:
        solver_inputs = state.node_associator.format_candidates_for_solver(assoc)
        node_cfgs = get_node_configs()
        for s_in in solver_inputs:
            if s_in["n_nodes"] < 2:
                continue
            # Off-load to background solver thread — keeps the hot path fast.
            try:
                state.solver_queue.put_nowait((s_in, node_cfgs))
            except Exception:
                pass  # queue.Full: drop candidate; solver is lagging

    # Extract embedded ADS-B
    adsb_list = frame.get("adsb")
    if adsb_list:
        ts_ms = int(time.time() * 1000)
        for entry in adsb_list:
            if not isinstance(entry, dict):
                continue
            hex_code = entry.get("hex")
            if not hex_code:
                continue
            lat = entry.get("lat", 0)
            lon = entry.get("lon", 0)
            if not lat or not lon or not math.isfinite(lat) or not math.isfinite(lon):
                continue
            state.adsb_aircraft[hex_code] = {
                "hex": hex_code,
                "flight": entry.get("flight", ""),
                "lat": lat,
                "lon": lon,
                "alt_baro": entry.get("alt_baro", 0),
                "gs": entry.get("gs", 0),
                "track": entry.get("track", 0),
                "last_seen_ms": ts_ms,
            }

    pipeline = get_or_create_node_pipeline(node_id, default_pipeline)
    pipeline.process_frame(frame)

    state.node_analytics.maybe_auto_save()
    # Queue frame for batched archival instead of blocking per-frame
    _archive_buffer[node_id].append(frame)
    if len(_archive_buffer[node_id]) >= _ARCHIVE_BATCH_MAX:
        _flush_archive_node(node_id)


# ── Multi-node result → tar1090-compatible dict ──────────────────────────────

def multinode_to_aircraft(key: str, r: dict) -> dict:
    speed_ms = math.sqrt(r["vel_east"] ** 2 + r["vel_north"] ** 2)
    heading = math.degrees(math.atan2(r["vel_east"], r["vel_north"])) % 360
    return {
        "hex": f"mn{abs(hash(key)) % 0xFFFF:04x}",
        "type": "multinode_solve",
        "flight": f"MN{r['n_nodes']}N",
        "alt_baro": round(r["alt_m"] / 0.3048),
        "alt_geom": round(r["alt_m"] / 0.3048),
        "gs": round(speed_ms * 1.94384, 1),
        "track": round(heading, 1),
        "lat": round(r["lat"], 5),
        "lon": round(r["lon"], 5),
        "seen": 0,
        "messages": r["n_measurements"],
        "rssi": -round(1.0 / max(r.get("rms_delay", 1), 0.01), 1),
        "multinode": True,
        "n_nodes": r["n_nodes"],
        "contributing_node_ids": r.get("contributing_node_ids", []),
        "rms_delay": round(r["rms_delay"], 3),
        "rms_doppler": round(r["rms_doppler"], 2),
    }


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    dlonr = math.radians(lon2 - lon1)
    y = math.sin(dlonr) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlonr)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _enu_to_lla(rx_lat: float, rx_lon: float, east_km: float, north_km: float) -> list[float]:
    cos_lat = max(0.1, math.cos(math.radians(rx_lat)))
    lat = rx_lat + north_km / 111.32
    lon = rx_lon + east_km / (111.32 * cos_lat)
    return [float(lat), float(lon)]


def _build_single_node_arc(track_or_delay, node_cfg: dict) -> Optional[list[list[float]]]:
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

    cos_lat = max(0.1, math.cos(math.radians((rx_lat + tx_lat) / 2.0)))
    tx_east_km = (tx_lon - rx_lon) * 111.32 * cos_lat
    tx_north_km = (tx_lat - rx_lat) * 111.32
    baseline_km = math.hypot(tx_east_km, tx_north_km)
    differential_range_km = delay_us * 0.299792458

    def _differential_at(range_km: float, bearing_deg: float) -> float:
        bearing_rad = math.radians(bearing_deg)
        east_km = math.sin(bearing_rad) * range_km
        north_km = math.cos(bearing_rad) * range_km
        tx_dist_km = math.hypot(east_km - tx_east_km, north_km - tx_north_km)
        return tx_dist_km + range_km - baseline_km

    points: list[list[float]] = []
    steps = 36
    half_beam_deg = beam_width_deg / 2.0
    for step in range(steps + 1):
        bearing_deg = beam_azimuth_deg - half_beam_deg + beam_width_deg * (step / steps)
        lo = 0.0
        hi = max_range_km
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
    aircraft: list[dict] = []

    def _dead_reckon(entry: dict, ts: float):
        """Return dead-reckoned (lat, lon) from the last ADS-B fix.

        Uses stored gs (knots) and track (degrees from north) to extrapolate
        the aircraft's current position since the last fix.  Capped at 60s to
        avoid large extrapolation errors.
        """
        lat_fix = entry.get("lat", 0.0)
        lon_fix = entry.get("lon", 0.0)
        elapsed = min(ts - entry.get("last_seen_ms", 0) / 1000.0, 60.0)
        gs_knots = entry.get("gs", 0.0)
        track_deg = entry.get("track", 0.0)
        if elapsed <= 0.0 or gs_knots <= 0.0:
            return lat_fix, lon_fix
        gs_m_s = gs_knots * 0.514444
        track_rad = math.radians(track_deg)
        cos_lat = math.cos(math.radians(lat_fix)) or 1e-9
        lat_dr = lat_fix + (gs_m_s * math.cos(track_rad) / 111_320.0) * elapsed
        lon_dr = lon_fix + (gs_m_s * math.sin(track_rad) / (111_320.0 * cos_lat)) * elapsed
        return lat_dr, lon_dr

    def _fresh_adsb(ac_hex: str):
        """Return state.adsb_aircraft entry if it's recent (< 60 s), else None."""
        entry = state.adsb_aircraft.get(ac_hex)
        if not entry:
            return None
        if now - entry.get("last_seen_ms", 0) / 1000 > 60:
            return None
        return entry

    def _track_entry(ac_hex, track, node_cfg):
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
        alt_ft = adsb.get("alt_baro", track.alt_ft) if adsb else track.alt_ft
        gs = round(adsb.get("gs", track.speed_knots) if adsb else track.speed_knots, 1)
        heading = round(adsb.get("track", track.track_angle) if adsb else track.track_angle, 1)

        # Build ambiguity arc only when we have no ADS-B seed — ADS-B-seeded
        # solver positions are tight enough that arcs add visual noise.
        ambiguity_arc = (
            _build_single_node_arc(track, node_cfg)
            if not has_adsb
            else None
        )
        if ambiguity_arc and position_source == "solver_single_node":
            midpoint = ambiguity_arc[len(ambiguity_arc) // 2]
            lat = round(midpoint[0], 6)
            lon = round(midpoint[1], 6)
            position_source = "single_node_ellipse_arc"

        append_track_history(ac_hex, lat, lon, alt_ft, now)

        # Record ADS-B-verified positions as calibration points for empirical coverage.
        if has_adsb:
            nid = node_cfg.get("node_id")
            if nid:
                state.node_analytics.record_calibration_point(nid, solver_lat, solver_lon)
            # Track accuracy: haversine(solver, adsb) per aircraft per update
            adsb_lat = adsb.get("lat", 0)
            adsb_lon = adsb.get("lon", 0)
            if adsb_lat and adsb_lon:
                err_km = position_distance_km(solver_lat, solver_lon, adsb_lat, adsb_lon)
                _record_accuracy_sample(ac_hex, err_km, position_source, now)

        rms_delay = round(getattr(track, "rms_delay", 0.0) or 0.0, 3)
        rms_doppler = round(getattr(track, "rms_doppler", 0.0) or 0.0, 2)

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
            "seen": 0,
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
        }

    # 1. Per-node pipelines
    for pipeline in list(state.node_pipelines.values()):
        for track in list(pipeline.geolocated_tracks.values()):
            ac_hex = track.adsb_hex or track.hex_id
            if ac_hex in seen_hex:
                continue
            seen_hex.add(ac_hex)
            aircraft.append(_track_entry(ac_hex, track, pipeline.config))

    # 2. Default pipeline
    for track in list(default_pipeline.geolocated_tracks.values()):
        ac_hex = track.adsb_hex or track.hex_id
        if ac_hex in seen_hex:
            continue
        seen_hex.add(ac_hex)
        aircraft.append(_track_entry(ac_hex, track, default_pipeline.config))

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
            cos_lat = math.cos(math.radians(ac["lat"])) or 1e-9
            ac["lat"] = round(ac["lat"] + (vel_north_m_s / 111_320.0) * elapsed, 5)
            ac["lon"] = round(ac["lon"] + (vel_east_m_s / (111_320.0 * cos_lat)) * elapsed, 5)
        if ac["hex"] not in seen_hex:
            seen_hex.add(ac["hex"])
            append_track_history(ac["hex"], ac["lat"], ac["lon"], ac["alt_baro"], now)
            ac["recent_positions"] = list(state.track_histories.get(ac["hex"], []))
            ac["ground_truth_hex"] = resolve_ground_truth_hex(ac["hex"], ac["lat"], ac["lon"])
            aircraft.append(ac)
    for k in stale_mn:
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

    # 5. Pending detection arcs from tracker tracks not yet geolocated.
    # These arcs appear immediately on each detection without waiting for
    # M-of-N promotion + LM solver convergence.
    pending_arcs = []
    for pipeline in list(state.node_pipelines.values()):
        node_cfg = pipeline.config
        for track in list(pipeline.tracker.tracks):
            # Only emit arcs for confirmed tracks (M-of-N promoted).
            # TENTATIVE tracks are single-detection hypotheses; showing arcs
            # for them produces too much clutter before a real target is confirmed.
            if track.state_status == TrackState.TENTATIVE:
                continue
            # Suppress when ADS-B already gives a precise position.
            if track.adsb_hex:
                _ae = state.adsb_aircraft.get(track.adsb_hex)
                if _ae and now - _ae.get("last_seen_ms", 0) / 1000 < 60:
                    continue
            meas = track.history.get("measurements")
            if not meas:
                continue
            # measurements list can contain None for missed detections
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

    gt_snapshot = {
        h: list(trail)[-30:]
        for h, trail in list(state.ground_truth_trails.items())
        if trail
    }

    gt_meta = dict(state.ground_truth_meta)

    return {
        "now": now,
        "messages": len(aircraft),
        "aircraft": aircraft,
        "detection_arcs": pending_arcs,
        "ground_truth": gt_snapshot,
        "ground_truth_meta": gt_meta,
        "anomaly_hexes": sorted(state.anomaly_hexes),
    }
