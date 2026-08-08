"""Bench-only pairwise-intersection position estimator.

An alternative to the LM least-squares solve (retina_geolocator.
multinode_solver.solve_multinode) for comparison in association_bench.py.
Never imported from services/, and never touches Doppler (see rms_doppler
note below) — a research tool, not a shipped solver.

Method: pin altitude z from the association initial_guess.  For every pair
of contributing nodes (A, B), sample A's pinned-altitude delay locus by
ground bearing from A's RX in 2 deg steps, bisecting the ground range each
time (mirrors initial_guess_single.ellipsoid_boresight_intersection's ray
construction, inlined as a plain bisection rather than scipy's
minimize_scalar).  Evaluate B's differential residual along that sampled
locus; each sign change between consecutive bearings brackets a crossing
of the two loci, refined by bisecting on the bearing parameter.  Pool every
pair's candidates, cluster them (greedy, 3 km radius), and score the
winning cluster by how many distinct nodes/pairs corroborate it, then by
residual.  At n=2 (one pair, no cross-pair corroboration possible) this
degenerates to the LM's own displacement heuristic: nearest candidate to
the initial guess.

Simplification vs. the production 3-D solve this is adapted from (see git
history, services/track_gates.py): a ray whose sub-RX point already
exceeds the measured differential is skipped rather than restarting the
bracket at its interior dip — fine for an exploratory tool, not
production-grade.
"""

from __future__ import annotations

import itertools
import math

from retina_geolocator.constants import C_KM_US as _C_KM_US
from retina_geolocator.multinode_solver import _enu_km_to_lla, _lla_to_enu_km

_BEARING_STEP_DEG = 2.0
_RAY_MAX_ITERS = 40
_RAY_TOL_KM = 0.05  # 50 m
_BEARING_REFINE_ITERS = 30
_CLUSTER_RADIUS_KM = 3.0


def _differential_at(node: dict, point: tuple) -> float:
    return (math.dist(point, node["tx_enu"]) + math.dist(point, node["rx_enu"])
            - node["baseline_km"])


def _locus_point(node: dict, bearing_deg: float, ceiling_km: float,
                  z_km: float) -> tuple | None:
    """Ground-range bisection along one bearing from node's RX, at z_km."""
    rx = node["rx_enu"]
    brg = math.radians(bearing_deg)
    sin_b, cos_b = math.sin(brg), math.cos(brg)

    def point_at(g):
        return (rx[0] + g * sin_b, rx[1] + g * cos_b, z_km)

    target = node["diff_km"]
    lo, hi = 0.0, ceiling_km
    if _differential_at(node, point_at(lo)) - target > 0:
        return None  # no clean bracket (see module docstring)
    if _differential_at(node, point_at(hi)) - target < 0:
        return None  # ceiling doesn't reach the locus here
    for _ in range(_RAY_MAX_ITERS):
        if (hi - lo) < _RAY_TOL_KM:
            break
        mid = (lo + hi) / 2.0
        if _differential_at(node, point_at(mid)) - target < 0:
            lo = mid
        else:
            hi = mid
    return point_at((lo + hi) / 2.0)


def _pair_candidates(node_a: dict, node_b: dict, z_km: float) -> list[dict]:
    """0-4 typical intersection candidates between A's and B's loci."""
    ceiling_a = node_a["diff_km"] / 2.0 + node_a["baseline_km"] + 5.0
    samples: list[tuple[float, tuple | None, float | None]] = []
    for step in range(int(360 / _BEARING_STEP_DEG)):
        bearing = step * _BEARING_STEP_DEG
        pt = _locus_point(node_a, bearing, ceiling_a, z_km)
        res = _differential_at(node_b, pt) - node_b["diff_km"] if pt else None
        samples.append((bearing, pt, res))
    samples.append((360.0,) + samples[0][1:])  # close the loop

    def res_at(bearing):
        pt = _locus_point(node_a, bearing, ceiling_a, z_km)
        if pt is None:
            return None, None
        return _differential_at(node_b, pt) - node_b["diff_km"], pt

    candidates = []
    for (b_lo, _, r_lo), (b_hi, _, r_hi) in zip(samples, samples[1:]):
        if r_lo is None or r_hi is None or r_lo * r_hi > 0:
            continue
        lo, hi, r = b_lo, b_hi, r_lo
        best_pt, best_res = None, None
        for _ in range(_BEARING_REFINE_ITERS):
            mid = (lo + hi) / 2.0
            r_mid, pt_mid = res_at(mid)
            if r_mid is None:
                break
            best_pt, best_res = pt_mid, r_mid
            if (r < 0) == (r_mid < 0):
                lo, r = mid, r_mid
            else:
                hi = mid
        if best_pt is not None:
            candidates.append({"e": best_pt[0], "n": best_pt[1],
                                "residual": abs(best_res)})
    return candidates


def _cluster_candidates(candidates: list[dict]) -> list[dict]:
    """Greedy radius clustering with an incrementally-updated centroid."""
    clusters: list[dict] = []
    for c in candidates:
        target = None
        for cl in clusters:
            d = math.hypot(c["e"] - cl["centroid"][0], c["n"] - cl["centroid"][1])
            if d <= _CLUSTER_RADIUS_KM:
                target = cl
                break
        if target is None:
            target = {"centroid": (c["e"], c["n"]), "count": 0,
                       "pairs": set(), "nodes": set(), "residuals": []}
            clusters.append(target)
        n0 = target["count"]
        ce, cn = target["centroid"]
        target["centroid"] = ((ce * n0 + c["e"]) / (n0 + 1),
                               (cn * n0 + c["n"]) / (n0 + 1))
        target["count"] = n0 + 1
        target["pairs"].add(c["pair"])
        target["nodes"].update(c["pair"])
        target["residuals"].append(c["residual"])
    return clusters


