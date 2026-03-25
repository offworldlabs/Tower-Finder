"""
Shared Simulation World for Retina Passive Radar Network.

Maintains a single "world" of simulated aircraft objects, and multiple
synthetic nodes that observe them. Each node generates detections only
for aircraft within its detection cone (Yagi beamwidth + range limit).

Architecture:
  SimulationWorld  ─ holds SimulatedAircraft objects
       │
       ├── SyntheticNodeView(node1) ─ observes aircraft through node1's geometry
       ├── SyntheticNodeView(node2) ─ observes aircraft through node2's geometry
       └── ...

Usage:
    world = SimulationWorld()
    world.add_node(node_config_1)
    world.add_node(node_config_2)
    world.step(dt)  # advance simulation
    frames = world.generate_all_frames(timestamp_ms)  # {node_id: frame_dict}
"""

import hashlib
import json
import math
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

C_KM_US = 0.299792458  # speed of light km/μs
C_KM_S = 299792.458    # speed of light km/s
R_EARTH = 6371.0        # Earth radius km

# ── US flight corridor waypoints (major airports / airways) ──────────────────
_US_WAYPOINTS = [
    # East coast
    (33.6407, -84.4277),   # ATL Atlanta
    (35.2144, -80.9473),   # CLT Charlotte
    (35.8776, -78.7875),   # RDU Raleigh
    (36.0984, -79.9372),   # GSO Greensboro
    (37.5054, -77.3197),   # RIC Richmond
    (38.8512, -77.0402),   # DCA Washington
    (39.1776, -76.6683),   # BWI Baltimore
    (39.8744, -75.2424),   # PHL Philadelphia
    (40.6413, -73.7781),   # JFK New York
    (42.3656, -71.0096),   # BOS Boston
    # Southeast
    (32.1271, -81.2020),   # SAV Savannah
    (28.4312, -81.3081),   # MCO Orlando
    (25.7959, -80.2870),   # MIA Miami
    (30.4941, -81.6879),   # JAX Jacksonville
    (27.9755, -82.5332),   # TPA Tampa
    # Central
    (36.1245, -86.6782),   # BNA Nashville
    (38.1744, -85.7360),   # SDF Louisville
    (39.0489, -84.6678),   # CVG Cincinnati
    (41.4117, -81.8498),   # CLE Cleveland
    (42.2125, -83.3534),   # DTW Detroit
    (41.9742, -87.9073),   # ORD Chicago
    (44.8848, -93.2223),   # MSP Minneapolis
    (38.7487, -90.3700),   # STL St Louis
    (39.2976, -94.7139),   # MCI Kansas City
    (29.9934, -90.2580),   # MSY New Orleans
    (29.6454, -95.2789),   # IAH Houston
    (32.8968, -97.0380),   # DFW Dallas
    (35.3926, -97.6007),   # OKC Oklahoma City
    (39.8561, -104.6737),  # DEN Denver
    # West
    (33.4373, -112.0078),  # PHX Phoenix
    (36.0840, -115.1537),  # LAS Las Vegas
    (34.0522, -118.2437),  # LAX Los Angeles
    (37.6213, -122.3790),  # SFO San Francisco
    (47.4502, -122.3088),  # SEA Seattle
    (45.5898, -122.5951),  # PDX Portland
]


@dataclass
class SimulatedAircraft:
    """A simulated aircraft in the world with lat/lon/alt position."""
    object_id: str
    # Position LLA
    lat: float
    lon: float
    alt_km: float
    # Velocity (km/s) in ENU-like local frame
    vel_east: float   # km/s east
    vel_north: float  # km/s north
    vel_up: float     # km/s vertical
    # Heading (degrees from north, clockwise)
    heading_deg: float
    speed_km_s: float
    # Type and classification
    has_adsb: bool = False
    is_anomalous: bool = False
    object_type: str = "aircraft"  # "aircraft", "drone", "anomalous"
    adsb_hex: Optional[str] = None
    adsb_callsign: Optional[str] = None
    # Lifecycle
    created_at: float = 0.0
    lifetime_s: float = 600.0
    # Waypoint navigation
    waypoints: list = field(default_factory=list)
    waypoint_idx: int = 0


