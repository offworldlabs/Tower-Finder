"""
Inter-Node Association Gate for Retina Passive Radar Network.

Determines which detections from different nodes could correspond to the
same physical target by comparing bistatic delay/Doppler measurements
against pre-calculated association gates.

Architecture:
  1. For each node pair, pre-compute a grid of bistatic delay values
     within the overlapping detection region.
  2. When detections arrive, filter candidate associations using the
     delay/Doppler gates.
  3. Submit associated detection groups to the multi-node solver.

Key concepts:
  - Bistatic Ellipse: locus of points with constant bistatic range
    (TX→target→RX path delay) for a given node.
  - Association Gate: allowable delay/Doppler difference between two
    node measurements that could correspond to the same target.
"""

import math
from dataclasses import dataclass, field
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

C_KM_US = 0.299792458   # speed of light km/μs
C_KM_S = 299792.458     # speed of light km/s
R_EARTH = 6371.0         # Earth radius km


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _lla_to_enu(lat, lon, alt_km, ref_lat, ref_lon, ref_alt_km):
    dlat = math.radians(lat - ref_lat)
    dlon = math.radians(lon - ref_lon)
    north = dlat * R_EARTH
    east = dlon * R_EARTH * math.cos(math.radians(ref_lat))
    up = alt_km - ref_alt_km
    return (east, north, up)


