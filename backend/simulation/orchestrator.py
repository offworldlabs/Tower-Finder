"""
Fleet Orchestrator — Runs a large-scale test network of 100-1000 synthetic
nodes against a RETINA server using a shared SimulationWorld.

All nodes observe the same simulated aircraft from their own geometry.
Detection frames are streamed over TCP to the server, exercising:
  - TCP protocol (HELLO → CONFIG → DETECTION/HEARTBEAT)
  - Passive radar pipeline (tracker + solver)
  - Inter-node association and multi-node solver
  - Node analytics (trust scoring, reputation)
  - Data archival
  - Live map feed (aircraft.json / WebSocket)

Usage:
    # 200 nodes against local server
    python fleet_orchestrator.py --config fleet_config.json --host localhost --port 3012

    # 500 nodes against test server
    python fleet_orchestrator.py --config fleet_config.json --host testapi.retina.fm --port 3012 --nodes 500

    # With validation (stores ground truth for comparison)
    python fleet_orchestrator.py --config fleet_config.json --validate --validation-url http://localhost:8000
"""

import argparse
import asyncio
import json
import logging
import math
import os
import random
import signal
import ssl
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# Add parent dir so we can import simulation packages
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from simulation.world import SimulationWorld, NodeConfig
from simulation.generator import generate_fleet, fleet_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fleet")

RETINA_VERSION = "1.0"
HEARTBEAT_INTERVAL_S = 60
CONFIG_ACK_TIMEOUT_S = 10


def _config_hash(cfg: dict) -> str:
    import hashlib
    cfg_str = json.dumps(cfg, sort_keys=True)
    return hashlib.sha256(cfg_str.encode()).hexdigest()[:16]


class NodeConnection:
    """Manages a single async TCP connection for one synthetic node."""

    def __init__(self, node_cfg: dict, host: str, port: int):
        self.cfg = node_cfg
        self.node_id = node_cfg["node_id"]
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self.handshake_ok = False
        self.frames_sent = 0
        self.last_heartbeat = 0.0
        self._cfg_hash = _config_hash(node_cfg)

    async def connect(self, max_retries: int = 3) -> bool:
        """Establish TCP connection with retry."""
        for attempt in range(max_retries):
            try:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=10.0,
                )
                self.connected = True
                return True
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
                wait = min(2 ** (attempt + 1), 15)
                log.debug("%s: connect failed (%s), retry in %ds", self.node_id, e, wait)
                await asyncio.sleep(wait)
        return False

    async def _send(self, msg: dict):
        """Send a newline-delimited JSON message."""
        if not self.writer:
            return
        data = json.dumps(msg).encode("utf-8") + b"\n"
        self.writer.write(data)
        await self.writer.drain()

    async def _recv(self, timeout: float = CONFIG_ACK_TIMEOUT_S) -> Optional[dict]:
        """Receive a single newline-delimited JSON message."""
        if not self.reader:
            return None
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            if not line:
                return None
            return json.loads(line.strip())
        except (asyncio.TimeoutError, json.JSONDecodeError):
            return None

    async def handshake(self) -> bool:
        """Perform RETINA protocol handshake."""
        # HELLO
        await self._send({
            "type": "HELLO",
            "node_id": self.node_id,
            "version": RETINA_VERSION,
            "is_synthetic": True,
            "capabilities": {
                "detection": True,
                "adsb_correlation": True,
                "doppler": True,
                "config_hash": True,
                "heartbeat": True,
            },
        })

        # CONFIG
        await self._send({
            "type": "CONFIG",
            "node_id": self.node_id,
            "config_hash": self._cfg_hash,
            "config": self.cfg,
        })

        # Wait for CONFIG_ACK
        for _ in range(3):
            ack = await self._recv(timeout=CONFIG_ACK_TIMEOUT_S)
            if ack and ack.get("type") == "CONFIG_ACK":
                if ack.get("config_hash") == self._cfg_hash:
                    self.handshake_ok = True
                    return True
            # Re-send CONFIG on timeout
            await self._send({
                "type": "CONFIG",
                "node_id": self.node_id,
                "config_hash": self._cfg_hash,
                "config": self.cfg,
            })
        return False

    async def send_detection(self, frame: dict):
        """Send a detection frame."""
        await self._send({
            "type": "DETECTION",
            "node_id": self.node_id,
            "data": frame,
        })
        self.frames_sent += 1

    async def send_heartbeat(self):
        """Send a heartbeat if interval has elapsed."""
        now = time.monotonic()
        if now - self.last_heartbeat < HEARTBEAT_INTERVAL_S:
            return
        await self._send({
            "type": "HEARTBEAT",
            "node_id": self.node_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config_hash": self._cfg_hash,
            "status": "active",
        })
        self.last_heartbeat = now

    async def close(self):
        """Close the connection."""
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.connected = False