@dataclass
class NodeConfig:
    """Configuration for a synthetic radar node."""
    node_id: str = "synth-node-01"
    rx_lat: float = 33.939182
    rx_lon: float = -84.651910
    rx_alt_ft: float = 950.0
    tx_lat: float = 33.75667
    tx_lon: float = -84.331844
    tx_alt_ft: float = 1600.0
    fc_hz: float = 195_000_000.0
    fs_hz: float = 2_000_000.0
    doppler_min: float = -300.0
    doppler_max: float = 300.0
    min_doppler: float = 15.0
    # Detection geometry
    beam_azimuth_deg: float = 0.0   # Yagi boresight azimuth from north
    beam_width_deg: float = 41.0     # Yagi half-power beamwidth (40-42° spec)
    max_range_km: float = 50.0       # maximum detection range


def config_hash(config: NodeConfig) -> str:
    """Compute a short hash of the node configuration."""
    cfg_str = json.dumps(asdict(config), sort_keys=True)
    return hashlib.sha256(cfg_str.encode()).hexdigest()[:16]


# ── Coordinate helpers ────────────────────────────────────────────────────────

def _lla_to_enu(lat, lon, alt_km, ref_lat, ref_lon, ref_alt_km):
    """Convert LLA to ENU (km) relative to reference point."""
    dlat = math.radians(lat - ref_lat)
    dlon = math.radians(lon - ref_lon)
    north = dlat * R_EARTH
    east = dlon * R_EARTH * math.cos(math.radians(ref_lat))
    up = alt_km - ref_alt_km
    return east, north, up


def _enu_to_lla(east_km, north_km, up_km, ref_lat, ref_lon, ref_alt_km):
    """Convert ENU (km) back to LLA."""
    lat = ref_lat + math.degrees(north_km / R_EARTH)
    lon = ref_lon + math.degrees(east_km / (R_EARTH * math.cos(math.radians(ref_lat))))
    alt_km_out = ref_alt_km + up_km
    return lat, lon, alt_km_out


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_deg(lat1, lon1, lat2, lon2):
    """Bearing from point1 to point2, in degrees from north clockwise."""
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def _bistatic_delay(target_enu, tx_enu, rx_enu=(0, 0, 0)):
    """Compute bistatic differential delay in microseconds."""
    d_tx_tgt = _norm([target_enu[i] - tx_enu[i] for i in range(3)])
    d_tgt_rx = _norm([rx_enu[i] - target_enu[i] for i in range(3)])
    d_tx_rx = _norm([rx_enu[i] - tx_enu[i] for i in range(3)])
    diff_range = (d_tx_tgt + d_tgt_rx) - d_tx_rx
    return diff_range / C_KM_US


def _bistatic_doppler(target_enu, vel_enu, tx_enu, rx_enu, freq_hz):
    """Compute bistatic Doppler shift in Hz."""
    to_tx = [tx_enu[i] - target_enu[i] for i in range(3)]
    to_rx = [rx_enu[i] - target_enu[i] for i in range(3)]
    d_tx = _norm(to_tx)
    d_rx = _norm(to_rx)
    if d_tx < 1e-9 or d_rx < 1e-9:
        return 0.0
    u_tx = [to_tx[i] / d_tx for i in range(3)]
    u_rx = [to_rx[i] / d_rx for i in range(3)]
    v_rad_tx = sum(vel_enu[i] * u_tx[i] for i in range(3))
    v_rad_rx = sum(vel_enu[i] * u_rx[i] for i in range(3))
    return (freq_hz / C_KM_S) * (v_rad_tx + v_rad_rx)


# ── Flight corridor route generation ─────────────────────────────────────────

