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
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# Add parent dir so we can import simulation_world
sys.path.insert(0, os.path.dirname(__file__))

from simulation_world import SimulationWorld, NodeConfig
from fleet_generator import generate_fleet, fleet_summary

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
    ):
        self.node_configs = node_configs
        self.host = host
        self.port = port
        self.mode = mode
        self.frame_interval = frame_interval
        self.max_concurrent_connects = max_concurrent_connects
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
        self.world.min_aircraft = max(5, n // 20)
        self.world.max_aircraft = max(15, n // 10)

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
                beam_width_deg=cfg.get("beam_width_deg", 45),
                max_range_km=cfg.get("max_range_km", 50),
            )
            self.world.add_node(node)

        log.info(
            "SimulationWorld: center=(%.2f, %.2f), %d nodes, %d-%d aircraft",
            center_lat, center_lon, len(self.node_configs),
            self.world.min_aircraft, self.world.max_aircraft,
        )

    async def _connect_batch(self, configs: list[dict]) -> int:
        """Connect a batch of nodes concurrently."""
        sem = asyncio.Semaphore(self.max_concurrent_connects)
        connected = 0

        async def _connect_one(cfg):
            nonlocal connected
            async with sem:
                conn = NodeConnection(cfg, self.host, self.port)
                if await conn.connect():
                    if await conn.handshake():
                        self.connections[cfg["node_id"]] = conn
                        connected += 1
                    else:
                        await conn.close()
                        log.warning("%s: handshake failed", cfg["node_id"])

        await asyncio.gather(*[_connect_one(c) for c in configs])
        return connected

    async def connect_all(self):
        """Connect all nodes in batches to avoid overwhelming the server."""
        log.info("Connecting %d nodes to %s:%d ...", len(self.node_configs), self.host, self.port)

        batch_size = min(50, len(self.node_configs))
        total_connected = 0

        for i in range(0, len(self.node_configs), batch_size):
            batch = self.node_configs[i:i + batch_size]
            n = await self._connect_batch(batch)
            total_connected += n
            log.info(
                "  batch %d-%d: %d/%d connected (total: %d/%d)",
                i + 1, i + len(batch), n, len(batch),
                total_connected, len(self.node_configs),
            )
            # Small delay between batches to let server process handshakes
            await asyncio.sleep(0.5)

        self._stats["connected_nodes"] = total_connected
        self._stats["handshake_ok"] = total_connected
        log.info("Fleet connected: %d/%d nodes", total_connected, len(self.node_configs))

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
        """Main simulation loop — step world, generate frames, send to nodes.

        Args:
            duration_s: How long to run (0 = forever until Ctrl-C).
        """
        self._running = True
        self._stats["start_time"] = time.monotonic()
        dt = self.frame_interval
        frame_count = 0
        report_interval = 10  # log stats every N seconds
        last_report = time.monotonic()

        log.info(
            "Starting simulation loop (dt=%.1fs, mode=%s, duration=%s)",
            dt, self.mode, f"{duration_s}s" if duration_s else "infinite",
        )

        try:
            while self._running:
                loop_start = time.monotonic()

                # Step the simulation
                self.world.step(dt, mode=self.mode)
                timestamp_ms = int(time.time() * 1000)

                # Record ground truth
                self._record_ground_truth(timestamp_ms)

                # Generate and send frames for all connected nodes
                send_tasks = []
                for node_id, conn in self.connections.items():
                    if not conn.connected:
                        continue
                    frame = self.world.generate_detections_for_node(node_id, timestamp_ms)
                    if frame.get("delay"):  # only send non-empty frames
                        send_tasks.append(self._send_frame_to_node(node_id, frame))

                if send_tasks:
                    await asyncio.gather(*send_tasks)

                frame_count += 1

                # Periodic stats report
                now = time.monotonic()
                if now - last_report >= report_interval:
                    elapsed = now - self._stats["start_time"]
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
                    last_report = now

                # Check duration limit
                if duration_s > 0 and (now - self._stats["start_time"]) >= duration_s:
                    log.info("Duration limit reached (%.0fs), stopping", duration_s)
                    break

                # Pace the loop
                elapsed_loop = time.monotonic() - loop_start
                sleep_time = max(0, dt - elapsed_loop)
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


async def _validate_against_server(
    orchestrator: FleetOrchestrator,
    base_url: str,
    interval_s: float = 30.0,
):
    """Periodically compare server output against simulation ground truth."""
    import httpx

    log.info("Validation loop started (interval=%.0fs, url=%s)", interval_s, base_url)

    while orchestrator._running:
        await asyncio.sleep(interval_s)

        if not orchestrator.ground_truth:
            continue

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Fetch server's aircraft state
                resp = await client.get(f"{base_url}/api/radar/data/aircraft.json")
                if resp.status_code != 200:
                    log.warning("Validation: server returned %d", resp.status_code)
                    continue
                server_aircraft = resp.json().get("aircraft", [])

                # Fetch server analytics
                resp2 = await client.get(f"{base_url}/api/radar/analytics")
                analytics = resp2.json() if resp2.status_code == 200 else {}

                # Fetch node status
                resp3 = await client.get(f"{base_url}/api/radar/nodes")
                nodes_status = resp3.json() if resp3.status_code == 200 else {}

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
    parser.add_argument("--duration", type=float, default=0,
                        help="Run duration in seconds (0 = infinite)")
    parser.add_argument("--concurrency", type=int, default=50,
                        help="Max concurrent TCP connections during setup")
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
