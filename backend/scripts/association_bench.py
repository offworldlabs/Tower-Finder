#!/usr/bin/env python3
"""Offline benchmark for inter-node association: how many tracks are false?

Why this exists
---------------
The false-track ("ghost") rate cannot be measured on staging.  Sampling the
live feed gives a per-snapshot standard deviation of ~25 points on a metric
whose interesting differences are 10-20 points, and it drifts systematically
with fleet age -- a 40-sample window read 3% and a 70-sample window read 50%
for identical code.  Two algorithm changes were evaluated that way and both
conclusions were wrong, in opposite directions.

Nothing about the question needs a live fleet.  The simulator produces exactly
the frames the nodes send (`generate_detections_for_node` is the same call the
fleet orchestrator makes), and it knows where every aircraft actually is.  So
the whole pipeline -- world, detections, association, solve -- runs offline,
deterministically under a seed, in seconds.

A "ghost" here is a solve with no true aircraft within MATCH_KM.  That is the
same criterion used against staging, so numbers are comparable to the
measurements already taken.

Usage
-----
    python backend/scripts/association_bench.py
    python backend/scripts/association_bench.py --assoc-interval 2 10 30
    python backend/scripts/association_bench.py --seconds 300 --seed 7
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import Counter
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retina_analytics.association import InterNodeAssociator  # noqa: E402
from retina_geolocator.multinode_solver import solve_multinode  # noqa: E402
from retina_simulation.generator import generate_fleet  # noqa: E402
from retina_simulation.world import (  # noqa: E402
    NodeConfig,
    SimulationWorld,
    waypoints_for_metro,
)

# A solve is credited to an aircraft if it lands within this radius.  Matches
# resolve_ground_truth_hex's display radius so results line up with the staging
# numbers; a solve further out than this is not a fix of that aircraft.
MATCH_KM = 8.0


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Result:
    matched: int = 0
    ghosts: int = 0
    errors_km: list = None
    ghost_dist_km: list = None
    n_nodes_matched: Counter = None
    n_nodes_ghost: Counter = None
    speeds_kt: list = None
    solver_rejects: int = 0

    def __post_init__(self):
        self.errors_km = []
        self.ghost_dist_km = []
        self.n_nodes_matched = Counter()
        self.n_nodes_ghost = Counter()
        self.speeds_kt = []

    @property
    def total(self):
        return self.matched + self.ghosts

    @property
    def ghost_pct(self):
        return 100.0 * self.ghosts / self.total if self.total else 0.0


def build_scene(seed: int, n_nodes: int, n_cluster: int, metro: str):
    """Fleet + world, wired exactly as the orchestrator wires them."""
    fleet = generate_fleet(
        n_nodes=n_nodes, metro=metro, n_cluster=n_cluster, n_clusters=1,
        use_tower_api=False, seed=seed,
    )
    world = SimulationWorld(waypoints=waypoints_for_metro(metro))
    for nd in fleet:
        world.add_node(NodeConfig(
            node_id=nd["node_id"],
            rx_lat=nd["rx_lat"], rx_lon=nd["rx_lon"], rx_alt_ft=nd["rx_alt_ft"],
            tx_lat=nd["tx_lat"], tx_lon=nd["tx_lon"], tx_alt_ft=nd["tx_alt_ft"],
            fc_hz=nd["fc_hz"], fs_hz=nd["fs_hz"],
            beam_azimuth_deg=nd.get("beam_azimuth_deg"),
            beam_width_deg=nd["beam_width_deg"],
            max_range_km=nd["max_range_km"],
            max_bistatic_range_km=nd.get("max_bistatic_range_km"),
        ))
    world.center_lat = sum(n.rx_lat for n in world.nodes.values()) / len(world.nodes)
    world.center_lon = sum(n.rx_lon for n in world.nodes.values()) / len(world.nodes)
    # Match the deployed fleet's traffic density (FLEET_AIRCRAFT=10-20).  Ghost
    # rate depends on how many aircraft share an overlap zone, so this is not a
    # cosmetic setting — it is the independent variable the effect scales with.
    world.min_aircraft = 10
    world.max_aircraft = 20
    return fleet, world


def run(seed, seconds, dt, frame_interval, assoc_interval,
        n_nodes, n_cluster, metro) -> Result:
    import random

    random.seed(seed)
    fleet, world = build_scene(seed, n_nodes, n_cluster, metro)
    node_cfgs = {nd["node_id"]: nd for nd in fleet}

    assoc = InterNodeAssociator(grid_step_km=3.0)
    for nd in fleet:
        assoc.register_node(nd["node_id"], nd)
    # The library rate-limits on time.monotonic(), i.e. wall-clock.  A replay
    # compresses minutes of simulated time into a second of real time, so the
    # limiter would gate out nearly every round and the sweep would measure
    # nothing.  Disable it and drive the cadence from simulated time below,
    # which is what the parameter means on a live fleet anyway.
    assoc._ASSOC_MIN_INTERVAL_S = 0.0
    last_assoc: dict[str, float] = {}

    res = Result()

    # Reproduce the orchestrator's staggered sending (orchestrator.py:508-531).
    # Nodes are geo-sorted so same-metro neighbours land in adjacent slots, then
    # spread evenly across frame_interval.  This matters more than it looks:
    # each node's frame therefore describes the world at a *different* instant,
    # up to frame_interval apart, so one aircraft's delay at node A and node B
    # correspond to positions hundreds of metres apart.  Emitting every node
    # from a single world snapshot removes exactly the timing skew that lets a
    # cross-aircraft pairing look self-consistent — i.e. it engineers the bug
    # out of the scenario.
    def _geo_key(nid):
        n = world.nodes[nid]
        return (round(n.rx_lat, 0), round(n.rx_lon, 0), n.rx_lat, n.rx_lon)

    node_ids = sorted(node_cfgs, key=_geo_key)
    n = max(len(node_ids), 1)
    next_send = {nid: i * (frame_interval / n) for i, nid in enumerate(node_ids)}

    t = 0.0
    while t < seconds:
        world.step(dt, mode="adsb")
        t += dt
        ts_ms = int(t * 1000)
        due_nodes = [nid for nid in node_ids if next_send[nid] <= t]
        if not due_nodes:
            continue
        truth = [(ac.lat, ac.lon) for ac in world.aircraft]
        for nid in due_nodes:
            next_send[nid] += frame_interval
            frame = world.generate_detections_for_node(nid, ts_ms)
            # Keep every node's pending frame current — a neighbour's latest
            # frame is what association pairs against — but only let a node
            # *trigger* a round on its own cadence.
            if (t - last_assoc.get(nid, -1e9)) < assoc_interval:
                assoc._pending_frames[nid] = frame
                continue
            last_assoc[nid] = t
            cands = assoc.submit_frame(nid, frame, ts_ms)
            if not cands:
                continue
            for s_in in assoc.format_candidates_for_solver(cands):
                if s_in.get("n_nodes", 0) < 2:
                    continue
                try:
                    out = solve_multinode(s_in, node_cfgs)
                except Exception:
                    res.solver_rejects += 1
                    continue
                if not out or not out.get("success"):
                    res.solver_rejects += 1
                    continue
                d = min((_haversine_km(out["lat"], out["lon"], a, b)
                         for a, b in truth), default=float("inf"))
                nn = out.get("n_nodes", s_in.get("n_nodes", 0))
                speed = math.hypot(out.get("vel_east", 0.0), out.get("vel_north", 0.0))
                res.speeds_kt.append(speed * 1.94384)
                if d <= MATCH_KM:
                    res.matched += 1
                    res.errors_km.append(d)
                    res.n_nodes_matched[nn] += 1
                else:
                    res.ghosts += 1
                    res.ghost_dist_km.append(d)
                    res.n_nodes_ghost[nn] += 1
    return res


def report(label: str, r: Result, truth_max_kt: float | None = None):
    print(f"\n=== {label} ===")
    print(f"  solves {r.total:>5}   matched {r.matched:>5}   ghosts {r.ghosts:>5}"
          f"   -> {r.ghost_pct:>5.1f}% ghosts")
    if r.errors_km:
        e = sorted(r.errors_km)
        print(f"  matched position error: median {statistics.median(e):.2f} km"
              f"  p90 {e[int(0.9 * (len(e) - 1))]:.2f}  max {max(e):.2f}")
    if r.ghost_dist_km:
        g = sorted(r.ghost_dist_km)
        print(f"  ghost distance to nearest aircraft: median {statistics.median(g):.1f} km"
              f"  max {max(g):.1f}")
    print(f"  matched n_nodes {dict(sorted(r.n_nodes_matched.items()))}")
    print(f"  ghost   n_nodes {dict(sorted(r.n_nodes_ghost.items()))}")
    if r.speeds_kt and truth_max_kt:
        over = sum(1 for s in r.speeds_kt if s > truth_max_kt)
        print(f"  solves faster than any real aircraft ({truth_max_kt:.0f} kt): "
              f"{over} ({100 * over / len(r.speeds_kt):.0f}%)")
    print(f"  solver rejects/failures: {r.solver_rejects}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--seconds", type=float, default=180.0,
                   help="simulated seconds per run")
    p.add_argument("--dt", type=float, default=1.0, help="world step (s)")
    p.add_argument("--frame-interval", type=float, default=2.0,
                   help="node frame cadence (s) — matches FLEET_INTERVAL")
    p.add_argument("--assoc-interval", type=float, nargs="+", default=[2.0],
                   help="per-node association rate limit(s) to compare")
    p.add_argument("--nodes", type=int, default=15)
    p.add_argument("--n-cluster", type=int, default=10)
    p.add_argument("--metro", default="gvl")
    p.add_argument("--repeat", type=int, default=1,
                   help="repeat each config with different seeds and report the spread")
    args = p.parse_args()

    print(f"scene: metro={args.metro} nodes={args.nodes} ring={args.n_cluster} "
          f"{args.seconds:.0f}s @ {args.frame_interval:.0f}s frames, seed {args.seed}")

    for interval in args.assoc_interval:
        rates = []
        last = None
        for k in range(args.repeat):
            last = run(args.seed + k, args.seconds, args.dt, args.frame_interval,
                       interval, args.nodes, args.n_cluster, args.metro)
            rates.append(last.ghost_pct)
        report(f"assoc_interval={interval:g}s", last)
        if args.repeat > 1:
            print(f"  across {args.repeat} seeds: "
                  f"{', '.join(f'{x:.1f}%' for x in rates)}"
                  f"   spread {max(rates) - min(rates):.1f} pts")


if __name__ == "__main__":
    main()