def _pick_route(center_lat: float, center_lon: float, max_dist_km: float = 300) -> list[tuple[float, float]]:
    """Pick a sequence of 2-4 waypoints near center forming a realistic route."""
    nearby = [
        wp for wp in _US_WAYPOINTS
        if _haversine_km(center_lat, center_lon, wp[0], wp[1]) < max_dist_km
    ]
    if len(nearby) < 2:
        nearby = sorted(_US_WAYPOINTS, key=lambda wp: _haversine_km(center_lat, center_lon, wp[0], wp[1]))[:6]

    n_waypoints = random.randint(2, min(4, len(nearby)))
    start = random.choice(nearby)
    route = [start]
    remaining = [wp for wp in nearby if wp != start]
    for _ in range(n_waypoints - 1):
        if not remaining:
            break
        # Pick next waypoint somewhat in the forward direction
        last = route[-1]
        remaining.sort(key=lambda wp: _haversine_km(last[0], last[1], wp[0], wp[1]))
        # Choose from closest 3, weighted toward closer ones
        candidates = remaining[:min(3, len(remaining))]
        nxt = random.choice(candidates)
        route.append(nxt)
        remaining = [wp for wp in remaining if wp != nxt]
    return route


# ── SimulationWorld ───────────────────────────────────────────────────────────

