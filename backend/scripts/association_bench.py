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
from retina_simulation.generator import coverage_cells, generate_fleet  # noqa: E402
from retina_simulation.orchestrator import _cells_to_metrocells  # noqa: E402
from retina_simulation.world import (  # noqa: E402
    NodeConfig,
    SimulationWorld,
    waypoints_for_metro,
)

# A solve is credited to an aircraft if it lands within this radius.  Matches
# resolve_ground_truth_hex's display radius so results line up with the staging
# numbers; a solve further out than this is not a fix of that aircraft.
MATCH_KM = 8.0

# Track-identity constants, mirroring solver.py's _MN_ASSOC_* so the harness
# counts *tracks* the way production does.  This matters more than it sounds:
# a real aircraft's many solves collapse onto one transponder identity while
# phantoms mint separate ids, so the same pipeline reads ~3% ghosts by solve
# and ~68% by track.  Comparing one against the other is what made the staging
# numbers look irreconcilable with the offline ones.
GHOST_ASSOC_MAX_DIST_KM = 6.0
GHOST_ASSOC_MAX_AGE_S = 60.0


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
    matched_tracks: set = None
    ghost_tracks: set = None
    speed_err_ms: list = None

    def __post_init__(self):
        self.errors_km = []
        self.ghost_dist_km = []
        self.n_nodes_matched = Counter()
        self.n_nodes_ghost = Counter()
        self.speeds_kt = []
        self.matched_tracks = set()
        self.ghost_tracks = set()
        self.speed_err_ms = []
        self._ghost_tracks = {}

    @property
    def track_ghost_pct(self):
        n = len(self.matched_tracks) + len(self.ghost_tracks)
        return 100.0 * len(self.ghost_tracks) / n if n else 0.0

    @property
    def total(self):
        return self.matched + self.ghosts

    @property
    def ghost_pct(self):
        return 100.0 * self.ghosts / self.total if self.total else 0.0


def build_scene(seed: int, n_nodes: int, n_cluster: int, metro: str,
                min_aircraft: int, max_aircraft: int, metro_traffic_frac: float):
    """Fleet + world, wired exactly as FleetOrchestrator._build_world does.

    The hub-radial routing is not a detail.  With metro_cells set,
    metro_traffic_frac of all spawns are funnelled through the ring core --
    the one patch of sky every ring beam overlaps.  Leave it unset (the
    SimulationWorld default is an empty cell list) and traffic disperses along
    the en-route waypoint net instead, so aircraft rarely share an overlap
    zone.  Since a false pairing requires two aircraft in one overlap zone,
    that single omission removes the mechanism under study.
    """
    fleet = generate_fleet(
        n_nodes=n_nodes, metro=metro, n_cluster=n_cluster, n_clusters=1,
        use_tower_api=False, seed=seed,
    )
    cells = coverage_cells(
        n_cluster=n_cluster, n_clusters=1, metro=metro,
    )
    center_lat = sum(c["rx_lat"] for c in fleet) / len(fleet)
    center_lon = sum(c["rx_lon"] for c in fleet) / len(fleet)
    world = SimulationWorld(
        center_lat=center_lat, center_lon=center_lon,
        waypoints=waypoints_for_metro(metro),
    )
    world.metro_cells = _cells_to_metrocells(cells)
    world.frac_metro_traffic = metro_traffic_frac
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
    # Traffic density is the independent variable the ghost rate scales with:
    # a false pairing needs two aircraft inside one overlap zone.
    world.min_aircraft = min_aircraft
    world.max_aircraft = max_aircraft
    return fleet, world