class FleetOrchestrator:
    """Manages a fleet of 100-1000 synthetic nodes sending to a RETINA server."""

    def __init__(
        self,
        node_configs: list[dict],
        host: str = "localhost",
        port: int = 3012,
        mode: str = "adsb",
        frame_interval: float = 0.5,
        max_concurrent_connects: int = 50,
        connect_retries: int = 3,
        time_scale: float = 1.0,
        min_aircraft: int = 0,
        max_aircraft: int = 0,
        beam_width_deg: float = 0.0,
        max_range_km: float = 0.0,
    ):
        self.node_configs = node_configs
        self.host = host
        self.port = port
        self.mode = mode
        self.frame_interval = frame_interval
        self.max_concurrent_connects = max_concurrent_connects
        self.connect_retries = max(1, connect_retries)
        self.time_scale = max(0.1, time_scale)
        self.min_aircraft = max(0, min_aircraft)
        self.max_aircraft = max(0, max_aircraft)
        self.beam_width_deg = max(0.0, beam_width_deg)
        self.max_range_km = max(0.0, max_range_km)
        self.connections: dict[str, NodeConnection] = {}
        self.world: Optional[SimulationWorld] = None
        self._running = False
        self._stats = {
            "total_frames": 0,
            "total_detections": 0,
            "connected_nodes": 0,
            "handshake_ok": 0,
            "start_time": 0,
            "errors": 0,
        }
        # Ground truth storage for validation
        self.ground_truth: list[dict] = []

    def _build_world(self):
        """Build the SimulationWorld from node configurations."""
        # Determine center from all node positions
        lats = [c["rx_lat"] for c in self.node_configs]
        lons = [c["rx_lon"] for c in self.node_configs]
        center_lat = sum(lats) / len(lats)
        center_lon = sum(lons) / len(lons)

        self.world = SimulationWorld(center_lat=center_lat, center_lon=center_lon)
        # Scale aircraft count with node count
        n = len(self.node_configs)
        auto_min_aircraft = max(10, n // 5)
        auto_max_aircraft = max(18, n // 3)
        self.world.min_aircraft = self.min_aircraft or auto_min_aircraft
        self.world.max_aircraft = self.max_aircraft or auto_max_aircraft
        if self.world.max_aircraft < self.world.min_aircraft:
            self.world.max_aircraft = self.world.min_aircraft

        for cfg in self.node_configs:
            node = NodeConfig(
                node_id=cfg["node_id"],
                rx_lat=cfg["rx_lat"],
                rx_lon=cfg["rx_lon"],
                rx_alt_ft=cfg["rx_alt_ft"],
                tx_lat=cfg["tx_lat"],
                tx_lon=cfg["tx_lon"],
                tx_alt_ft=cfg["tx_alt_ft"],
                fc_hz=cfg["fc_hz"],
                fs_hz=cfg.get("fs_hz", 2_000_000),
                beam_width_deg=self.beam_width_deg or cfg.get("beam_width_deg", 45),
                max_range_km=self.max_range_km or cfg.get("max_range_km", 50),
            )
            self.world.add_node(node)

        log.info(
            "SimulationWorld: center=(%.2f, %.2f), %d nodes, %d-%d aircraft",
            center_lat, center_lon, len(self.node_configs),
            self.world.min_aircraft, self.world.max_aircraft,
        )

    async def _connect_batch(self, configs: list[dict]) -> list[dict]:
        """Connect a batch of nodes concurrently and return failed configs."""
        sem = asyncio.Semaphore(self.max_concurrent_connects)
        failed: list[dict] = []

        async def _connect_one(cfg):
            async with sem:
                if cfg["node_id"] in self.connections:
                    return
                conn = NodeConnection(cfg, self.host, self.port)
                if await conn.connect():
                    if await conn.handshake():
                        self.connections[cfg["node_id"]] = conn
                        return
                    else:
                        await conn.close()
                        log.warning("%s: handshake failed", cfg["node_id"])
                failed.append(cfg)

        await asyncio.gather(*[_connect_one(c) for c in configs])
        return failed

    async def connect_all(self):
        """Connect all nodes in batches and retry failed handshakes."""
        log.info("Connecting %d nodes to %s:%d ...", len(self.node_configs), self.host, self.port)

        batch_size = max(1, min(self.max_concurrent_connects, len(self.node_configs)))
        pending = list(self.node_configs)

        for attempt in range(1, self.connect_retries + 1):
            if not pending:
                break

            log.info(
                "Connection round %d/%d: %d nodes pending",
                attempt,
                self.connect_retries,
                len(pending),
            )

            next_pending: list[dict] = []
            round_start_connected = len(self.connections)

            for i in range(0, len(pending), batch_size):
                batch = pending[i:i + batch_size]
                failed = await self._connect_batch(batch)
                next_pending.extend(failed)
                round_connected = len(self.connections) - round_start_connected
                log.info(
                    "  batch %d-%d: %d/%d connected this round (total: %d/%d)",
                    i + 1,
                    i + len(batch),
                    len(batch) - len(failed),
                    len(batch),
                    len(self.connections),
                    len(self.node_configs),
                )
                await asyncio.sleep(1.0)

            pending = next_pending
            if pending and attempt < self.connect_retries:
                log.info("Retrying %d failed nodes after cooldown", len(pending))
                await asyncio.sleep(min(2 * attempt, 5))

        self._stats["connected_nodes"] = len(self.connections)
        self._stats["handshake_ok"] = len(self.connections)
        log.info("Fleet connected: %d/%d nodes", len(self.connections), len(self.node_configs))
        if pending:
            log.warning("%d nodes still failed after retries", len(pending))

    def _record_ground_truth(self, timestamp_ms: int):
        """Snapshot the simulation ground truth for validation."""
        if self.world is None:
            return
        truth = {
            "timestamp_ms": timestamp_ms,
            "aircraft": self.world.get_aircraft_summary(),
        }
        self.ground_truth.append(truth)
        # Keep rolling window to bound memory
        if len(self.ground_truth) > 1000:
            self.ground_truth = self.ground_truth[-500:]

    async def _send_frame_to_node(self, node_id: str, frame: dict):
        """Send a detection frame to a single node connection."""
        conn = self.connections.get(node_id)
        if not conn or not conn.connected:
            return
        try:
            await conn.send_detection(frame)
            await conn.send_heartbeat()
            n_det = len(frame.get("delay", []))
            self._stats["total_frames"] += 1
            self._stats["total_detections"] += n_det
        except (ConnectionResetError, BrokenPipeError, OSError):
            conn.connected = False
            self._stats["errors"] += 1

    async def run_simulation_loop(self, duration_s: float = 0):
        """Main simulation loop — step world continuously, send frames staggered.

        The world now advances every ``tick_dt=1s`` (multiplied by time_scale)
        regardless of ``frame_interval``.  Node frames are staggered evenly
        across ``frame_interval`` so the total frame rate stays the same
        (n_nodes / frame_interval per second) but aircraft positions update
        every real second instead of every ``frame_interval`` seconds.
        This prevents the "stationary then teleport" issue when frame_interval
        is large (e.g. 40 s).
        """
        self._running = True
        self._stats["start_time"] = time.monotonic()
        tick_dt = 1.0  # world advances this many real-seconds per loop iteration
        report_interval = 10  # log stats every N seconds
        last_report = time.monotonic()

        # Stagger per-node send times evenly across frame_interval so we never
        # send to all nodes at once.
        now_init = time.monotonic()
        node_ids = list(self.connections.keys())
        n_nodes = max(len(node_ids), 1)
        next_send: dict[str, float] = {}
        for i, nid in enumerate(node_ids):
            next_send[nid] = now_init + i * (self.frame_interval / n_nodes)

        log.info(
            "Starting simulation loop (tick=%.1fs, frame_interval=%.1fs, "
            "time_scale=%.1fx, mode=%s, duration=%s, ~%.1f frames/s)",
            tick_dt, self.frame_interval, self.time_scale, self.mode,
            f"{duration_s}s" if duration_s else "infinite",
            n_nodes / self.frame_interval,
        )

        try:
            while self._running:
                loop_start = time.monotonic()

                # Advance the simulation world by one tick
                self.world.step(tick_dt * self.time_scale, mode=self.mode)
                # Use simulation time (not wall clock) so the LM solver sees
                # timestamps that are consistent with the delay/Doppler physics.
                # With time_scale>1, wall-clock dt is compressed but position
                # changes are proportionally larger, causing the solver to
                # estimate velocities that exceed its bounds and return None.
                timestamp_ms = int(self.world._time * 1000)

                # Record ground truth on every world step
                self._record_ground_truth(timestamp_ms)

                # Send frames only to nodes whose per-node cooldown has elapsed
                now_t = time.monotonic()
                send_tasks = []
                for node_id, conn in self.connections.items():
                    if not conn.connected:
                        continue
                    if now_t < next_send.get(node_id, 0.0):
                        continue
                    frame = self.world.generate_detections_for_node(node_id, timestamp_ms)
                    if frame.get("delay"):  # only send non-empty frames
                        send_tasks.append(self._send_frame_to_node(node_id, frame))
                    # Schedule next send regardless of whether frame was non-empty
                    next_send[node_id] = now_t + self.frame_interval

                if send_tasks:
                    await asyncio.gather(*send_tasks)

                # Periodic stats report
                now_t = time.monotonic()
                if now_t - last_report >= report_interval:
                    elapsed = now_t - self._stats["start_time"]
                    active = sum(1 for c in self.connections.values() if c.connected)
                    fps = self._stats["total_frames"] / max(elapsed, 1)
                    dps = self._stats["total_detections"] / max(elapsed, 1)
                    log.info(
                        "STATS: %.0fs elapsed | %d active nodes | %d frames (%.0f/s) | "
                        "%d detections (%.0f/s) | %d errors | %d aircraft",
                        elapsed, active, self._stats["total_frames"], fps,
                        self._stats["total_detections"], dps,
                        self._stats["errors"],
                        len(self.world.aircraft) if self.world else 0,
                    )
                    last_report = now_t

                # Check duration limit
                if duration_s > 0 and (now_t - self._stats["start_time"]) >= duration_s:
                    log.info("Duration limit reached (%.0fs), stopping", duration_s)
                    break

                # Pace the loop to tick_dt
                elapsed_loop = time.monotonic() - loop_start
                sleep_time = max(0, tick_dt - elapsed_loop)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            log.info("Simulation loop cancelled")
        finally:
            self._running = False

    async def stop(self):
        """Gracefully disconnect all nodes."""
        self._running = False
        log.info("Disconnecting %d nodes...", len(self.connections))
        close_tasks = [conn.close() for conn in self.connections.values()]
        await asyncio.gather(*close_tasks, return_exceptions=True)
        log.info("All nodes disconnected")

    def get_stats(self) -> dict:
        """Return current fleet statistics."""
        elapsed = time.monotonic() - self._stats.get("start_time", time.monotonic())
        active = sum(1 for c in self.connections.values() if c.connected)
        return {
            **self._stats,
            "elapsed_s": round(elapsed, 1),
            "active_nodes": active,
            "frames_per_sec": round(self._stats["total_frames"] / max(elapsed, 1), 1),
            "detections_per_sec": round(self._stats["total_detections"] / max(elapsed, 1), 1),
            "aircraft_count": len(self.world.aircraft) if self.world else 0,
            "ground_truth_snapshots": len(self.ground_truth),
        }

    def save_ground_truth(self, path: str):
        """Save ground truth data for offline validation."""
        with open(path, "w") as f:
            json.dump({
                "fleet_stats": self.get_stats(),
                "ground_truth": self.ground_truth[-200:],  # last 200 snapshots
            }, f, indent=2)
        log.info("Ground truth saved: %s (%d snapshots)", path, len(self.ground_truth))


async def _push_ground_truth_live(
    orchestrator: FleetOrchestrator,
    base_url: str,
    interval_s: float = 2.0,
):
    """Push live ground truth positions to the server every interval_s seconds.

    The server stores these in _ground_truth_trails so the frontend map can
    render side-by-side solved track (yellow) vs ground truth (cyan dashed).
    """
    import urllib.request

    log.info("Ground truth live push started (url=%s, interval=%.1fs)", base_url, interval_s)
    url = f"{base_url}/api/test/ground-truth/push"
    loop = asyncio.get_event_loop()
    ssl_context = None
    if "localhost" in base_url or "127.0.0.1" in base_url:
        ssl_context = ssl._create_unverified_context()

    while orchestrator._running:
        await asyncio.sleep(interval_s)

        if orchestrator.world is None:
            continue

        try:
            aircraft = orchestrator.world.get_aircraft_summary()
            # Remap field names to what the server endpoint expects
            payload_aircraft = []
            for ac in aircraft:
                hex_code = ac.get("adsb_hex") or ac.get("id", "")
                if not hex_code:
                    continue
                payload_aircraft.append({
                    "hex": hex_code,
                    "lat": ac["lat"],
                    "lon": ac["lon"],
                    "alt_m": ac["alt_km"] * 1000,
                    "heading": ac.get("heading", 0),
                    "speed_ms": ac.get("speed_ms", 0),
                    "object_type": ac.get("object_type", "aircraft"),
                    "is_anomalous": ac.get("is_anomalous", False),
                })

            if payload_aircraft:
                body = json.dumps({
                    "ts_ms": int(time.time() * 1000),
                    "aircraft": payload_aircraft,
                }).encode()
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                await loop.run_in_executor(
                    None,
                    lambda r=req, ctx=ssl_context: urllib.request.urlopen(r, timeout=4, context=ctx),
                )
        except Exception as e:
            log.debug("Ground truth push failed: %s", e)


async def _push_adsb_live(
    orchestrator: FleetOrchestrator,
    base_url: str,
    interval_s: float = 1.0,
):
    """Push every aircraft's current ADS-B position to the server every interval_s.

    This runs independently of the frame pipeline so each aircraft gets a
    fresh position every second regardless of how many nodes observe it.
    The server endpoint updates state.adsb_aircraft directly and marks it
    dirty so the next 1-Hz WS broadcast includes the latest positions.
    """
    import urllib.request

    log.info("ADS-B live push started (url=%s, interval=%.1fs)", base_url, interval_s)
    url = f"{base_url}/api/sim/adsb/push"
    loop = asyncio.get_event_loop()
    ssl_context = None
    if "localhost" in base_url or "127.0.0.1" in base_url:
        import ssl as _ssl
        ssl_context = _ssl._create_unverified_context()

    while orchestrator._running:
        await asyncio.sleep(interval_s)

        if orchestrator.world is None:
            continue

        try:
            aircraft_raw = orchestrator.world.get_aircraft_summary()
            payload_aircraft = []
            for ac in aircraft_raw:
                if not ac.get("has_adsb"):
                    continue
                hex_code = ac.get("adsb_hex") or ""
                if not hex_code:
                    continue
                speed_ms = ac.get("speed_ms", 0)
                payload_aircraft.append({
                    "hex": hex_code,
                    "flight": "",
                    "lat": round(ac["lat"], 5),
                    "lon": round(ac["lon"], 5),
                    "alt_baro": round(ac["alt_km"] * 1000 / 0.3048),
                    "gs": round(speed_ms * 1.94384, 1),
                    "track": round(ac.get("heading", 0), 1),
                })

            if not payload_aircraft:
                continue

            body = json.dumps({
                "ts_ms": int(time.time() * 1000),
                "aircraft": payload_aircraft,
            }).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            await loop.run_in_executor(
                None,
                lambda r=req, ctx=ssl_context: urllib.request.urlopen(r, timeout=3, context=ctx),
            )
        except Exception as e:
            log.debug("ADS-B push failed: %s", e)


async def _validate_against_server(
    orchestrator: FleetOrchestrator,
    base_url: str,
    interval_s: float = 30.0,
):
    """Periodically compare server output against simulation ground truth."""
    import urllib.request

    log.info("Validation loop started (interval=%.0fs, url=%s)", interval_s, base_url)
    loop = asyncio.get_event_loop()
    ssl_context = None
    if "localhost" in base_url or "127.0.0.1" in base_url:
        ssl_context = ssl._create_unverified_context()

    def _get_json(endpoint_url):
        with urllib.request.urlopen(endpoint_url, timeout=10, context=ssl_context) as r:
            return json.loads(r.read())

    while orchestrator._running:
        await asyncio.sleep(interval_s)

        if not orchestrator.ground_truth:
            continue

        try:
            server_aircraft_data = await loop.run_in_executor(
                None, _get_json, f"{base_url}/api/radar/data/aircraft.json"
            )
            server_aircraft = server_aircraft_data.get("aircraft", [])

            try:
                analytics = await loop.run_in_executor(
                    None, _get_json, f"{base_url}/api/radar/analytics"
                )
            except Exception:
                analytics = {}

            try:
                nodes_status = await loop.run_in_executor(
                    None, _get_json, f"{base_url}/api/radar/nodes"
                )
            except Exception:
                nodes_status = {}

            # Compare against latest ground truth
            truth = orchestrator.ground_truth[-1]
            truth_aircraft = truth["aircraft"]

            log.info(
                "VALIDATION: server=%d aircraft, truth=%d aircraft, "
                "server_nodes=%d connected, analytics_nodes=%d",
                len(server_aircraft),
                len(truth_aircraft),
                nodes_status.get("connected", 0),
                len(analytics.get("nodes", {})),
            )

        except Exception as e:
            log.debug("Validation check failed: %s", e)


async def main_async(args):
    """Main async entry point."""
    # Load or generate fleet config
    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            data = json.load(f)
        all_nodes = data.get("nodes", data.get("fleet", {}).get("nodes", []))
        if not all_nodes:
            # Fallback: maybe it's the old nodes_config.json format
            all_nodes = data.get("nodes", [])
    else:
        log.info("No config file, generating %d nodes...", args.nodes)
        regions = [r.strip() for r in args.regions.split(",")]
        all_nodes = generate_fleet(n_nodes=args.nodes, regions=regions, seed=args.seed)

    # Limit to requested number
    if args.nodes and args.nodes < len(all_nodes):
        all_nodes = all_nodes[:args.nodes]

    log.info("Fleet: %d nodes", len(all_nodes))
    summary = fleet_summary(all_nodes)
    log.info("Summary: %s", json.dumps(summary, indent=2))

    orchestrator = FleetOrchestrator(
        node_configs=all_nodes,
        host=args.host,
        port=args.port,
        mode=args.mode,
        frame_interval=args.interval,
        max_concurrent_connects=args.concurrency,
        connect_retries=args.connect_retries,
        time_scale=args.time_scale,
        min_aircraft=args.min_aircraft,
        max_aircraft=args.max_aircraft,
        beam_width_deg=args.beam_width_deg,
        max_range_km=args.max_range_km,
    )

    # Build shared simulation world
    orchestrator._build_world()

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(orchestrator.stop()))

    # Connect all nodes
    await orchestrator.connect_all()

    if orchestrator._stats["connected_nodes"] == 0:
        log.error("No nodes connected, exiting")
        return

    # Start validation loop if requested
    tasks = [orchestrator.run_simulation_loop(duration_s=args.duration)]

    # Always push per-aircraft ADS-B positions every second so every aircraft
    # has a fresh position in state.adsb_aircraft regardless of node visibility.
    if args.validation_url:
        tasks.append(_push_adsb_live(
            orchestrator, args.validation_url, interval_s=1.0,
        ))

    # Always push ground truth when we have a validation_url — needed for
    # anomaly detection and the frontend map overlay, not just validation.
    if args.validation_url:
        tasks.append(_push_ground_truth_live(
            orchestrator, args.validation_url, interval_s=2.0,
        ))

    if args.validate and args.validation_url:
        tasks.append(_validate_against_server(
            orchestrator, args.validation_url, interval_s=30.0,
        ))

    try:
        await asyncio.gather(*tasks)
    finally:
        # Save ground truth
        if args.ground_truth_path:
            orchestrator.save_ground_truth(args.ground_truth_path)

        await orchestrator.stop()

        # Final report
        stats = orchestrator.get_stats()
        log.info("=" * 60)
        log.info("FINAL REPORT")
        log.info("=" * 60)
        for k, v in stats.items():
            log.info("  %s: %s", k, v)


def main():
    parser = argparse.ArgumentParser(
        description="Fleet Orchestrator — run 100-1000 synthetic nodes"
    )
    parser.add_argument("--config", type=str, default="fleet_config.json",
                        help="Path to fleet_config.json")
    parser.add_argument("--nodes", type=int, default=0,
                        help="Number of nodes to use (0 = all from config)")
    parser.add_argument("--regions", type=str, default="us",
                        help="Regions for auto-generation: us,eu,au")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for fleet generation")
    parser.add_argument("--host", type=str, default="localhost",
                        help="Server hostname")
    parser.add_argument("--port", type=int, default=3012,
                        help="Server TCP port")
    parser.add_argument("--mode", type=str, default="adsb",
                        choices=["detection", "adsb", "anomalous"],
                        help="Detection mode")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Frame interval in seconds")
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="Simulation speed multiplier (for demo visibility)")
    parser.add_argument("--duration", type=float, default=0,
                        help="Run duration in seconds (0 = infinite)")
    parser.add_argument("--min-aircraft", type=int, default=0,
                        help="Minimum aircraft to keep alive (0 = auto demo default)")
    parser.add_argument("--max-aircraft", type=int, default=0,
                        help="Maximum aircraft in world (0 = auto demo default)")
    parser.add_argument("--beam-width-deg", type=float, default=0,
                        help="Override node beam width for demo visibility (0 = use config)")
    parser.add_argument("--max-range-km", type=float, default=0,
                        help="Override node max range for demo visibility (0 = use config)")
    parser.add_argument("--concurrency", type=int, default=50,
                        help="Max concurrent TCP connections during setup")
    parser.add_argument("--connect-retries", type=int, default=3,
                        help="How many retry rounds to use for failed handshakes")
    parser.add_argument("--validate", action="store_true",
                        help="Enable validation against server API")
    parser.add_argument("--validation-url", type=str, default="http://localhost:8000",
                        help="Base URL for validation API calls")
    parser.add_argument("--ground-truth-path", type=str, default="ground_truth.json",
                        help="Path to save ground truth data")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
