"""blah2 bridge — polls radar3.retnode.com /api/detection and injects
real passive-radar detections directly into the RETINA frame pipeline.

The blah2 delay axis is in km (bistatic range difference).
RETINA expects delay in µs.  Conversion: delay_us = delay_km / C_KM_US.

Node geometry (from radar3.retnode.com/api/config):
  RX  Wilderness   33.939182, -84.65191,   320 m
  TX  WXIA HDTV    33.939182, -84.331844,  650 m
  FC  195 MHz      FS 2 MHz
"""

import asyncio
import logging
import math
import time

import httpx

from core import state
from config.constants import (
    C_KM_US,
    BLAH2_POLL_INTERVAL_S as POLL_INTERVAL_S,
    BLAH2_STALE_THRESHOLD_S as STALE_THRESHOLD_S,
    BLAH2_RECONNECT_DELAY_S as RECONNECT_DELAY_S,
    BLAH2_MAX_FAILURES as MAX_FAILURES,
)

log = logging.getLogger("blah2_bridge")

# ── Node identity ─────────────────────────────────────────────────────────────────
NODE_ID = "radar3-retnode"
DETECTION_URL = "https://radar3.retnode.com/api/detection"

# Node config pushed to state.connected_nodes and pipeline factory
_NODE_CONFIG = {
    "node_id": NODE_ID,
    "Fs": 2_000_000,
    "FC": 195_000_000,
    "fs_hz": 2_000_000,
    "fc_hz": 195_000_000,
    "rx_lat": 33.939182,
    "rx_lon": -84.651910,
    "rx_alt_ft": 1050,          # 320 m
    "tx_lat": 33.939182,
    "tx_lon": -84.331844,
    "tx_alt_ft": 2133,          # 650 m
    "doppler_min": -300,
    "doppler_max": 300,
    "min_doppler": 15,
    "beam_width_deg": 120,
    "max_range_km": 140,
}


def _register_node():
    """Register radar3 in state as a real (non-synthetic) connected node."""
    import hashlib, json
    cfg_hash = hashlib.sha256(json.dumps(_NODE_CONFIG, sort_keys=True).encode()).hexdigest()[:16]
    with state.connected_nodes_lock:
        state.connected_nodes[NODE_ID] = {
            "config_hash": cfg_hash,
            "config": _NODE_CONFIG,
            "status": "active",
            "last_heartbeat": "",
            "peer": "radar3.retnode.com",
            "is_synthetic": False,
            "capabilities": {"adsb_report": True},
        }
    state.node_analytics.register_node(NODE_ID, _NODE_CONFIG)
    state.node_associator.register_node(NODE_ID, _NODE_CONFIG)
    log.info("blah2_bridge: registered node %s", NODE_ID)


def _convert_frame(raw: dict) -> dict | None:
    """Convert a blah2 /api/detection response to a RETINA frame dict."""
    ts_ms = raw.get("timestamp")
    delays_km = raw.get("delay", [])
    dopplers_hz = raw.get("doppler", [])
    snrs = raw.get("snr", [])

    if not ts_ms or not delays_km:
        return None

    # Reject stale frames (blah2 sometimes serves cached responses)
    age_s = time.time() - ts_ms / 1000.0
    if abs(age_s) > STALE_THRESHOLD_S:
        return None

    # Convert delay: km → µs
    delays_us = [d / C_KM_US for d in delays_km]

    # Convert blah2 adsb entries to RETINA format
    adsb_out = []
    for entry in raw.get("adsb", []):
        if not isinstance(entry, dict):
            adsb_out.append(None)
            continue
        lat = entry.get("lat") or entry.get("latitude")
        lon = entry.get("lon") or entry.get("longitude")
        if lat and lon and math.isfinite(lat) and math.isfinite(lon):
            adsb_out.append({
                "hex": entry.get("hex") or entry.get("icao"),
                "lat": lat,
                "lon": lon,
                "alt_baro": entry.get("alt_baro") or entry.get("altitude", 0),
                "gs": entry.get("gs") or entry.get("speed", 0),
                "track": entry.get("track") or entry.get("heading", 0),
                "flight": entry.get("flight") or entry.get("callsign", ""),
            })
        else:
            adsb_out.append(None)

    frame = {
        "timestamp": ts_ms,
        "delay": delays_us,
        "doppler": dopplers_hz,
        "snr": snrs,
        "_node_id": NODE_ID,
    }
    if adsb_out:
        frame["adsb"] = adsb_out
    return frame


async def blah2_bridge_task():
    """Long-running background task: poll blah2 and inject frames."""
    _register_node()
    failures = 0
    last_ts = 0

    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
        while True:
            try:
                resp = await client.get(DETECTION_URL)
                resp.raise_for_status()
                raw = resp.json()

                frame = _convert_frame(raw)
                if frame is not None:
                    ts_ms = raw.get("timestamp", 0)
                    if ts_ms != last_ts:   # skip duplicate frames
                        last_ts = ts_ms
                        # Update heartbeat timestamp
                        if NODE_ID in state.connected_nodes:
                            from datetime import datetime, timezone
                            with state.connected_nodes_lock:
                                state.connected_nodes[NODE_ID]["last_heartbeat"] = (
                                    datetime.now(timezone.utc).isoformat()
                                )
                        try:
                            state.frame_queue.put_nowait((NODE_ID, frame))
                        except Exception:
                            state.frames_dropped += 1

                failures = 0
                await asyncio.sleep(POLL_INTERVAL_S)

            except (httpx.HTTPError, Exception) as exc:
                failures += 1
                if failures >= MAX_FAILURES:
                    log.warning(
                        "blah2_bridge: %d consecutive failures (%s), backing off %ds",
                        failures, exc, RECONNECT_DELAY_S,
                    )
                    # Mark node as degraded but don't remove it
                    if NODE_ID in state.connected_nodes:
                        with state.connected_nodes_lock:
                            state.connected_nodes[NODE_ID]["status"] = "degraded"
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    failures = 0
                    # Re-register in case state was reset
                    _register_node()
                else:
                    await asyncio.sleep(POLL_INTERVAL_S)