def run(seed, seconds, dt, frame_interval, assoc_interval,
        n_nodes, n_cluster, metro, min_aircraft, max_aircraft,
        metro_traffic_frac) -> Result:
    import random

    random.seed(seed)
    fleet, world = build_scene(seed, n_nodes, n_cluster, metro,
                               min_aircraft, max_aircraft, metro_traffic_frac)
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
        # Carry the object id so a real target keeps one identity across
        # solves — keying on a position would mint a new track per epoch.
        truth = [(ac.lat, ac.lon, ac.object_id, ac.speed_km_s * 1000.0)
                 for ac in world.aircraft]
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
                d, best_id, best_speed = min(
                    ((_haversine_km(out["lat"], out["lon"], a, b), oid, sp)
                     for a, b, oid, sp in truth),
                    default=(float("inf"), None, 0.0))
                nn = out.get("n_nodes", s_in.get("n_nodes", 0))
                speed = math.hypot(out.get("vel_east", 0.0), out.get("vel_north", 0.0))
                res.speeds_kt.append(speed * 1.94384)
                if d <= MATCH_KM:
                    res.matched += 1
                    res.errors_km.append(d)
                    res.n_nodes_matched[nn] += 1
                    # Track identity, mirroring solver._multinode_track_key:
                    # a solve with a known transponder collapses onto that
                    # aircraft, so a real target is one track however many
                    # times it is solved.
                    res.matched_tracks.add(best_id)
                    # Velocity error is the quantity the Doppler rework
                    # targets; ghost rate is not.
                    if best_speed > 0:
                        res.speed_err_ms.append(abs(speed - best_speed))
                else:
                    res.ghosts += 1
                    res.ghost_dist_km.append(d)
                    res.n_nodes_ghost[nn] += 1
                    # A phantom has no transponder, so it falls to the
                    # proximity/dead-reckon branch: successive solves within
                    # _MN_ASSOC_MAX_DIST_KM and _MN_ASSOC_MAX_AGE_S join one
                    # track, otherwise a new id is minted.
                    hit = None
                    for gk, (glat, glon, gts) in res._ghost_tracks.items():
                        if (t - gts) <= GHOST_ASSOC_MAX_AGE_S and \
                                _haversine_km(out["lat"], out["lon"], glat, glon) \
                                <= GHOST_ASSOC_MAX_DIST_KM:
                            hit = gk
                            break
                    if hit is None:
                        hit = f"gh-{len(res._ghost_tracks)}"
                    res._ghost_tracks[hit] = (out["lat"], out["lon"], t)
                    res.ghost_tracks.add(hit)
    return res


def report(label: str, r: Result, truth_max_kt: float | None = None):
    print(f"\n=== {label} ===")
    print(f"  solves {r.total:>5}   matched {r.matched:>5}   ghosts {r.ghosts:>5}"
          f"   -> {r.ghost_pct:>5.1f}% ghosts (by solve)")
    print(f"  tracks: real {len(r.matched_tracks):>3}   false {len(r.ghost_tracks):>3}"
          f"   -> {r.track_ghost_pct:>5.1f}% ghosts (by track — comparable to staging)")
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
    if r.speed_err_ms:
        e = sorted(r.speed_err_ms)
        print(f"  matched SPEED error: median {statistics.median(e):6.1f} m/s"
              f"  p90 {e[int(0.9 * (len(e) - 1))]:6.1f}  max {max(e):6.1f}")
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
    p.add_argument("--min-aircraft", type=int, default=10,
                   help="matches FLEET_AIRCRAFT lower bound")
    p.add_argument("--max-aircraft", type=int, default=20)
    p.add_argument("--metro-traffic-frac", type=float, default=0.85,
                   help="matches FLEET_METRO_TRAFFIC_FRAC")
    p.add_argument("--repeat", type=int, default=1,
                   help="repeat each config with different seeds and report the spread")
    args = p.parse_args()

    print(f"scene: metro={args.metro} nodes={args.nodes} ring={args.n_cluster} "
          f"{args.seconds:.0f}s @ {args.frame_interval:.0f}s frames, seed {args.seed}")

    for interval in args.assoc_interval:
        rates, solve_rates, reals, fakes, speed_errs = [], [], [], [], []
        last = None
        for k in range(args.repeat):
            last = run(args.seed + k, args.seconds, args.dt, args.frame_interval,
                       interval, args.nodes, args.n_cluster, args.metro,
                       args.min_aircraft, args.max_aircraft,
                       args.metro_traffic_frac)
            # Track-level is the comparable metric — solve-level and
            # track-level differ by ~20x on the same data, so mixing them is
            # how two staging conclusions went wrong.
            rates.append(last.track_ghost_pct)
            solve_rates.append(last.ghost_pct)
            reals.append(len(last.matched_tracks))
            fakes.append(len(last.ghost_tracks))
            if last.speed_err_ms:
                speed_errs.append(statistics.median(last.speed_err_ms))
        report(f"assoc_interval={interval:g}s", last)
        if args.repeat > 1:
            mean = statistics.mean(rates)
            sd = statistics.pstdev(rates)
            print(f"  across {args.repeat} seeds (by track): "
                  f"{', '.join(f'{x:.0f}%' for x in rates)}")
            print(f"    mean {mean:.0f}%   sd {sd:.0f}   "
                  f"range {min(rates):.0f}-{max(rates):.0f}%   "
                  f"real {min(reals)}-{max(reals)}  false {min(fakes)}-{max(fakes)}")
            print(f"    by solve: {', '.join(f'{x:.1f}%' for x in solve_rates)}")
            if speed_errs:
                print(f"    median speed error per seed: "
                      f"{', '.join(f'{x:.0f}' for x in speed_errs)} m/s"
                      f"   mean {statistics.mean(speed_errs):.0f}")


if __name__ == "__main__":
    main()
