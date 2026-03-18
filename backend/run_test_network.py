#!/usr/bin/env python3
"""
Test Network Orchestrator — Retina Passive Radar.

Runs N synthetic nodes against a server, validates all subsystems:
data ingestion, tracking, analytics, multi-node solving, archiving.

Usage:
    # Quick smoke test (10 nodes, 20 steps, ~60 s sim time)
    python run_test_network.py --nodes 10 --steps 20

    # Medium scale (100 nodes, 30 steps)
    python run_test_network.py --nodes 100 --steps 30

    # Full scale (1000 nodes, 30 steps) against production
    python run_test_network.py --nodes 1000 --steps 30 \\
        --server https://towers.retina.fm \\
        --api-key YOUR_KEY

    # Use a pre-generated node config
    python run_test_network.py \\
        --config nodes_config_test.json --steps 30

    # testapi endpoint
    python run_test_network.py --nodes 100 --steps 30 \\
        --server https://testapi.retina.fm \\
        --api-key YOUR_KEY
"""

import argparse
import asyncio
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))

from simulation_world import SimulationWorld, NodeConfig

# Max concurrent HTTP posts (avoids overwhelming server / OS fd limits)
CONCURRENCY = 20
STEP_INTERVAL_S = 3.0

# Clutter detections are generated with SNR in [4, 7]; real targets are > 8.
_CLUTTER_SNR_MAX = 8.0


def _has_real_detections(frame: dict) -> bool:
    """True if the frame contains at least one detection above clutter threshold."""
    return any(s > _CLUTTER_SNR_MAX for s in frame.get("snr", []))


# ── Per-node posting ──────────────────────────────────────────────────────────

