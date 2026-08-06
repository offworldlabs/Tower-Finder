"""blah2 bridge — polls live retnodes' /api/detection endpoints and injects
real passive-radar detections directly into the RETINA frame pipeline.

The blah2 delay axis is in km (bistatic range difference).
RETINA expects delay in µs.  Conversion: delay_us = delay_km / C_KM_US.

Nodes are defined in ``blah2_nodes.json`` (see that file's _README for the
schema), loaded through the same runtime-config overlay as nodes_config.json:
``backend/config/`` holds the source-controlled default, the app reads the copy
under ``backend/data/runtime/``.  Set ``BLAH2_NODES_FILE`` to point somewhere
else entirely.  One polling task runs per configured node, so adding a node is
a config change, not a code change.

Node geometry is *not* discoverable at runtime — /api/config on these hosts
returns only a truth flag — which is why it lives in the config file and must
match the real hardware: tx/rx positions and fc feed the bistatic solver
directly.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from config.constants import (
    BLAH2_MAX_FAILURES as MAX_FAILURES,
)
from config.constants import (
    BLAH2_POLL_INTERVAL_S as POLL_INTERVAL_S,
)
from config.constants import (
    BLAH2_RECONNECT_DELAY_S as RECONNECT_DELAY_S,
)
from config.constants import (
    BLAH2_STALE_THRESHOLD_S as STALE_THRESHOLD_S,
)
from config.constants import (
    C_KM_US,
)
from core import state
from core.runtime_config import default_source_path, runtime_path
from core.task_registry import register_task

log = logging.getLogger("blah2_bridge")

CONFIG_FILENAME = "blah2_nodes.json"

# Each node polls every POLL_INTERVAL_S (1 s); flag it stale past this.
BRIDGE_STALE_INTERVAL_S = 10

# Fields every node must supply — without these the bistatic solve is undefined.
_REQUIRED = ("node_id", "detection_url", "rx_lat", "rx_lon", "tx_lat", "tx_lon", "fc_hz")

# Optional fields and the defaults applied when a node omits them.
_OPTIONAL_DEFAULTS = {
    "rx_alt_ft": 0.0,
    "tx_alt_ft": 0.0,
    "fs_hz": 2_000_000,
    "doppler_min": -300,
    "doppler_max": 300,
    "min_doppler": 15,
    # 42-degree Yagi per the actual hardware; the old 120 was a stale guess
    # that tripled every radar3 beam gate and coverage sector.
    "beam_width_deg": 42.0,
    "max_range_km": 140,
}


@dataclass(frozen=True)
class Blah2Node:
    """One live blah2 node: RETINA identity, endpoint, and pipeline config."""

    node_id: str
    detection_url: str
    config: dict = field(repr=False)

    @property
    def peer(self) -> str:
        """Hostname, for display in the node list."""
        return httpx.URL(self.detection_url).host


class Blah2ConfigError(ValueError):
    """A node entry in the config file is unusable."""


def task_key(node_id: str) -> str:
    """Per-node key in state.task_last_success / the staleness registry."""
    return f"blah2_bridge:{node_id}"


def config_file_path() -> Path:
    """Where the node list is read from.

    BLAH2_NODES_FILE wins; otherwise the runtime overlay copy; otherwise the
    source-controlled default that ships in the image.
    """
    override = os.getenv("BLAH2_NODES_FILE")
    if override:
        return Path(override)
    overlay = runtime_path(CONFIG_FILENAME)
    if overlay.exists():
        return overlay
    return default_source_path(CONFIG_FILENAME) or overlay


def _build_node(entry: dict) -> Blah2Node:
    """Validate one config entry and expand it into a Blah2Node.

    Raises Blah2ConfigError with a message naming the offending field.
    """
    if not isinstance(entry, dict):
        raise Blah2ConfigError(f"node entry must be an object, got {type(entry).__name__}")

    missing = [k for k in _REQUIRED if entry.get(k) is None]
    if missing:
        raise Blah2ConfigError(f"missing required field(s): {', '.join(missing)}")

    node_id = str(entry["node_id"]).strip()
    if not node_id:
        raise Blah2ConfigError("node_id is empty")

    url = str(entry["detection_url"]).strip()
    if not url.startswith(("http://", "https://")):
        raise Blah2ConfigError(f"{node_id}: detection_url must be http(s), got {url!r}")

    cfg: dict = {"node_id": node_id}
    for key in ("rx_lat", "rx_lon", "tx_lat", "tx_lon", "fc_hz"):
        try:
            cfg[key] = float(entry[key])
        except (TypeError, ValueError) as exc:
            raise Blah2ConfigError(f"{node_id}: {key} is not a number: {entry[key]!r}") from exc
    for key, default in _OPTIONAL_DEFAULTS.items():
        raw = entry.get(key, default)
        try:
            cfg[key] = float(raw)
        except (TypeError, ValueError) as exc:
            raise Blah2ConfigError(f"{node_id}: {key} is not a number: {raw!r}") from exc

    for key, lo, hi in (("rx_lat", -90, 90), ("tx_lat", -90, 90),
                        ("rx_lon", -180, 180), ("tx_lon", -180, 180)):
        if not lo <= cfg[key] <= hi:
            raise Blah2ConfigError(f"{node_id}: {key}={cfg[key]} out of range [{lo}, {hi}]")
    if cfg["fc_hz"] <= 0 or cfg["fs_hz"] <= 0:
        raise Blah2ConfigError(f"{node_id}: fc_hz and fs_hz must be positive")

    # The pipeline factory and analytics read both spellings; keep them in step.
    cfg["FC"] = cfg["fc_hz"]
    cfg["Fs"] = cfg["fs_hz"]
    if entry.get("notes"):
        cfg["notes"] = str(entry["notes"])

    return Blah2Node(node_id=node_id, detection_url=url, config=cfg)


def load_nodes(path: Path | None = None) -> list[Blah2Node]:
    """Read the node list from the config file.

    A malformed individual entry is logged and skipped so one bad node cannot
    keep the others off the air; a missing or unreadable file yields an empty
    list and the bridge simply runs no pollers.  Every failure is logged at
    error level — and a node that fails to load is also visibly absent from
    /api/radar/nodes.
    """
    path = Path(path) if path else config_file_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        log.error("blah2_bridge: node config %s not found — no live nodes will be polled", path)
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.error("blah2_bridge: cannot read node config %s: %s", path, exc)
        return []

    entries = raw.get("nodes") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        log.error("blah2_bridge: %s must contain a 'nodes' array", path)
        return []

    nodes: list[Blah2Node] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for entry in entries:
        try:
            node = _build_node(entry)
        except Blah2ConfigError as exc:
            log.error("blah2_bridge: skipping node in %s — %s", path, exc)
            continue
        # Duplicates would fight over the same slot in state.connected_nodes.
        if node.node_id in seen_ids:
            log.error("blah2_bridge: skipping duplicate node_id %s in %s", node.node_id, path)
            continue
        if node.detection_url in seen_urls:
            log.error(
                "blah2_bridge: skipping %s — detection_url %s already used by another node",
                node.node_id, node.detection_url,
            )
            continue
        seen_ids.add(node.node_id)
        seen_urls.add(node.detection_url)
        nodes.append(node)

    log.info("blah2_bridge: loaded %d node(s) from %s: %s",
             len(nodes), path, ", ".join(n.node_id for n in nodes) or "(none)")
    for node in nodes:
        register_task(task_key(node.node_id), BRIDGE_STALE_INTERVAL_S)
    return nodes


def _register_node(node: Blah2Node):
    """Register a node in state as a real (non-synthetic) connected node."""
    cfg_hash = hashlib.sha256(
        json.dumps(node.config, sort_keys=True).encode()
    ).hexdigest()[:16]
    with state.connected_nodes_lock:
        state.connected_nodes[node.node_id] = {
            "config_hash": cfg_hash,
            "config": node.config,
            "status": "active",
            "last_heartbeat": "",
            "peer": node.peer,
            "is_synthetic": False,
            "capabilities": {"adsb_report": True},
        }
    state.node_analytics.register_node(node.node_id, node.config)
    state.node_associator.register_node(node.node_id, node.config)
    log.info("blah2_bridge: registered node %s", node.node_id)


def _convert_frame(raw: dict, node_id: str) -> dict | None:
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
        "_node_id": node_id,
    }
    if adsb_out:
        frame["adsb"] = adsb_out
    return frame


async def blah2_bridge_task(node: Blah2Node):
    """Long-running background task: poll one blah2 node and inject frames."""
    _register_node(node)
    key = task_key(node.node_id)
    failures = 0
    last_ts = 0

    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
        while True:
            try:
                resp = await client.get(node.detection_url)
                resp.raise_for_status()
                raw = resp.json()

                frame = _convert_frame(raw, node.node_id)
                if frame is not None:
                    ts_ms = raw.get("timestamp", 0)
                    if ts_ms != last_ts:   # skip duplicate frames
                        last_ts = ts_ms
                        # Update heartbeat timestamp
                        if node.node_id in state.connected_nodes:
                            from datetime import datetime, timezone
                            with state.connected_nodes_lock:
                                state.connected_nodes[node.node_id]["last_heartbeat"] = (
                                    datetime.now(timezone.utc).isoformat()
                                )
                        try:
                            state.frame_queue.put_nowait((node.node_id, frame))
                        except Exception:
                            state.bump_counter("frames_dropped")

                failures = 0
                state.task_last_success[key] = time.time()
                await asyncio.sleep(POLL_INTERVAL_S)

            except (httpx.HTTPError, Exception) as exc:
                failures += 1
                state.task_error_counts[key] += 1
                if failures >= MAX_FAILURES:
                    log.warning(
                        "blah2_bridge[%s]: %d consecutive failures (%s), backing off %ds",
                        node.node_id, failures, exc, RECONNECT_DELAY_S,
                    )
                    # Mark node as degraded but don't remove it
                    if node.node_id in state.connected_nodes:
                        with state.connected_nodes_lock:
                            state.connected_nodes[node.node_id]["status"] = "degraded"
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    failures = 0
                    # Re-register in case state was reset
                    _register_node(node)
                else:
                    await asyncio.sleep(POLL_INTERVAL_S)