def _enu_to_lla(east_km, north_km, up_km, ref_lat, ref_lon, ref_alt_km):
    lat = ref_lat + math.degrees(north_km / R_EARTH)
    lon = ref_lon + math.degrees(east_km / (R_EARTH * math.cos(math.radians(ref_lat))))
    alt_km_out = ref_alt_km + up_km
    return (lat, lon, alt_km_out)


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def _haversine_km(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _bistatic_delay_at(target_enu, tx_enu, rx_enu=(0, 0, 0)):
    """Bistatic differential delay in μs."""
    d_tx = _norm([target_enu[i] - tx_enu[i] for i in range(3)])
    d_rx = _norm([rx_enu[i] - target_enu[i] for i in range(3)])
    d_bl = _norm([rx_enu[i] - tx_enu[i] for i in range(3)])
    return (d_tx + d_rx - d_bl) / C_KM_US


# ── Node Pair Configuration ─────────────────────────────────────────────────

@dataclass
class NodeGeometry:
    """Geometry of a single radar node."""
    node_id: str
    rx_lat: float
    rx_lon: float
    rx_alt_km: float
    tx_lat: float
    tx_lon: float
    tx_alt_km: float
    fc_hz: float = 195e6
    beam_azimuth_deg: float = 0.0
    beam_width_deg: float = 41.0
    max_range_km: float = 50.0


@dataclass
class OverlapZone:
    """Pre-computed overlap zone between a pair of nodes."""
    node_a_id: str
    node_b_id: str
    # Grid points in the overlap region (lat, lon, alt_km)
    grid_points: list[tuple[float, float, float]] = field(default_factory=list)
    # For each grid point: (delay_a_us, delay_b_us) expected bistatic delays
    delay_pairs: list[tuple[float, float]] = field(default_factory=list)
    # Association gate parameters
    delay_gate_us: float = 5.0     # max delay mismatch between prediction and measurement
    doppler_gate_hz: float = 30.0  # max Doppler mismatch


@dataclass
class AssociationCandidate:
    """A detection pair from two nodes that may be the same target."""
    timestamp_ms: int
    node_a_id: str
    node_b_id: str
    det_a_idx: int      # index in node A's detection array
    det_b_idx: int      # index in node B's detection array
    delay_a: float      # measured delay at node A
    delay_b: float      # measured delay at node B
    doppler_a: float
    doppler_b: float
    snr_a: float
    snr_b: float
    # Grid-point match info
    grid_delay_a: float  # predicted delay at node A for the matching grid point
    grid_delay_b: float  # predicted delay at node B for the matching grid point
    grid_lat: float
    grid_lon: float
    grid_alt_km: float


# ── Pre-computation ──────────────────────────────────────────────────────────

def _compute_node_enu(geo: NodeGeometry, ref_lat: float, ref_lon: float, ref_alt_km: float):
    """Compute RX and TX ENU positions relative to a common reference."""
    rx_enu = _lla_to_enu(geo.rx_lat, geo.rx_lon, geo.rx_alt_km, ref_lat, ref_lon, ref_alt_km)
    tx_enu = _lla_to_enu(geo.tx_lat, geo.tx_lon, geo.tx_alt_km, ref_lat, ref_lon, ref_alt_km)
    return rx_enu, tx_enu


def _point_in_beam(lat, lon, geo: NodeGeometry) -> bool:
    """Check if a point falls within the node's beam cone (2D check)."""
    dist = _haversine_km(geo.rx_lat, geo.rx_lon, lat, lon)
    if dist > geo.max_range_km:
        return False
    bearing = _bearing_deg(geo.rx_lat, geo.rx_lon, lat, lon)
    angle_diff = abs((bearing - geo.beam_azimuth_deg + 180) % 360 - 180)
    return angle_diff <= geo.beam_width_deg / 2


def compute_overlap_zone(geo_a: NodeGeometry, geo_b: NodeGeometry,
                         grid_step_km: float = 3.0,
                         altitudes_km: tuple[float, ...] = (3.0, 6.0, 9.0, 12.0),
                         delay_gate_us: float = 5.0,
                         doppler_gate_hz: float = 30.0) -> OverlapZone:
    """Pre-compute the overlap zone between two nodes.

    Creates a grid of test points within both nodes' detection cones
    and calculates the expected bistatic delay at each node for every
    grid point. These are used as association gates at runtime.
    """
    # Fast geographic pre-filter: if the two RX sites are farther apart
    # than the sum of their max ranges, NO point can lie in both beams.
    # Skip the O(n²) grid computation for this pair entirely.
    rx_sep = _haversine_km(geo_a.rx_lat, geo_a.rx_lon, geo_b.rx_lat, geo_b.rx_lon)
    if rx_sep > geo_a.max_range_km + geo_b.max_range_km:
        return OverlapZone(
            node_a_id=geo_a.node_id,
            node_b_id=geo_b.node_id,
            grid_points=[],
            delay_pairs=[],
            delay_gate_us=delay_gate_us,
            doppler_gate_hz=doppler_gate_hz,
        )

    # Common reference point: midpoint of the two RX positions
    ref_lat = (geo_a.rx_lat + geo_b.rx_lat) / 2
    ref_lon = (geo_a.rx_lon + geo_b.rx_lon) / 2
    ref_alt_km = 0.0

    rx_a_enu, tx_a_enu = _compute_node_enu(geo_a, ref_lat, ref_lon, ref_alt_km)
    rx_b_enu, tx_b_enu = _compute_node_enu(geo_b, ref_lat, ref_lon, ref_alt_km)

    # Determine bounding box for the grid
    max_range = max(geo_a.max_range_km, geo_b.max_range_km)
    n_steps = int(2 * max_range / grid_step_km) + 1

    grid_points = []
    delay_pairs = []

    for alt_km in altitudes_km:
        for i in range(n_steps):
            for j in range(n_steps):
                east = -max_range + i * grid_step_km
                north = -max_range + j * grid_step_km

                lat, lon, _ = _enu_to_lla(east, north, 0.0, ref_lat, ref_lon, ref_alt_km)

                # Must be in BOTH beams
                if not _point_in_beam(lat, lon, geo_a):
                    continue
                if not _point_in_beam(lat, lon, geo_b):
                    continue

                # Calculate bistatic delay at each node
                target_enu = (east, north, alt_km)
                delay_a = _bistatic_delay_at(target_enu, tx_a_enu, rx_a_enu)
                delay_b = _bistatic_delay_at(target_enu, tx_b_enu, rx_b_enu)

                # Only keep physically meaningful delays
                if delay_a < 0 or delay_b < 0:
                    continue

                grid_points.append((lat, lon, alt_km))
                delay_pairs.append((delay_a, delay_b))

    return OverlapZone(
        node_a_id=geo_a.node_id,
        node_b_id=geo_b.node_id,
        grid_points=grid_points,
        delay_pairs=delay_pairs,
        delay_gate_us=delay_gate_us,
        doppler_gate_hz=doppler_gate_hz,
    )


# ── Runtime association ──────────────────────────────────────────────────────

def find_associations(zone: OverlapZone,
                      frame_a: dict, frame_b: dict,
                      timestamp_ms: int) -> list[AssociationCandidate]:
    """Find detection associations between two nodes using pre-computed gates.

    For each detection in frame_a, find grid points whose predicted delay_a
    is close enough. Then check if any detection in frame_b matches the
    predicted delay_b for that same grid point.

    Args:
        zone: Pre-computed OverlapZone for this node pair.
        frame_a: Detection frame from node A {delay:[], doppler:[], snr:[]}.
        frame_b: Detection frame from node B {delay:[], doppler:[], snr:[]}.
        timestamp_ms: Current timestamp.

    Returns:
        List of AssociationCandidate objects.
    """
    delays_a = frame_a.get("delay", [])
    dopplers_a = frame_a.get("doppler", [])
    snrs_a = frame_a.get("snr", [])
    delays_b = frame_b.get("delay", [])
    dopplers_b = frame_b.get("doppler", [])
    snrs_b = frame_b.get("snr", [])

    candidates = []

    for i_a, (d_a, f_a) in enumerate(zip(delays_a, dopplers_a)):
        s_a = snrs_a[i_a] if i_a < len(snrs_a) else 0.0

        # Find grid points whose predicted delay_a is near the measured delay_a
        for g_idx, (pred_a, pred_b) in enumerate(zone.delay_pairs):
            if abs(d_a - pred_a) > zone.delay_gate_us:
                continue

            # This grid point is consistent with detection i_a at node A.
            # Now check if any detection at node B matches pred_b.
            for i_b, (d_b, f_b) in enumerate(zip(delays_b, dopplers_b)):
                if abs(d_b - pred_b) > zone.delay_gate_us:
                    continue

                # Doppler consistency: both should be similar direction
                # (not exact match since different bistatic geometry)
                if abs(f_a) > 0 and abs(f_b) > 0:
                    # Both should have same sign for most geometries
                    # Use a relaxed gate
                    if abs(f_a - f_b) > zone.doppler_gate_hz * 3:
                        continue

                s_b = snrs_b[i_b] if i_b < len(snrs_b) else 0.0
                g_lat, g_lon, g_alt = zone.grid_points[g_idx]

                candidates.append(AssociationCandidate(
                    timestamp_ms=timestamp_ms,
                    node_a_id=zone.node_a_id,
                    node_b_id=zone.node_b_id,
                    det_a_idx=i_a,
                    det_b_idx=i_b,
                    delay_a=d_a,
                    delay_b=d_b,
                    doppler_a=f_a,
                    doppler_b=f_b,
                    snr_a=s_a,
                    snr_b=s_b,
                    grid_delay_a=pred_a,
                    grid_delay_b=pred_b,
                    grid_lat=g_lat,
                    grid_lon=g_lon,
                    grid_alt_km=g_alt,
                ))

    # Deduplicate: keep best candidate per (det_a_idx, det_b_idx) pair
    best: dict[tuple[int, int], AssociationCandidate] = {}
    for c in candidates:
        key = (c.det_a_idx, c.det_b_idx)
        if key not in best:
            best[key] = c
        else:
            # Keep the one with smaller total delay residual
            old = best[key]
            old_res = abs(old.delay_a - old.grid_delay_a) + abs(old.delay_b - old.grid_delay_b)
            new_res = abs(c.delay_a - c.grid_delay_a) + abs(c.delay_b - c.grid_delay_b)
            if new_res < old_res:
                best[key] = c

    return list(best.values())


# ── InterNodeAssociator ──────────────────────────────────────────────────────

class InterNodeAssociator:
    """Manages overlap zones for all node pairs and runs association at runtime."""

    def __init__(self, delay_gate_us: float = 5.0, doppler_gate_hz: float = 30.0,
                 grid_step_km: float = 30.0):
        self.delay_gate_us = delay_gate_us
        self.doppler_gate_hz = doppler_gate_hz
        self.grid_step_km = grid_step_km
        self.node_geometries: dict[str, NodeGeometry] = {}
        self.overlap_zones: dict[tuple[str, str], OverlapZone] = {}
        self._pending_frames: dict[str, dict] = {}  # node_id → latest frame
        # Adjacency index: node_id → set of neighbor node_ids that share a real
        # overlap zone (delay_pairs is non-empty).  Built during registration so
        # submit_frame can iterate O(K) neighbors instead of O(N) all nodes.
        self._neighbors: dict[str, set[str]] = {}
        # Rate-limit per-node association to at most once per _ASSOC_MIN_INTERVAL_S.
        # Prevents O(K) × N frames/s = O(N²) CPU burn in dense deployments where
        # K ≈ N (wide beams, small area).  Aircraft travel <200 m in 2 s so bistatic
        # geometry is essentially unchanged — no association quality is lost.
        self._ASSOC_MIN_INTERVAL_S: float = 10.0
        self._last_assoc: dict[str, float] = {}  # node_id → last association wall-time
        self._register_lock = __import__('threading').Lock()

    def register_node(self, node_id: str, config: dict):
        """Register a node and pre-compute overlap zones with all existing nodes.

        Thread-safe: acquires an internal lock so concurrent registrations
        (e.g. from multiple run_in_executor calls) cannot corrupt iteration.

        Reconnecting nodes skip the expensive O(n²) overlap recomputation
        as long as their geometry (RX/TX position) hasn't changed.
        """
        rx_alt_km = config.get("rx_alt_ft", 0) * 0.3048 / 1000.0
        tx_alt_km = config.get("tx_alt_ft", 0) * 0.3048 / 1000.0

        geo = NodeGeometry(
            node_id=node_id,
            rx_lat=config.get("rx_lat", 0),
            rx_lon=config.get("rx_lon", 0),
            rx_alt_km=rx_alt_km,
            tx_lat=config.get("tx_lat", 0),
            tx_lon=config.get("tx_lon", 0),
            tx_alt_km=tx_alt_km,
            fc_hz=config.get("fc_hz", config.get("FC", 195e6)),
            beam_width_deg=config.get("beam_width_deg", 41),
            max_range_km=config.get("max_range_km", 50),
        )

        # Compute beam azimuth: perpendicular to the RX→TX baseline.
        # Yagi antennas point broadside (90° from the baseline) to maximise
        # cross-coverage of aircraft transiting the bistatic zone.
        geo.beam_azimuth_deg = (_bearing_deg(
            geo.rx_lat, geo.rx_lon, geo.tx_lat, geo.tx_lon
        ) + 90.0) % 360.0

        with self._register_lock:
            existing = self.node_geometries.get(node_id)
            if existing is not None and (
                abs(existing.rx_lat - geo.rx_lat) < 1e-6
                and abs(existing.rx_lon - geo.rx_lon) < 1e-6
                and abs(existing.tx_lat - geo.tx_lat) < 1e-6
                and abs(existing.tx_lon - geo.tx_lon) < 1e-6
                and abs(existing.max_range_km - geo.max_range_km) < 1e-4
            ):
                # Same geometry — overlap zones are still valid; skip O(n²) recompute.
                return

            # Pre-compute overlap zones with existing nodes (serialised to avoid
            # RuntimeError: dictionary changed size during iteration when multiple
            # nodes register concurrently from a thread-pool executor).
            for existing_id, existing_geo in list(self.node_geometries.items()):
                pair_key = tuple(sorted([node_id, existing_id]))
                zone = compute_overlap_zone(
                    geo if pair_key[0] == node_id else existing_geo,
                    existing_geo if pair_key[0] == node_id else geo,
                    grid_step_km=self.grid_step_km,
                    delay_gate_us=self.delay_gate_us,
                    doppler_gate_hz=self.doppler_gate_hz,
                )
                self.overlap_zones[pair_key] = zone
                # Update adjacency index for O(K) submit_frame lookup.
                if zone.delay_pairs:  # only real overlaps, not geographic misses
                    self._neighbors.setdefault(node_id, set()).add(existing_id)
                    self._neighbors.setdefault(existing_id, set()).add(node_id)

            self.node_geometries[node_id] = geo

    def submit_frame(self, node_id: str, frame: dict, timestamp_ms: int) -> list[AssociationCandidate]:
        """Submit a detection frame and find associations with other recent frames.

        Returns association candidates found with any other node's latest frame.
        Uses the _neighbors adjacency index so only O(K) actual-overlap pairs
        are checked instead of O(N) all connected nodes.  Additionally rate-limits
        the expensive inner loop to _ASSOC_MIN_INTERVAL_S so dense deployments
        (K ≈ N) don’t produce O(N²) CPU load when many nodes share the same area.
        """
        self._pending_frames[node_id] = frame

        # No detections → no possible associations from this frame
        if not frame.get("delay"):
            return []

        neighbors = self._neighbors.get(node_id)
        if not neighbors:
            return []  # no registered overlap pairs for this node yet

        # Rate-limit: only run association at most once per _ASSOC_MIN_INTERVAL_S
        now = __import__('time').monotonic()
        if now - self._last_assoc.get(node_id, 0.0) < self._ASSOC_MIN_INTERVAL_S:
            return []
        self._last_assoc[node_id] = now

        # Snapshot neighbors set to avoid RuntimeError if registration adds
        # new entries concurrently (Python set iteration is not thread-safe).
        all_candidates = []

        for other_id in list(neighbors):
            other_frame = self._pending_frames.get(other_id)
            if other_frame is None:
                continue  # neighbor hasn’t sent a frame yet

            pair_key = tuple(sorted([node_id, other_id]))
            zone = self.overlap_zones.get(pair_key)
            if zone is None or not zone.delay_pairs:
                continue

            # Ensure frame_a corresponds to zone.node_a_id
            if pair_key[0] == node_id:
                frame_a, frame_b = frame, other_frame
            else:
                frame_a, frame_b = other_frame, frame

            candidates = find_associations(zone, frame_a, frame_b, timestamp_ms)
            all_candidates.extend(candidates)

        return all_candidates

    def get_overlap_summary(self) -> list[dict]:
        """Return summary of all overlap zones."""
        summaries = []
        for (a_id, b_id), zone in list(self.overlap_zones.items()):
            summaries.append({
                "node_a": a_id,
                "node_b": b_id,
                "grid_points": len(zone.grid_points),
                "delay_gate_us": zone.delay_gate_us,
                "doppler_gate_hz": zone.doppler_gate_hz,
                "has_overlap": len(zone.grid_points) > 0,
            })
        return summaries

    def format_candidates_for_solver(self, candidates: list[AssociationCandidate]) -> list[dict]:
        """Format association candidates for the multi-node least-squares solver.

        Returns a list of measurement groups, each containing bistatic
        delay/Doppler measurements from multiple nodes for the same
        estimated target position.
        """
        if not candidates:
            return []

        # Group by approximate grid location
        groups: dict[tuple[float, float], list[AssociationCandidate]] = {}
        for c in candidates:
            key = (round(c.grid_lat, 3), round(c.grid_lon, 3))
            groups.setdefault(key, []).append(c)

        solver_inputs = []
        for (g_lat, g_lon), group in groups.items():
            measurements = []
            for c in group:
                measurements.append({
                    "node_id": c.node_a_id,
                    "delay_us": c.delay_a,
                    "doppler_hz": c.doppler_a,
                    "snr": c.snr_a,
                })
                measurements.append({
                    "node_id": c.node_b_id,
                    "delay_us": c.delay_b,
                    "doppler_hz": c.doppler_b,
                    "snr": c.snr_b,
                })

            # Deduplicate measurements by node_id (keep highest SNR)
            by_node: dict[str, dict] = {}
            for m in measurements:
                nid = m["node_id"]
                if nid not in by_node or m["snr"] > by_node[nid]["snr"]:
                    by_node[nid] = m

            solver_inputs.append({
                "initial_guess": {
                    "lat": g_lat,
                    "lon": g_lon,
                    "alt_km": group[0].grid_alt_km,
                },
                "measurements": list(by_node.values()),
                "n_nodes": len(by_node),
                "timestamp_ms": group[0].timestamp_ms,
            })

        return solver_inputs