def _cluster_score(cluster: dict) -> tuple:
    mean_res = sum(cluster["residuals"]) / len(cluster["residuals"])
    return (len(cluster["nodes"]), len(cluster["pairs"]), -mean_res)


def solve_consensus(s_in: dict, node_cfgs: dict, *, refine_fn=None) -> dict | None:
    """Pairwise-intersection consensus estimate. Same shape as solve_multinode.

    rms_doppler is always 0.0 and vel_east/vel_north merely pass through
    s_in["initial_velocity"] — this estimator never touches Doppler, so
    speed/velocity metrics computed from its output are not meaningful
    unless refine_fn (an LM solve over the winning subset) is supplied.
    """
    guess = s_in.get("initial_guess") or {}
    ref_lat, ref_lon = guess.get("lat"), guess.get("lon")
    if ref_lat is None or ref_lon is None:
        return None
    z_km = float(guess.get("alt_km") or 7.0)

    by_node: dict[str, dict] = {}
    for m in s_in.get("measurements") or []:
        nid = m.get("node_id")
        if nid is None:
            continue
        if nid not in by_node or (m.get("snr", 0) or 0) > (by_node[nid].get("snr", 0) or 0):
            by_node[nid] = m

    nodes: dict[str, dict] = {}
    for nid, m in by_node.items():
        cfg = node_cfgs.get(nid)
        delay_us = m.get("delay_us")
        if cfg is None or delay_us is None or delay_us <= 0:
            continue
        rx_enu = _lla_to_enu_km(cfg.get("rx_lat", 0), cfg.get("rx_lon", 0),
                                 (cfg.get("rx_alt_ft") or 0) * 0.3048,
                                 ref_lat, ref_lon, 0.0)
        tx_enu = _lla_to_enu_km(cfg.get("tx_lat", 0), cfg.get("tx_lon", 0),
                                 (cfg.get("tx_alt_ft") or 0) * 0.3048,
                                 ref_lat, ref_lon, 0.0)
        nodes[nid] = {"rx_enu": rx_enu, "tx_enu": tx_enu,
                      "baseline_km": math.dist(rx_enu, tx_enu),
                      "diff_km": delay_us * _C_KM_US}
    if len(nodes) < 2:
        return None

    pairs_list = list(itertools.combinations(sorted(nodes), 2))
    all_candidates = []
    for a_id, b_id in pairs_list:
        for c in _pair_candidates(nodes[a_id], nodes[b_id], z_km):
            c["pair"] = (a_id, b_id)
            all_candidates.append(c)
    if not all_candidates:
        return None

    if len(pairs_list) == 1:
        # n=2: only one pair exists, so there is no second pair to
        # corroborate against — mirror the LM's own mirror-point
        # disambiguation and take the candidate nearest the initial guess,
        # which sits at the ENU origin by construction (ref == guess).
        winner = min(all_candidates, key=lambda c: math.hypot(c["e"], c["n"]))
        winner_e, winner_n = winner["e"], winner["n"]
        winner_nodes, winner_residuals = set(winner["pair"]), [winner["residual"]]
    else:
        clusters = [c for c in _cluster_candidates(all_candidates)
                    if len(c["pairs"]) >= 2]
        if not clusters:
            return None
        best = max(clusters, key=_cluster_score)
        winner_e, winner_n = best["centroid"]
        winner_nodes, winner_residuals = best["nodes"], best["residuals"]

    lat, lon, _ = _enu_km_to_lla(winner_e, winner_n, z_km, ref_lat, ref_lon, 0.0)
    iv = s_in.get("initial_velocity") or {}
    result = {
        "success": True,
        "lat": float(lat), "lon": float(lon), "alt_m": z_km * 1000.0,
        "vel_east": float(iv.get("vel_east_ms") or 0.0),
        "vel_north": float(iv.get("vel_north_ms") or 0.0),
        "vel_up": 0.0,
        "rms_delay": (sum(winner_residuals) / len(winner_residuals)) / _C_KM_US,
        "rms_doppler": 0.0,
        "n_nodes": len(winner_nodes),
        "n_measurements": len(winner_nodes),
        "contributing_node_ids": sorted(winner_nodes),
        "timestamp_ms": s_in.get("timestamp_ms", 0),
    }

    if refine_fn is not None:
        s_sub = dict(s_in)
        s_sub["measurements"] = [by_node[nid] for nid in winner_nodes]
        s_sub["n_nodes"] = len(winner_nodes)
        try:
            refined = refine_fn(s_sub, node_cfgs)
        except Exception:
            refined = None
        if refined and refined.get("success"):
            for k in ("lat", "lon", "alt_m", "vel_east", "vel_north", "vel_up",
                      "rms_delay", "rms_doppler"):
                if k in refined:
                    result[k] = refined[k]
            # contributing_node_ids/n_nodes stay the consensus identity —
            # refine_fn solved the same subset, so they should agree, but
            # this estimator is answerable for its own winner set.
    return result