class SimulationWorld:
    """Shared simulation world with aircraft and multiple observer nodes."""

    def __init__(self, center_lat: float = 34.0, center_lon: float = -84.0):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.aircraft: list[SimulatedAircraft] = []
        self.nodes: dict[str, NodeConfig] = {}
        self._next_id = 1
        self._time = 0.0
        # Target count range
        self.min_aircraft = 5
        self.max_aircraft = 15

    def add_node(self, config: NodeConfig):
        """Register a synthetic node in the simulation."""
        self.nodes[config.node_id] = config
        # Auto-set beam azimuth perpendicular to the RX→TX baseline.
        # Yagi antennas point broadside to maximise aircraft cross-coverage.
        config.beam_azimuth_deg = (_bearing_deg(
            config.rx_lat, config.rx_lon,
            config.tx_lat, config.tx_lon,
        ) + 90.0) % 360.0

    def _spawn_aircraft(self, mode: str = "detection") -> SimulatedAircraft:
        """Spawn a new aircraft along a realistic flight corridor.

        Object types are selected probabilistically:
          - 70% commercial aircraft (with ADS-B in adsb/anomalous modes)
          - 15% dark aircraft (no ADS-B transponder)
          - 10% drones (low/slow)
          -  5% anomalous objects (erratic behavior)
        """
        oid = f"obj-{self._next_id:05d}"
        self._next_id += 1

        is_anomalous = False
        has_adsb = False
        adsb_hex = None
        adsb_callsign = None
        object_type = "aircraft"

        # Roll object type
        roll = random.random()
        if roll < 0.05:
            object_type = "anomalous"
            is_anomalous = True
        elif roll < 0.15:
            object_type = "drone"
        elif roll < 0.30:
            object_type = "aircraft"  # dark aircraft — no ADS-B
        else:
            object_type = "aircraft"  # commercial — will get ADS-B in adsb modes

        # Anchor near a random node (if any exist) so aircraft spawn within
        # detection range of actual nodes.  Fall back to the world centre when
        # no nodes are registered yet.
        if self.nodes:
            anchor = random.choice(list(self.nodes.values()))
            # Spawn broadside to the baseline so the aircraft is guaranteed to
            # be inside the anchor node's Yagi beam (which points perpendicular
            # to the RX→TX baseline).
            baseline_bearing = _bearing_deg(
                anchor.rx_lat, anchor.rx_lon, anchor.tx_lat, anchor.tx_lon
            )
            perp_rad = math.radians((baseline_bearing + 90.0) % 360.0)
            dist_km = random.uniform(5.0, anchor.max_range_km * 0.7)
            anchor_lat = anchor.rx_lat + (dist_km * math.cos(perp_rad)) / 111.32
            cos_lat = math.cos(math.radians(anchor.rx_lat))
            anchor_lon = anchor.rx_lon + (dist_km * math.sin(perp_rad)) / (111.32 * max(cos_lat, 1e-6))
        else:
            anchor_lat, anchor_lon = self.center_lat, self.center_lon

        # Start aircraft very close to the anchor point (within ~3 km)
        lat = anchor_lat + random.gauss(0, 0.03)
        lon = anchor_lon + random.gauss(0, 0.03)

        # Route: fly toward a waypoint within 50 km, staying in-region.
        # Prepend the actual spawn position so the aircraft starts here and
        # heads toward the nearest regional waypoint.
        route = [(lat, lon)] + _pick_route(anchor_lat, anchor_lon, max_dist_km=50)

        if mode == "anomalous" and random.random() < 0.2:
            is_anomalous = True
            object_type = "anomalous"
            speed_km_s = random.uniform(0.01, 0.5)  # 10-500 m/s
            alt_km = random.uniform(0.3, 15.0)
        elif object_type == "anomalous":
            speed_km_s = random.uniform(0.01, 0.5)
            alt_km = random.uniform(0.3, 15.0)
        elif object_type == "drone":
            speed_km_s = random.uniform(0.01, 0.06)  # 10-60 m/s — small UAS
            alt_km = random.uniform(0.05, 0.5)        # 50-500m AGL
        else:
            speed_km_s = random.uniform(0.12, 0.27)  # 120-270 m/s → typical jet
            alt_km = random.uniform(5.0, 12.0)       # 16k-40k ft

        # Anomalous objects also get ADS-B — anomalous means unusual flight
        # behaviour (speed/altitude/heading changes), NOT transponder absence.
        if mode in ("adsb", "anomalous") and object_type != "drone" and roll >= 0.30 or is_anomalous:
            has_adsb = True
            adsb_hex = f"{random.randint(0x100000, 0xFFFFFF):06x}"
            letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            adsb_callsign = f"{''.join(random.choices(letters, k=3))}{random.randint(100, 9999)}"

        # Initial heading toward next waypoint
        next_wp = route[1] if len(route) > 1 else route[0]
        heading = _bearing_deg(lat, lon, next_wp[0], next_wp[1])
        heading_rad = math.radians(heading)

        vel_east = speed_km_s * math.sin(heading_rad)
        vel_north = speed_km_s * math.cos(heading_rad)
        vel_up = random.uniform(-0.003, 0.003)

        return SimulatedAircraft(
            object_id=oid,
            lat=lat, lon=lon, alt_km=alt_km,
            vel_east=vel_east, vel_north=vel_north, vel_up=vel_up,
            heading_deg=heading, speed_km_s=speed_km_s,
            has_adsb=has_adsb, is_anomalous=is_anomalous,
            object_type=object_type,
            adsb_hex=adsb_hex, adsb_callsign=adsb_callsign,
            created_at=self._time,
            lifetime_s=random.uniform(180, 900) if object_type != "drone" else random.uniform(60, 300),
            waypoints=route,
            waypoint_idx=1,
        )

    def step(self, dt: float, mode: str = "detection"):
        """Advance simulation by dt seconds."""
        self._time += dt

        # Remove expired aircraft
        self.aircraft = [
            ac for ac in self.aircraft
            if (self._time - ac.created_at) < ac.lifetime_s
        ]

        # Spawn to maintain target count
        while len(self.aircraft) < self.min_aircraft:
            self.aircraft.append(self._spawn_aircraft(mode))
        if len(self.aircraft) < self.max_aircraft and random.random() < 0.01:
            self.aircraft.append(self._spawn_aircraft(mode))

        # Update each aircraft
        for ac in self.aircraft:
            self._update_aircraft(ac, dt)

    def _update_aircraft(self, ac: SimulatedAircraft, dt: float):
        """Update aircraft position and navigate toward waypoints."""
        # Move in ENU then convert back to LLA
        dlat = (ac.vel_north / R_EARTH) * (180 / math.pi) * dt
        dlon = (ac.vel_east / (R_EARTH * math.cos(math.radians(ac.lat)))) * (180 / math.pi) * dt
        ac.lat += dlat
        ac.lon += dlon
        ac.alt_km += ac.vel_up * dt

        # Clamp altitude
        ac.alt_km = max(0.1, min(ac.alt_km, 15.0))

        # Navigate toward next waypoint
        if ac.waypoint_idx < len(ac.waypoints):
            target_wp = ac.waypoints[ac.waypoint_idx]
            dist_to_wp = _haversine_km(ac.lat, ac.lon, target_wp[0], target_wp[1])

            if dist_to_wp < 5.0:
                # Arrived at waypoint, move to next
                ac.waypoint_idx += 1
            else:
                # Steer toward waypoint (gradual turn)
                desired_heading = _bearing_deg(ac.lat, ac.lon, target_wp[0], target_wp[1])
                heading_diff = (desired_heading - ac.heading_deg + 180) % 360 - 180
                # Max turn rate ~3°/s (standard rate turn)
                max_turn = 3.0 * dt
                turn = max(-max_turn, min(max_turn, heading_diff))
                ac.heading_deg = (ac.heading_deg + turn) % 360

        # Update velocity from heading and speed
        heading_rad = math.radians(ac.heading_deg)
        ac.vel_east = ac.speed_km_s * math.sin(heading_rad)
        ac.vel_north = ac.speed_km_s * math.cos(heading_rad)

        # Small random perturbations
        if not ac.is_anomalous:
            ac.vel_east += random.gauss(0, 0.0005) * dt
            ac.vel_north += random.gauss(0, 0.0005) * dt
        else:
            # Anomalous: erratic behaviour including instant stop/start
            r = random.random()
            if r < 0.005:
                # Instant stop: velocity drops to near zero
                ac.speed_km_s = random.uniform(0.0, 0.005)
                ac.vel_east = 0.0
                ac.vel_north = 0.0
                ac.vel_up = 0.0
            elif r < 0.015:
                # Instant start / speed jump: sudden acceleration
                ac.speed_km_s = random.uniform(0.15, 0.5)
                ac.heading_deg = random.uniform(0, 360)
            elif r < 0.025:
                # Sharp turn: heading change much larger than standard rate
                ac.heading_deg = (ac.heading_deg + random.uniform(30, 120)) % 360
            elif r < 0.03:
                # Random speed/heading change
                ac.speed_km_s = random.uniform(0.01, 0.5)
                ac.heading_deg = random.uniform(0, 360)

    def _aircraft_in_detection_cone(self, ac: SimulatedAircraft, node: NodeConfig) -> bool:
        """Check if aircraft is within the node's detection cone."""
        dist = _haversine_km(node.rx_lat, node.rx_lon, ac.lat, ac.lon)
        if dist > node.max_range_km:
            return False

        bearing = _bearing_deg(node.rx_lat, node.rx_lon, ac.lat, ac.lon)
        angle_diff = abs((bearing - node.beam_azimuth_deg + 180) % 360 - 180)
        if angle_diff > node.beam_width_deg / 2:
            return False

        return True

    def generate_detections_for_node(self, node_id: str, timestamp_ms: int) -> dict:
        """Generate a detection frame for a specific node.

        Returns a frame dict: {timestamp, delay[], doppler[], snr[], adsb?[]}
        Only includes aircraft within the node's detection cone.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return {"timestamp": timestamp_ms, "delay": [], "doppler": [], "snr": []}

        rx_alt_km = node.rx_alt_ft * 0.3048 / 1000.0
        tx_alt_km = node.tx_alt_ft * 0.3048 / 1000.0
        rx_enu = (0.0, 0.0, 0.0)
        tx_enu = _lla_to_enu(
            node.tx_lat, node.tx_lon, tx_alt_km,
            node.rx_lat, node.rx_lon, rx_alt_km,
        )

        delays = []
        dopplers = []
        snrs = []
        adsb_list = []
        has_any_adsb = False

        for ac in self.aircraft:
            if not self._aircraft_in_detection_cone(ac, node):
                continue

            # Convert aircraft to ENU relative to this node's RX
            target_enu = _lla_to_enu(
                ac.lat, ac.lon, ac.alt_km,
                node.rx_lat, node.rx_lon, rx_alt_km,
            )
            # Velocity already in km/s ENU
            vel_enu = (ac.vel_east, ac.vel_north, ac.vel_up)

            delay = _bistatic_delay(target_enu, tx_enu, rx_enu)
            doppler = _bistatic_doppler(target_enu, vel_enu, tx_enu, rx_enu, node.fc_hz)

            # SNR depends on distance — compute before noise/miss decisions
            dist = _norm(target_enu)
            base_snr = 25 - 10 * math.log10(max(dist, 1))
            snr = max(base_snr + random.gauss(0, 2), 4.0)

            # SNR-dependent missed detection: weaker signals are harder to detect.
            # P(miss) rises from ~5% at SNR 20+ to ~40% near detection threshold SNR 5.
            p_miss = max(0.0, min(0.5, 0.05 + 0.35 * (1 - (snr - 4) / 18)))
            if random.random() < p_miss:
                continue

            # Add measurement noise — scales inversely with SNR
            noise_scale = max(0.5, 2.0 - (snr - 4) / 18)
            delay += random.gauss(0, 0.3 * noise_scale)
            doppler += random.gauss(0, 2.0 * noise_scale)

            delays.append(round(delay, 2))
            dopplers.append(round(doppler, 2))
            snrs.append(round(snr, 2))

            # ADS-B entry
            if ac.has_adsb:
                has_any_adsb = True
                speed_ms = ac.speed_km_s * 1000
                adsb_list.append({
                    "hex": ac.adsb_hex,
                    "flight": ac.adsb_callsign,
                    "lat": round(ac.lat, 5),
                    "lon": round(ac.lon, 5),
                    "alt_baro": round(ac.alt_km * 1000 / 0.3048),
                    "gs": round(speed_ms * 1.94384, 1),
                    "track": round(ac.heading_deg, 1),
                })
            else:
                adsb_list.append(None)

        # Clutter/false alarm detections — Poisson-distributed count (mean ~2)
        # with range-biased delays (more clutter at shorter ranges).
        n_clutter = min(int(random.expovariate(0.5)), 6)
        for _ in range(n_clutter):
            # Bias toward shorter delays (near-range ground clutter)
            clutter_delay = random.expovariate(0.05)
            clutter_delay = min(clutter_delay, 60.0)
            delays.append(round(clutter_delay, 2))
            dopplers.append(round(random.uniform(node.doppler_min, node.doppler_max), 2))
            snrs.append(round(random.uniform(4, 8), 2))
            adsb_list.append(None)

        frame = {
            "timestamp": timestamp_ms,
            "delay": delays,
            "doppler": dopplers,
            "snr": snrs,
        }

        if has_any_adsb:
            frame["adsb"] = adsb_list

        return frame

    def generate_all_frames(self, timestamp_ms: int) -> dict[str, dict]:
        """Generate detection frames for all registered nodes."""
        return {
            node_id: self.generate_detections_for_node(node_id, timestamp_ms)
            for node_id in self.nodes
        }

    def get_aircraft_summary(self) -> list[dict]:
        """Return summary of all current aircraft (for debugging/monitoring)."""
        return [
            {
                "id": ac.object_id,
                "lat": round(ac.lat, 5),
                "lon": round(ac.lon, 5),
                "alt_km": round(ac.alt_km, 2),
                "heading": round(ac.heading_deg, 1),
                "speed_ms": round(ac.speed_km_s * 1000, 1),
                "has_adsb": ac.has_adsb,
                "is_anomalous": ac.is_anomalous,
                "object_type": ac.object_type,
                "adsb_hex": ac.adsb_hex,
            }
            for ac in self.aircraft
        ]

    # ── ML Training Data Batch Export ────────────────────────────────────────

    def generate_training_batch(self, n_frames: int, dt: float = 0.5,
                                mode: str = "adsb") -> list[dict]:
        """Generate a batch of labeled training frames for ML pipelines.

        Each output record contains:
          - node_id, timestamp
          - delay[], doppler[], snr[] (measurements)
          - ground_truth[]: per-detection truth with object_id, lat, lon, alt,
            heading, speed, has_adsb, is_anomalous
          - adsb[] (if mode includes ADS-B)

        This runs the simulation fast (no sleep) to produce data quickly.

        Args:
            n_frames: Total number of frames to generate (across all nodes).
            dt: Simulation time step in seconds.
            mode: "detection", "adsb", or "anomalous".

        Returns:
            List of labeled frame dicts.
        """
        records = []
        frames_per_step = max(len(self.nodes), 1)
        n_steps = max(1, n_frames // frames_per_step)

        for step_i in range(n_steps):
            self.step(dt, mode=mode)
            timestamp_ms = int((self._time) * 1000)

            for node_id, node in self.nodes.items():
                rx_alt_km = node.rx_alt_ft * 0.3048 / 1000.0
                tx_alt_km = node.tx_alt_ft * 0.3048 / 1000.0
                rx_enu = (0.0, 0.0, 0.0)
                tx_enu = _lla_to_enu(
                    node.tx_lat, node.tx_lon, tx_alt_km,
                    node.rx_lat, node.rx_lon, rx_alt_km,
                )

                delays = []
                dopplers = []
                snrs = []
                ground_truth = []
                adsb_list = []

                for ac in self.aircraft:
                    if not self._aircraft_in_detection_cone(ac, node):
                        continue

                    target_enu = _lla_to_enu(
                        ac.lat, ac.lon, ac.alt_km,
                        node.rx_lat, node.rx_lon, rx_alt_km,
                    )
                    vel_enu = (ac.vel_east, ac.vel_north, ac.vel_up)

                    delay = _bistatic_delay(target_enu, tx_enu, rx_enu)
                    doppler = _bistatic_doppler(target_enu, vel_enu, tx_enu, rx_enu, node.fc_hz)

                    delay_noisy = delay + random.gauss(0, 0.3)
                    doppler_noisy = doppler + random.gauss(0, 2.0)

                    dist = _norm(target_enu)
                    base_snr = 25 - 10 * math.log10(max(dist, 1))
                    snr = max(base_snr + random.gauss(0, 2), 4.0)

                    delays.append(round(delay_noisy, 2))
                    dopplers.append(round(doppler_noisy, 2))
                    snrs.append(round(snr, 2))

                    ground_truth.append({
                        "object_id": ac.object_id,
                        "lat": round(ac.lat, 5),
                        "lon": round(ac.lon, 5),
                        "alt_km": round(ac.alt_km, 2),
                        "heading_deg": round(ac.heading_deg, 1),
                        "speed_ms": round(ac.speed_km_s * 1000, 1),
                        "has_adsb": ac.has_adsb,
                        "is_anomalous": ac.is_anomalous,
                        "delay_true": round(delay, 4),
                        "doppler_true": round(doppler, 4),
                        "is_clutter": False,
                    })

                    if ac.has_adsb:
                        adsb_list.append({
                            "hex": ac.adsb_hex,
                            "flight": ac.adsb_callsign,
                            "lat": round(ac.lat, 5),
                            "lon": round(ac.lon, 5),
                            "alt_baro": round(ac.alt_km * 1000 / 0.3048),
                            "gs": round(ac.speed_km_s * 1000 * 1.94384, 1),
                            "track": round(ac.heading_deg, 1),
                        })
                    else:
                        adsb_list.append(None)

                # Add clutter with ground truth label
                n_clutter = random.randint(0, 3)
                for _ in range(n_clutter):
                    delays.append(round(random.uniform(0, 60), 2))
                    dopplers.append(round(random.uniform(node.doppler_min, node.doppler_max), 2))
                    snrs.append(round(random.uniform(4, 7), 2))
                    ground_truth.append({
                        "object_id": None,
                        "is_clutter": True,
                        "is_anomalous": False,
                        "has_adsb": False,
                    })
                    adsb_list.append(None)

                record = {
                    "node_id": node_id,
                    "timestamp": timestamp_ms,
                    "delay": delays,
                    "doppler": dopplers,
                    "snr": snrs,
                    "ground_truth": ground_truth,
                }
                if mode in ("adsb", "anomalous"):
                    record["adsb"] = adsb_list

                records.append(record)

        return records

    def export_training_ndjson(self, path: str, n_frames: int = 10000,
                               dt: float = 0.5, mode: str = "adsb"):
        """Export training data as newline-delimited JSON file.

        Fast bulk export for ML training pipelines.
        """
        records = self.generate_training_batch(n_frames, dt, mode)
        with open(path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        return len(records)