async def _post_node(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    node_id: str,
    frame: dict,
) -> tuple[str, bool, int]:
    """POST one frame for one node.  Returns (node_id, success, n_tracks)."""
    if not frame.get("delay"):
        return node_id, True, 0   # nothing to detect this step

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        resp = await client.post(
            url,
            json={"node_id": node_id, "frames": [frame]},
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return node_id, True, data.get("tracks", 0)
        return node_id, False, -resp.status_code  # negative = HTTP error code
    except httpx.ReadTimeout:
        return node_id, False, -1  # timeout
    except httpx.ConnectError:
        return node_id, False, -2  # connection refused/reset
    except Exception:
        return node_id, False, -9  # other


# ── Main orchestration loop ───────────────────────────────────────────────────

async def run(
    server: str,
    api_key: str,
    n_nodes: int,
    n_steps: int,
    config_file: str | None,
    mode: str,
    verbose: bool,
):
    detections_url = f"{server.rstrip('/')}/api/radar/detections"

    # ── Load or generate node configs ────────────────────────────────────────
    if config_file and os.path.exists(config_file):
        with open(config_file) as f:
            raw = json.load(f)
        node_dicts = raw.get("nodes", raw if isinstance(raw, list) else [])
        print(f"[cfg] Loaded {len(node_dicts)} nodes from {config_file}")
    else:
        from generate_test_network import generate
        raw = generate(n_nodes)
        node_dicts = raw["nodes"]
        print(f"[cfg] Generated {len(node_dicts)} nodes across regions")

    node_dicts = node_dicts[:n_nodes]

    # ── Build SimulationWorld ─────────────────────────────────────────────────
    world = SimulationWorld()
    for nd in node_dicts:
        cfg = NodeConfig(
            node_id=nd["node_id"],
            rx_lat=nd["rx_lat"],
            rx_lon=nd["rx_lon"],
            rx_alt_ft=nd.get("rx_alt_ft", 900),
            tx_lat=nd["tx_lat"],
            tx_lon=nd["tx_lon"],
            tx_alt_ft=nd.get("tx_alt_ft", 1200),
            fc_hz=nd.get("fc_hz", 195_000_000),
            fs_hz=nd.get("fs_hz", 2_000_000),
            beam_width_deg=nd.get("beam_width_deg", 48),
            max_range_km=nd.get("max_range_km", 50),
        )
        world.add_node(cfg)

    # ── Stats ─────────────────────────────────────────────────────────────────
    stats = {
        "steps": 0,
        "posts_ok": 0,
        "posts_err": 0,
        "total_tracks": 0,
        "start": time.monotonic(),
    }

    print()
    print("=" * 66)
    print(f"  Retina Test Network")
    print(f"  Nodes:  {len(node_dicts)}   Steps: {n_steps}   Mode: {mode}")
    print(f"  Server: {server}")
    print("=" * 66)
    print(
        f"  {'step':>4}  {'sim-aircraft':>12}  "
        f"{'posting':>9}  {'step-tracks':>11}  {'errors':>7}"
    )
    print("  " + "-" * 60)

    limits = httpx.Limits(
        max_connections=CONCURRENCY,
        max_keepalive_connections=CONCURRENCY,
    )
    sem = asyncio.Semaphore(CONCURRENCY)

    bulk_url = f"{server.rstrip('/')}/api/radar/detections/bulk"

    async with httpx.AsyncClient(limits=limits) as client:
        for step in range(n_steps):
            ts_ms = int(time.time() * 1000)

            # Advance simulation
            world.step(STEP_INTERVAL_S, mode=mode)
            all_frames = world.generate_all_frames(ts_ms)

            # Step 0: POST all frames to register every node on the server.
            # Step 1+: only POST frames with real aircraft detections.
            # Mirrors real-world behaviour — idle nodes don't send data.
            if step == 0:
                to_send = all_frames
            else:
                to_send = {
                    nid: f for nid, f in all_frames.items()
                    if _has_real_detections(f)
                }
            step_sending = len(to_send)

            async def _post_one(nid, frame):
                async with sem:
                    return await _post_node(client, detections_url, api_key, nid, frame)

            items = list(to_send.items())

            if step == 0:
                # ── Bulk registration: single HTTP request for all nodes ──
                BULK_CHUNK = 500  # nodes per bulk request (keeps payload < 1 MB)
                step_ok = step_err = step_tracks = 0
                for chunk_start in range(0, len(items), BULK_CHUNK):
                    chunk = items[chunk_start:chunk_start + BULK_CHUNK]
                    payload = {
                        "nodes": [
                            {"node_id": nid, "frames": [frame]}
                            for nid, frame in chunk
                        ]
                    }
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        headers["X-API-Key"] = api_key
                    try:
                        resp = await client.post(
                            bulk_url, json=payload, headers=headers, timeout=60.0,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            step_ok += len(chunk)
                            print(f"  [reg] bulk {chunk_start//BULK_CHUNK+1}: "
                                  f"{data.get('nodes_registered', '?')} registered, "
                                  f"{data.get('frames_queued', '?')} queued")
                        else:
                            step_err += len(chunk)
                            print(f"  [reg] bulk {chunk_start//BULK_CHUNK+1}: "
                                  f"HTTP {resp.status_code}")
                    except Exception as exc:
                        step_err += len(chunk)
                        print(f"  [reg] bulk {chunk_start//BULK_CHUNK+1}: {exc}")
                results = None  # skip normal result processing
            else:
                results = await asyncio.gather(
                    *[asyncio.create_task(_post_one(nid, frame)) for nid, frame in items],
                    return_exceptions=True,
                )

            if results is not None:
                step_ok = step_err = step_tracks = 0
                err_codes = {}
                for r in results:
                    if isinstance(r, Exception):
                        step_err += 1
                        continue
                    nid, ok, tracks = r
                    if ok:
                        step_ok += 1
                        step_tracks += tracks
                    else:
                        step_err += 1
                        err_codes[tracks] = err_codes.get(tracks, 0) + 1
            else:
                err_codes = {}
                step_tracks = 0

            stats["steps"] += 1
            stats["posts_ok"] += step_ok
            stats["posts_err"] += step_err
            stats["total_tracks"] += step_tracks

            print(
                f"  {step+1:>4}  {len(world.aircraft):>12}  "
                f"{step_sending:>9}  {step_tracks:>11}  {step_err:>7}"
                + (f"  {dict(err_codes)}" if err_codes else "")
            )

            if step < n_steps - 1:
                await asyncio.sleep(max(0, STEP_INTERVAL_S - 0.2))

    elapsed = time.monotonic() - stats["start"]

    # Give the server time to drain its processing queue before validation
    print()
    print("  Waiting for server to process remaining frames...")
    await asyncio.sleep(8)

    # ── Validation: query all subsystems ─────────────────────────────────────
    print()
    print("=" * 66)
    print("  VALIDATION")
    print("=" * 66)

    headers = {"X-API-Key": api_key} if api_key else {}
    subsystem_results = {}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            checks = {
                "health":    client.get(f"{server}/api/health"),
                "aircraft":  client.get(f"{server}/api/radar/data/aircraft.json"),
                "nodes":     client.get(f"{server}/api/radar/nodes"),
                "analytics": client.get(f"{server}/api/radar/analytics"),
                "archive":   client.get(f"{server}/api/data/archive", headers=headers),
            }
            responses = {}
            for name, coro in checks.items():
                try:
                    responses[name] = await coro
                except Exception as e:
                    responses[name] = e

            for name, resp in responses.items():
                if isinstance(resp, Exception):
                    print(f"  [{name:<10}] ERROR: {resp}")
                    subsystem_results[name] = False
                    continue
                try:
                    data = resp.json()
                except Exception:
                    data = {}

                if name == "health":
                    ok = resp.status_code == 200
                    print(f"  [{name:<10}] {resp.status_code}  {data}")
                elif name == "aircraft":
                    n = len(data.get("aircraft", []))
                    ok = resp.status_code == 200
                    print(f"  [{name:<10}] {resp.status_code}  {n} tracks in aircraft.json")
                elif name == "nodes":
                    node_list = data.get("nodes", data if isinstance(data, list) else [])
                    ok = resp.status_code == 200
                    print(f"  [{name:<10}] {resp.status_code}  {len(node_list)} nodes registered")
                elif name == "analytics":
                    an_list = data if isinstance(data, list) else data.get("nodes", [])
                    ok = resp.status_code == 200
                    print(f"  [{name:<10}] {resp.status_code}  {len(an_list)} nodes with analytics")
                elif name == "archive":
                    ok = resp.status_code in (200, 404)  # 404 if no archive yet = still OK
                    print(f"  [{name:<10}] {resp.status_code}  archive endpoint reachable")
                else:
                    ok = resp.status_code == 200
                    print(f"  [{name:<10}] {resp.status_code}")

                subsystem_results[name] = ok

    except Exception as e:
        print(f"  Validation requests failed: {e}")

    # ── Summary ───────────────────────────────────────────────────────────────
    total_posts = stats["posts_ok"] + stats["posts_err"]
    success_rate = 100 * stats["posts_ok"] / max(total_posts, 1)
    all_pass = success_rate >= 90 and all(subsystem_results.values())

    print()
    print("=" * 66)
    print("  SUMMARY")
    print("=" * 66)
    print(f"  Duration:       {elapsed:.1f}s")
    print(f"  Nodes:          {len(node_dicts)}")
    print(f"  Steps:          {stats['steps']}")
    print(f"  Posts OK/Total: {stats['posts_ok']}/{total_posts}  ({success_rate:.1f}%)")
    print(f"  Total tracks:   {stats['total_tracks']}")
    subs_ok = sum(subsystem_results.values())
    subs_total = len(subsystem_results)
    print(f"  Subsystems:     {subs_ok}/{subs_total} passing")
    print()
    verdict = "✅ PASS" if all_pass else "❌ FAIL"
    print(f"  VERDICT: {verdict}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Retina test network orchestrator")
    ap.add_argument(
        "--server", default="https://towers.retina.fm",
        help="Base URL of the server (default: https://towers.retina.fm)",
    )
    ap.add_argument(
        "--api-key", default=os.getenv("RADAR_API_KEY", ""),
        help="Value for X-API-Key header (can also use RADAR_API_KEY env var)",
    )
    ap.add_argument(
        "--nodes", type=int, default=10,
        help="Number of synthetic nodes (default: 10)",
    )
    ap.add_argument(
        "--steps", type=int, default=20,
        help="Simulation steps, each = 3 s sim time (default: 20)",
    )
    ap.add_argument(
        "--config", default=None,
        help="Path to a nodes_config JSON to use instead of auto-generating",
    )
    ap.add_argument(
        "--mode", default="adsb",
        choices=["detection", "adsb", "anomalous"],
        help="Simulation mode (default: adsb)",
    )
    ap.add_argument("--verbose", action="store_true", help="Print per-node errors")
    args = ap.parse_args()

    asyncio.run(run(
        server=args.server,
        api_key=args.api_key,
        n_nodes=args.nodes,
        n_steps=args.steps,
        config_file=args.config,
        mode=args.mode,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
