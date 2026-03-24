"""Detection frame processing + aircraft JSON builder.

Contains the synchronous per-frame pipeline that runs in a thread pool
and the combined aircraft.json builder used by the flush task.
"""

import logging
import math
import time
from collections import defaultdict, deque

from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from retina_geolocator.multinode_solver import solve_multinode
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
    for gt_hex, trail in state.ground_truth_trails.items():
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
            try:
                result = solve_multinode(s_in, node_cfgs)
            except Exception:
                result = None
            if result and result.get("success"):
                key = f"mn-{result['timestamp_ms']}-{result['lat']:.3f}"
                state.multinode_tracks[key] = result

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
        "rms_delay": round(r["rms_delay"], 3),
        "rms_doppler": round(r["rms_doppler"], 2),
    }


# ── Combined aircraft.json builder ───────────────────────────────────────────

def build_combined_aircraft_json(default_pipeline: PassiveRadarPipeline) -> dict:
    """Merge per-node pipelines, default pipeline, multinode, ADS-B into one feed."""
    now = time.time()
    seen_hex: set[str] = set()
    aircraft: list[dict] = []

    def _fresh_adsb(ac_hex: str):
        """Return state.adsb_aircraft entry if it's recent (< 60 s), else None."""
        entry = state.adsb_aircraft.get(ac_hex)
        if not entry:
            return None
        if now - entry.get("last_seen_ms", 0) / 1000 > 60:
            return None
        return entry

    def _track_entry(ac_hex, track):
        # Prefer live ADS-B position over potentially stale pipeline position.
        # The pipeline position can lag by queue_depth × processing_time when
        # the frame queue is saturated, but state.adsb_aircraft is updated on
        # every incoming TCP frame via the fast-path extractor.
        adsb = _fresh_adsb(ac_hex)
        lat = round(adsb["lat"] if adsb and adsb.get("lat") else track.lat, 6)
        lon = round(adsb["lon"] if adsb and adsb.get("lon") else track.lon, 6)
        alt_ft = adsb.get("alt_baro", track.alt_ft) if adsb else track.alt_ft
        gs = round(adsb.get("gs", track.speed_knots) if adsb else track.speed_knots, 1)
        heading = round(adsb.get("track", track.track_angle) if adsb else track.track_angle, 1)
        append_track_history(ac_hex, lat, lon, alt_ft, now)
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
            "recent_positions": list(state.track_histories.get(ac_hex, [])),
        }

    # 1. Per-node pipelines
    for pipeline in list(state.node_pipelines.values()):
        for track in list(pipeline.geolocated_tracks.values()):
            ac_hex = track.adsb_hex or track.hex_id
            if ac_hex in seen_hex:
                continue
            seen_hex.add(ac_hex)
            aircraft.append(_track_entry(ac_hex, track))

    # 2. Default pipeline
    for track in list(default_pipeline.geolocated_tracks.values()):
        ac_hex = track.adsb_hex or track.hex_id
        if ac_hex in seen_hex:
            continue
        seen_hex.add(ac_hex)
        aircraft.append(_track_entry(ac_hex, track))

    # 3. Multi-node solver
    stale_mn = []
    for key, r in list(state.multinode_tracks.items()):
        age_s = now - r.get("timestamp_ms", 0) / 1000
        if age_s > 60:
            stale_mn.append(key)
            continue
        ac = multinode_to_aircraft(key, r)
        if ac["hex"] not in seen_hex:
            seen_hex.add(ac["hex"])
            append_track_history(ac["hex"], ac["lat"], ac["lon"], ac["alt_baro"], now)
            ac["recent_positions"] = list(state.track_histories.get(ac["hex"], []))
            ac["ground_truth_hex"] = resolve_ground_truth_hex(ac["hex"], ac["lat"], ac["lon"])
            aircraft.append(ac)
    for k in stale_mn:
        state.multinode_tracks.pop(k, None)

    # 4. ADS-B correlated aircraft
    stale_adsb = []
    for hex_code, entry in list(state.adsb_aircraft.items()):
        if hex_code in seen_hex:
            continue
        age_s = now - entry.get("last_seen_ms", 0) / 1000
        if age_s > 60:
            stale_adsb.append(hex_code)
            continue
        lat, lon = entry.get("lat", 0), entry.get("lon", 0)
        if not lat or not lon or not math.isfinite(lat) or not math.isfinite(lon):
            continue
        seen_hex.add(hex_code)
        append_track_history(hex_code, lat, lon, entry.get("alt_baro", 0), now)
        aircraft.append({
            "hex": hex_code,
            "ground_truth_hex": resolve_ground_truth_hex(hex_code, lat, lon),
            "type": "adsb_icao",
            "flight": (entry.get("flight") or hex_code).strip(),
            "alt_baro": entry.get("alt_baro", 0),
            "alt_geom": entry.get("alt_baro", 0),
            "gs": round(entry.get("gs", 0), 1),
            "track": round(entry.get("track", 0), 1),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "seen": 0,
            "messages": 1,
            "rssi": -15.0,
            "recent_positions": list(state.track_histories.get(hex_code, [])),
        })
    for k in stale_adsb:
        state.adsb_aircraft.pop(k, None)

    gt_snapshot = {
        h: list(trail)[-30:]
        for h, trail in state.ground_truth_trails.items()
        if trail
    }

    return {
        "now": now,
        "messages": len(aircraft),
        "aircraft": aircraft,
        "ground_truth": gt_snapshot,
    }
