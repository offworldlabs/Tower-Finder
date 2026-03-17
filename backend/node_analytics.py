"""
Node Analytics for Retina Passive Radar Network.

Provides per-node and cross-node analytics:
  - Trust Score: ADS-B correlation accuracy
  - Detection Area characterisation: observed delay/Doppler bounds → geographic footprint
  - Uptime, SNR, track quality metrics
  - Cross-node comparison (delay bin overlap)
  - Bad actor detection and blocking
  - Historical coverage map accumulation with persistent storage
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

C_KM_US = 0.299792458   # speed of light km/μs
R_EARTH = 6371.0         # Earth radius km

# Yagi antenna spec
YAGI_BEAM_WIDTH_DEG = 41.0   # typical 40-42° half-power beamwidth
YAGI_MAX_RANGE_KM = 50.0


def _haversine_km(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Trust Score ──────────────────────────────────────────────────────────────

@dataclass
class AdsReportEntry:
    """A single ADS-B correlation sample."""
    timestamp_ms: int
    predicted_delay: float
    predicted_doppler: float
    measured_delay: float
    measured_doppler: float
    adsb_hex: str
    adsb_lat: float
    adsb_lon: float


@dataclass
class TrustScoreState:
    """Running trust score for one node."""
    node_id: str
    samples: list[AdsReportEntry] = field(default_factory=list)
    max_samples: int = 500
    # Thresholds
    delay_threshold_us: float = 5.0
    doppler_threshold_hz: float = 20.0

    def add_sample(self, entry: AdsReportEntry):
        self.samples.append(entry)
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples:]

    @property
    def score(self) -> float:
        """Trust score 0‒1.  Higher = better ADS-B correlation."""
        if not self.samples:
            return 0.0
        good = 0
        for s in self.samples:
            delay_err = abs(s.predicted_delay - s.measured_delay)
            doppler_err = abs(s.predicted_doppler - s.measured_doppler)
            if delay_err < self.delay_threshold_us and doppler_err < self.doppler_threshold_hz:
                good += 1
        return good / len(self.samples)

    @property
    def rms_delay_error(self) -> float:
        if not self.samples:
            return 0.0
        return math.sqrt(
            sum((s.predicted_delay - s.measured_delay) ** 2 for s in self.samples)
            / len(self.samples)
        )

    @property
    def rms_doppler_error(self) -> float:
        if not self.samples:
            return 0.0
        return math.sqrt(
            sum((s.predicted_doppler - s.measured_doppler) ** 2 for s in self.samples)
            / len(self.samples)
        )

    def summary(self) -> dict:
        return {
            "node_id": self.node_id,
            "trust_score": round(self.score, 4),
            "rms_delay_error_us": round(self.rms_delay_error, 3),
            "rms_doppler_error_hz": round(self.rms_doppler_error, 3),
            "n_samples": len(self.samples),
        }


# ── Detection Area ───────────────────────────────────────────────────────────

@dataclass
class DetectionAreaState:
    """Characterises the geographic detection footprint of one node from
    observed delay/Doppler bounds."""
    node_id: str
    # Node geometry (set once at registration)
    rx_lat: float = 0.0
    rx_lon: float = 0.0
    tx_lat: float = 0.0
    tx_lon: float = 0.0
    fc_hz: float = 195e6
    beam_azimuth_deg: float = 0.0
    beam_width_deg: float = YAGI_BEAM_WIDTH_DEG
    max_range_km: float = YAGI_MAX_RANGE_KM
    # Running bounds from actual detections
    min_delay: float = float("inf")
    max_delay: float = float("-inf")
    min_doppler: float = float("inf")
    max_doppler: float = float("-inf")
    n_detections: int = 0

    def update(self, delay: float, doppler: float):
        self.min_delay = min(self.min_delay, delay)
        self.max_delay = max(self.max_delay, delay)
        self.min_doppler = min(self.min_doppler, doppler)
        self.max_doppler = max(self.max_doppler, doppler)
        self.n_detections += 1

    def update_from_frame(self, frame: dict):
        """Update from a raw detection frame {delay:[], doppler:[], ...}."""
        for d, f in zip(frame.get("delay", []), frame.get("doppler", [])):
            self.update(d, f)

    @property
    def delay_range(self) -> tuple[float, float]:
        if self.n_detections == 0:
            return (0.0, 0.0)
        return (self.min_delay, self.max_delay)

    @property
    def doppler_range(self) -> tuple[float, float]:
        if self.n_detections == 0:
            return (0.0, 0.0)
        return (self.min_doppler, self.max_doppler)

    @property
    def estimated_max_range_km(self) -> float:
        """Estimate max bistatic range from max observed delay."""
        if self.n_detections == 0:
            return 0.0
        return self.max_delay * C_KM_US

    def summary(self) -> dict:
        return {
            "node_id": self.node_id,
            "rx": {"lat": self.rx_lat, "lon": self.rx_lon},
            "tx": {"lat": self.tx_lat, "lon": self.tx_lon},
            "beam_azimuth_deg": round(self.beam_azimuth_deg, 1),
            "beam_width_deg": self.beam_width_deg,
            "max_range_km": self.max_range_km,
            "observed_delay_range_us": [round(x, 2) for x in self.delay_range],
            "observed_doppler_range_hz": [round(x, 2) for x in self.doppler_range],
            "estimated_max_range_km": round(self.estimated_max_range_km, 2),
            "n_detections": self.n_detections,
        }


# ── Per-Node Metrics ─────────────────────────────────────────────────────────

@dataclass
class NodeMetrics:
    """Uptime / SNR / track quality metrics for one node."""
    node_id: str
    connected_at: float = 0.0
    last_heartbeat: float = 0.0
    total_frames: int = 0
    total_detections: int = 0
    total_tracks: int = 0
    geolocated_tracks: int = 0
    # SNR stats
    _snr_sum: float = 0.0
    _snr_count: int = 0
    _snr_max: float = 0.0
    # Track quality / gap detection
    _frame_timestamps: list = field(default_factory=list)
    _max_frame_ts: int = 500  # keep last N frame timestamps
    gap_threshold_s: float = 60.0  # gaps longer than this count as "detection gaps"

    def record_frame(self, frame: dict):
        self.total_frames += 1
        delays = frame.get("delay", [])
        self.total_detections += len(delays)
        for s in frame.get("snr", []):
            self._snr_sum += s
            self._snr_count += 1
            if s > self._snr_max:
                self._snr_max = s
        # Track timestamps for gap analysis
        ts = frame.get("timestamp")
        if ts is not None:
            self._frame_timestamps.append(ts / 1000.0 if ts > 1e12 else ts)
            if len(self._frame_timestamps) > self._max_frame_ts:
                self._frame_timestamps = self._frame_timestamps[-self._max_frame_ts:]

    def record_heartbeat(self):
        self.last_heartbeat = time.time()

    @property
    def uptime_s(self) -> float:
        if self.connected_at == 0:
            return 0.0
        return time.time() - self.connected_at

    @property
    def avg_snr(self) -> float:
        return self._snr_sum / self._snr_count if self._snr_count else 0.0

    @property
    def avg_detections_per_frame(self) -> float:
        return self.total_detections / self.total_frames if self.total_frames else 0.0

    @property
    def gap_stats(self) -> dict:
        """Compute detection gap statistics from frame timestamps.

        Returns gap_count, avg_gap_s, max_gap_s, and continuity_ratio.
        continuity_ratio = 1.0 means no gaps above threshold.
        """
        if len(self._frame_timestamps) < 2:
            return {"gap_count": 0, "avg_gap_s": 0.0, "max_gap_s": 0.0,
                    "continuity_ratio": 1.0}
        ts_sorted = sorted(self._frame_timestamps)
        gaps = []
        total_intervals = 0
        good_intervals = 0
        for i in range(1, len(ts_sorted)):
            dt = ts_sorted[i] - ts_sorted[i - 1]
            total_intervals += 1
            if dt > self.gap_threshold_s:
                gaps.append(dt)
            else:
                good_intervals += 1
        return {
            "gap_count": len(gaps),
            "avg_gap_s": round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
            "max_gap_s": round(max(gaps), 2) if gaps else 0.0,
            "continuity_ratio": round(good_intervals / total_intervals, 4) if total_intervals else 1.0,
        }

    def summary(self) -> dict:
        return {
            "node_id": self.node_id,
            "uptime_s": round(self.uptime_s, 1),
            "total_frames": self.total_frames,
            "total_detections": self.total_detections,
            "avg_detections_per_frame": round(self.avg_detections_per_frame, 2),
            "avg_snr": round(self.avg_snr, 2),
            "max_snr": round(self._snr_max, 2),
            "total_tracks": self.total_tracks,
            "geolocated_tracks": self.geolocated_tracks,
            "track_quality": self.gap_stats,
        }


# ── Bad Actor Detection ──────────────────────────────────────────────────────

@dataclass
class NodeReputation:
    """Tracks reputation and handles bad actor detection/blocking for a node.

    A node's reputation degrades when:
      - Trust score drops below threshold
      - Detection patterns don't match trusted neighbours
      - Excessive missed heartbeats
      - Anomalous data patterns (e.g. impossibly high SNR or detection rates)

    A node is blocked when its reputation score falls below the block threshold.
    """
    node_id: str
    reputation: float = 1.0        # 0-1, starts at 1
    blocked: bool = False
    block_reason: str = ""
    # Configurable thresholds
    trust_warn_threshold: float = 0.3    # warn below this trust score
    trust_block_threshold: float = 0.1   # block below this trust score
    reputation_block_threshold: float = 0.2
    # Penalty tracking
    penalties: list[dict] = field(default_factory=list)
    max_penalties: int = 100
    # Anomaly detection
    max_detections_per_frame: float = 50.0  # suspicious if consistently above
    min_heartbeat_interval_s: float = 300.0  # block if no heartbeat for this long

    def apply_penalty(self, amount: float, reason: str):
        """Apply a reputation penalty (clamped to [0, 1])."""
        self.reputation = max(0.0, self.reputation - amount)
        self.penalties.append({
            "time": time.time(),
            "amount": amount,
            "reason": reason,
            "reputation_after": self.reputation,
        })
        if len(self.penalties) > self.max_penalties:
            self.penalties = self.penalties[-self.max_penalties:]
        if self.reputation < self.reputation_block_threshold and not self.blocked:
            self.blocked = True
            self.block_reason = f"Reputation {self.reputation:.2f} below threshold"

    def apply_reward(self, amount: float):
        """Slowly restore reputation for good behaviour."""
        if not self.blocked:
            self.reputation = min(1.0, self.reputation + amount)

    def evaluate_trust(self, trust_score: float):
        """Evaluate trust score and apply penalties/rewards."""
        if trust_score < self.trust_block_threshold:
            self.apply_penalty(0.15, f"Trust score critically low: {trust_score:.3f}")
        elif trust_score < self.trust_warn_threshold:
            self.apply_penalty(0.05, f"Trust score low: {trust_score:.3f}")
        elif trust_score > 0.7:
            self.apply_reward(0.01)

    def evaluate_heartbeat(self, last_heartbeat: float):
        """Penalise if heartbeat is stale."""
        if last_heartbeat > 0:
            gap = time.time() - last_heartbeat
            if gap > self.min_heartbeat_interval_s:
                self.apply_penalty(0.1, f"Heartbeat stale: {gap:.0f}s")

    def evaluate_detection_rate(self, avg_det_per_frame: float):
        """Penalise suspiciously high detection rates."""
        if avg_det_per_frame > self.max_detections_per_frame:
            self.apply_penalty(0.05, f"High detection rate: {avg_det_per_frame:.1f}/frame")

    def evaluate_neighbour_consistency(self, overlap_ratio: float, neighbour_trust: float):
        """Penalise if this node's data doesn't match trusted neighbours."""
        if neighbour_trust > 0.7 and overlap_ratio < 0.05:
            self.apply_penalty(0.08, f"Inconsistent with trusted neighbour (overlap={overlap_ratio:.2f})")

    def unblock(self):
        """Manually unblock a node (admin action)."""
        self.blocked = False
        self.block_reason = ""
        self.reputation = 0.3  # reset to low but not blocked

    def summary(self) -> dict:
        return {
            "node_id": self.node_id,
            "reputation": round(self.reputation, 4),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "n_penalties": len(self.penalties),
            "recent_penalties": self.penalties[-5:] if self.penalties else [],
        }


# ── Historical Coverage Map ──────────────────────────────────────────────────

@dataclass
class CoverageMapEntry:
    """A single ADS-B-validated detection position."""
    lat: float
    lon: float
    alt_km: float
    timestamp: float
    snr: float
    delay_error: float  # how close the measured delay matched ADS-B prediction


@dataclass
class HistoricalCoverageMap:
    """Accumulates ADS-B-validated detection positions over time to build
    a factual coverage map for each node.

    As ADS-B-equipped aircraft are detected and correlated, their positions
    are recorded. Over time this builds a real-world map of where the node
    actually detects targets, accounting for terrain, interference, etc.
    """
    node_id: str
    entries: list[CoverageMapEntry] = field(default_factory=list)
    max_entries: int = 10000
    # Grid-based summary (lat/lon bucketed at ~1km resolution)
    _grid: dict[tuple[int, int], dict] = field(default_factory=dict)
    _grid_resolution_deg: float = 0.01  # ~1.1 km

    def add_detection(self, lat: float, lon: float, alt_km: float,
                      snr: float, delay_error: float):
        """Record an ADS-B-validated detection position."""
        entry = CoverageMapEntry(
            lat=lat, lon=lon, alt_km=alt_km,
            timestamp=time.time(), snr=snr, delay_error=delay_error,
        )
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]

        # Update grid summary
        grid_key = (
            round(lat / self._grid_resolution_deg),
            round(lon / self._grid_resolution_deg),
        )
        cell = self._grid.get(grid_key)
        if cell is None:
            self._grid[grid_key] = {
                "lat": lat, "lon": lon,
                "count": 1, "avg_snr": snr,
                "first_seen": time.time(), "last_seen": time.time(),
            }
        else:
            cell["count"] += 1
            cell["avg_snr"] = (cell["avg_snr"] * (cell["count"] - 1) + snr) / cell["count"]
            cell["last_seen"] = time.time()

    @property
    def coverage_area_km2(self) -> float:
        """Estimate covered area from grid cells."""
        cell_area = (self._grid_resolution_deg * 111.0) ** 2  # rough km²
        return len(self._grid) * cell_area

    @property
    def n_grid_cells(self) -> int:
        return len(self._grid)

    def get_coverage_grid(self) -> list[dict]:
        """Return coverage grid as list of cells for map visualisation."""
        return [
            {
                "lat": cell["lat"],
                "lon": cell["lon"],
                "count": cell["count"],
                "avg_snr": round(cell["avg_snr"], 2),
                "first_seen": cell["first_seen"],
                "last_seen": cell["last_seen"],
            }
            for cell in self._grid.values()
        ]

    def estimate_beam_width(self) -> Optional[float]:
        """Estimate actual beam width from accumulated detections.

        Uses the angular spread of validated detections around the node
        to derive the effective beam width (may differ from the nominal
        40-42° Yagi spec due to terrain, installation, etc.).
        Returns None if insufficient data.
        """
        if len(self.entries) < 20:
            return None
        # Need node position to compute bearings — use centroid as proxy
        lats = [e.lat for e in self.entries]
        lons = [e.lon for e in self.entries]
        # Outlier-robust center: median
        lats_sorted = sorted(lats)
        lons_sorted = sorted(lons)
        mid = len(lats_sorted) // 2
        center_lat = lats_sorted[mid]
        center_lon = lons_sorted[mid]
        # Compute bearings from center to each detection
        bearings = []
        for e in self.entries:
            dlat = e.lat - center_lat
            dlon = (e.lon - center_lon) * math.cos(math.radians(center_lat))
            b = math.degrees(math.atan2(dlon, dlat)) % 360
            bearings.append(b)
        if not bearings:
            return None
        # Use circular spread (5th-95th percentile angular range)
        bearings.sort()
        # Handle circular wraparound
        gaps = [(bearings[i + 1] - bearings[i]) for i in range(len(bearings) - 1)]
        gaps.append(360 - bearings[-1] + bearings[0])
        max_gap_idx = gaps.index(max(gaps))
        # Rotate so max gap is at the end
        rotated = bearings[max_gap_idx + 1:] + bearings[:max_gap_idx + 1]
        if not rotated:
            return None
        spread = (rotated[-1] - rotated[0]) % 360
        return min(spread, 180.0)  # cap at 180°

    def summary(self) -> dict:
        beam_est = self.estimate_beam_width()
        return {
            "node_id": self.node_id,
            "total_entries": len(self.entries),
            "grid_cells": self.n_grid_cells,
            "coverage_area_km2": round(self.coverage_area_km2, 1),
            "estimated_beam_width_deg": round(beam_est, 1) if beam_est else None,
        }

    def save_to_file(self, path: str):
        """Persist coverage map to a JSON file."""
        data = {
            "node_id": self.node_id,
            "entries": [
                {"lat": e.lat, "lon": e.lon, "alt_km": e.alt_km,
                 "timestamp": e.timestamp, "snr": e.snr,
                 "delay_error": e.delay_error}
                for e in self.entries
            ],
            "grid": {
                f"{k[0]},{k[1]}": v for k, v in self._grid.items()
            },
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    @classmethod
    def load_from_file(cls, path: str) -> "HistoricalCoverageMap":
        """Load a coverage map from a JSON file."""
        with open(path, "r") as f:
            data = json.load(f)
        cmap = cls(node_id=data["node_id"])
        for e in data.get("entries", []):
            cmap.entries.append(CoverageMapEntry(
                lat=e["lat"], lon=e["lon"], alt_km=e["alt_km"],
                timestamp=e["timestamp"], snr=e["snr"],
                delay_error=e["delay_error"],
            ))
        for k_str, v in data.get("grid", {}).items():
            parts = k_str.split(",")
            cmap._grid[(int(parts[0]), int(parts[1]))] = v
        return cmap


# ── Cross-Node Comparison ────────────────────────────────────────────────────

def compute_delay_bin_overlap(area_a: DetectionAreaState,
                              area_b: DetectionAreaState,
                              bin_width_us: float = 2.0) -> dict:
    """Compare two nodes' detection areas via delay-bin overlap.

    Bins the observed delay ranges and computes the Jaccard overlap.
    Returns:
        {overlap_ratio, shared_bins, total_bins_union, a_only, b_only}
    """
    if area_a.n_detections == 0 or area_b.n_detections == 0:
        return {"overlap_ratio": 0.0, "shared_bins": 0,
                "total_bins_union": 0, "a_only": 0, "b_only": 0}

    def _bins(lo, hi):
        if lo > hi:
            return set()
        start = int(lo // bin_width_us)
        end = int(hi // bin_width_us) + 1
        return set(range(start, end))

    bins_a = _bins(area_a.min_delay, area_a.max_delay)
    bins_b = _bins(area_b.min_delay, area_b.max_delay)
    shared = bins_a & bins_b
    union = bins_a | bins_b

    return {
        "overlap_ratio": len(shared) / len(union) if union else 0.0,
        "shared_bins": len(shared),
        "total_bins_union": len(union),
        "a_only": len(bins_a - bins_b),
        "b_only": len(bins_b - bins_a),
    }


def _point_in_beam(area: DetectionAreaState, lat: float, lon: float) -> bool:
    """Check whether a lat/lon point falls inside a node's detection cone."""
    dist = _haversine_km(area.rx_lat, area.rx_lon, lat, lon)
    if dist > area.max_range_km:
        return False
    dlat = lat - area.rx_lat
    dlon = lon - area.rx_lon
    bearing = math.degrees(math.atan2(
        dlon * math.cos(math.radians(area.rx_lat)), dlat
    )) % 360
    angle_diff = abs((bearing - area.beam_azimuth_deg + 180) % 360 - 180)
    return angle_diff < area.beam_width_deg / 2


def _count_covering_nodes(areas: list[DetectionAreaState],
                          lat: float, lon: float) -> int:
    """How many nodes' beams cover a given point."""
    return sum(1 for a in areas if _point_in_beam(a, lat, lon))


def coverage_suggestion(areas: list[DetectionAreaState],
                        center_lat: float, center_lon: float,
                        desired_range_km: float = 80.0,
                        trust_scores: dict | None = None,
                        solver_rms_history: list[float] | None = None,
                        ) -> list[dict]:
    """Suggest where to place additional nodes for better coverage.

    Strategy 1 (densification): When the network is young / sparse,
    prioritise locations that maximise overlap with existing high-trust
    nodes so that the solver gets redundant measurements.

    Strategy 2 (expansion): Once the network is saturated (solver RMS
    has plateaued), switch to suggesting locations that extend the
    geographic footprint.

    The strategy field in each suggestion indicates which mode was used.
    """
    suggestions = []
    directions = [
        ("N", 0), ("NE", 45), ("E", 90), ("SE", 135),
        ("S", 180), ("SW", 225), ("W", 270), ("NW", 315),
    ]

    # Decide strategy: expansion if solver has saturated, otherwise densification
    saturated = False
    if solver_rms_history and len(solver_rms_history) >= 10:
        recent = solver_rms_history[-10:]
        improvement = (recent[0] - recent[-1]) / max(recent[0], 0.001)
        saturated = improvement < 0.05  # <5% improvement in last 10 windows

    # Count node pairs that already have overlap
    n_overlap_pairs = 0
    for i, a in enumerate(areas):
        for b in areas[i + 1:]:
            dist = _haversine_km(a.rx_lat, a.rx_lon, b.rx_lat, b.rx_lon)
            if dist < a.max_range_km + b.max_range_km:
                n_overlap_pairs += 1
    max_pairs = len(areas) * (len(areas) - 1) / 2 if len(areas) > 1 else 1
    overlap_density = n_overlap_pairs / max_pairs if max_pairs else 0

    # Force densification if network is very sparse
    use_expansion = saturated and overlap_density > 0.3
    strategy_label = "expansion" if use_expansion else "densification"

    for label, bearing_deg in directions:
        bearing_rad = math.radians(bearing_deg)
        test_lat = center_lat + (desired_range_km / R_EARTH) * math.degrees(1) * math.cos(bearing_rad) / 111.32
        test_lon = center_lon + (desired_range_km / R_EARTH) * math.degrees(1) * math.sin(bearing_rad) / (111.32 * math.cos(math.radians(center_lat)))

        covered = any(_point_in_beam(a, test_lat, test_lon) for a in areas)

        if use_expansion:
            # Strategy 2: suggest uncovered directions to expand footprint
            if not covered:
                suggestions.append({
                    "direction": label,
                    "bearing_deg": bearing_deg,
                    "test_point": {"lat": round(test_lat, 5), "lon": round(test_lon, 5)},
                    "gap_km": round(desired_range_km, 1),
                    "strategy": "expansion",
                    "overlap_count": 0,
                })
        else:
            # Strategy 1: suggest locations near existing high-trust nodes
            # to maximise overlapping coverage
            if covered:
                n_covering = _count_covering_nodes(areas, test_lat, test_lon)
                if n_covering < 3:  # only suggest if not already well-covered
                    # Find the nearest high-trust node
                    best_trust = 0.0
                    if trust_scores:
                        for a in areas:
                            ts = trust_scores.get(a.node_id)
                            if ts and ts.score > best_trust and _point_in_beam(a, test_lat, test_lon):
                                best_trust = ts.score
                    suggestions.append({
                        "direction": label,
                        "bearing_deg": bearing_deg,
                        "test_point": {"lat": round(test_lat, 5), "lon": round(test_lon, 5)},
                        "gap_km": round(desired_range_km, 1),
                        "strategy": "densification",
                        "overlap_count": n_covering,
                        "nearest_trust": round(best_trust, 3),
                    })
            else:
                # Even in densification mode, flag uncovered directions
                suggestions.append({
                    "direction": label,
                    "bearing_deg": bearing_deg,
                    "test_point": {"lat": round(test_lat, 5), "lon": round(test_lon, 5)},
                    "gap_km": round(desired_range_km, 1),
                    "strategy": "expansion",
                    "overlap_count": 0,
                })

    # Sort: densification suggestions first (higher overlap_count = higher priority)
    suggestions.sort(key=lambda s: (-1 if s["strategy"] == "densification" else 0, -s.get("overlap_count", 0)))

    return suggestions


# ── NodeAnalyticsManager ─────────────────────────────────────────────────────

class NodeAnalyticsManager:
    """Central analytics aggregator for all connected nodes."""

    def __init__(self, storage_dir: str = ""):
        self.trust_scores: dict[str, TrustScoreState] = {}
        self.detection_areas: dict[str, DetectionAreaState] = {}
        self.metrics: dict[str, NodeMetrics] = {}
        self.reputations: dict[str, NodeReputation] = {}
        self.coverage_maps: dict[str, HistoricalCoverageMap] = {}
        self._storage_dir = storage_dir
        self._last_save_time = 0.0
        self._save_interval_s = 300.0  # auto-save every 5 minutes
        if storage_dir:
            self._load_coverage_maps()

    def register_node(self, node_id: str, config: dict):
        """Register a node when it connects with its config."""
        if node_id not in self.trust_scores:
            self.trust_scores[node_id] = TrustScoreState(node_id=node_id)

        self.detection_areas[node_id] = DetectionAreaState(
            node_id=node_id,
            rx_lat=config.get("rx_lat", 0),
            rx_lon=config.get("rx_lon", 0),
            tx_lat=config.get("tx_lat", 0),
            tx_lon=config.get("tx_lon", 0),
            fc_hz=config.get("fc_hz", config.get("FC", 195e6)),
            beam_width_deg=config.get("beam_width_deg", YAGI_BEAM_WIDTH_DEG),
            max_range_km=config.get("max_range_km", YAGI_MAX_RANGE_KM),
        )

        self.metrics[node_id] = NodeMetrics(
            node_id=node_id,
            connected_at=time.time(),
        )

        if node_id not in self.reputations:
            self.reputations[node_id] = NodeReputation(node_id=node_id)

        if node_id not in self.coverage_maps:
            self.coverage_maps[node_id] = HistoricalCoverageMap(node_id=node_id)

    def is_node_blocked(self, node_id: str) -> bool:
        """Check if a node is blocked due to bad reputation."""
        rep = self.reputations.get(node_id)
        return rep.blocked if rep else False

    def record_detection_frame(self, node_id: str, frame: dict):
        """Record an incoming detection frame for analytics.

        Returns False if node is blocked (data should be discarded).
        """
        if self.is_node_blocked(node_id):
            return False
        if node_id in self.detection_areas:
            self.detection_areas[node_id].update_from_frame(frame)
        if node_id in self.metrics:
            self.metrics[node_id].record_frame(frame)
        return True

    def record_adsb_correlation(self, node_id: str, entry: AdsReportEntry):
        """Record an ADS-B correlation sample for trust scoring and coverage map."""
        if node_id not in self.trust_scores:
            self.trust_scores[node_id] = TrustScoreState(node_id=node_id)
        self.trust_scores[node_id].add_sample(entry)

        # Update coverage map with validated position
        delay_err = abs(entry.predicted_delay - entry.measured_delay)
        if node_id in self.coverage_maps:
            self.coverage_maps[node_id].add_detection(
                lat=entry.adsb_lat, lon=entry.adsb_lon, alt_km=0.0,
                snr=0.0, delay_error=delay_err,
            )

    def record_heartbeat(self, node_id: str):
        if node_id in self.metrics:
            self.metrics[node_id].record_heartbeat()

    def evaluate_reputations(self):
        """Run periodic reputation evaluation across all nodes.

        Should be called on a timer (e.g. every 60s).
        """
        for node_id, rep in self.reputations.items():
            # Evaluate trust score
            ts = self.trust_scores.get(node_id)
            if ts and ts.samples:
                rep.evaluate_trust(ts.score)

            # Evaluate heartbeat freshness
            metrics = self.metrics.get(node_id)
            if metrics:
                rep.evaluate_heartbeat(metrics.last_heartbeat)
                rep.evaluate_detection_rate(metrics.avg_detections_per_frame)

        # Cross-node consistency checks
        node_ids = sorted(self.reputations.keys())
        for i, a_id in enumerate(node_ids):
            for b_id in node_ids[i + 1:]:
                area_a = self.detection_areas.get(a_id)
                area_b = self.detection_areas.get(b_id)
                if area_a and area_b and area_a.n_detections > 0 and area_b.n_detections > 0:
                    overlap = compute_delay_bin_overlap(area_a, area_b)
                    ts_a = self.trust_scores.get(a_id)
                    ts_b = self.trust_scores.get(b_id)
                    if ts_a and ts_b:
                        self.reputations[a_id].evaluate_neighbour_consistency(
                            overlap["overlap_ratio"], ts_b.score
                        )
                        self.reputations[b_id].evaluate_neighbour_consistency(
                            overlap["overlap_ratio"], ts_a.score
                        )

    def unblock_node(self, node_id: str):
        """Admin action: unblock a previously blocked node."""
        rep = self.reputations.get(node_id)
        if rep:
            rep.unblock()

    def get_node_summary(self, node_id: str) -> dict:
        result = {"node_id": node_id}
        if node_id in self.trust_scores:
            result["trust"] = self.trust_scores[node_id].summary()
        if node_id in self.detection_areas:
            result["detection_area"] = self.detection_areas[node_id].summary()
        if node_id in self.metrics:
            result["metrics"] = self.metrics[node_id].summary()
        if node_id in self.reputations:
            result["reputation"] = self.reputations[node_id].summary()
        if node_id in self.coverage_maps:
            result["coverage_map"] = self.coverage_maps[node_id].summary()
        return result

    def get_all_summaries(self) -> dict:
        all_nodes = (set(self.trust_scores) | set(self.detection_areas)
                     | set(self.metrics) | set(self.reputations))
        return {nid: self.get_node_summary(nid) for nid in sorted(all_nodes)}

    def get_cross_node_analysis(self) -> dict:
        """Compare all node pairs and suggest coverage improvements."""
        node_ids = sorted(self.detection_areas.keys())
        pair_overlaps = []
        for i, a_id in enumerate(node_ids):
            for b_id in node_ids[i + 1:]:
                overlap = compute_delay_bin_overlap(
                    self.detection_areas[a_id],
                    self.detection_areas[b_id],
                )
                pair_overlaps.append({
                    "node_a": a_id,
                    "node_b": b_id,
                    **overlap,
                })

        # Coverage suggestion with strategy awareness
        if self.detection_areas:
            areas = list(self.detection_areas.values())
            avg_lat = sum(a.rx_lat for a in areas) / len(areas)
            avg_lon = sum(a.rx_lon for a in areas) / len(areas)
            suggestions = coverage_suggestion(
                areas, avg_lat, avg_lon,
                trust_scores=self.trust_scores,
            )
        else:
            suggestions = []

        # Blocked nodes
        blocked = [
            nid for nid, rep in self.reputations.items() if rep.blocked
        ]

        return {
            "pair_overlaps": pair_overlaps,
            "coverage_suggestions": suggestions,
            "blocked_nodes": blocked,
        }

    # ── Persistent storage ────────────────────────────────────────────────

    def _coverage_map_path(self, node_id: str) -> str:
        safe_id = node_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._storage_dir, f"coverage_{safe_id}.json")

    def _load_coverage_maps(self):
        """Load all persisted coverage maps from storage_dir."""
        if not self._storage_dir or not os.path.isdir(self._storage_dir):
            return
        for fname in os.listdir(self._storage_dir):
            if fname.startswith("coverage_") and fname.endswith(".json"):
                try:
                    path = os.path.join(self._storage_dir, fname)
                    cmap = HistoricalCoverageMap.load_from_file(path)
                    self.coverage_maps[cmap.node_id] = cmap
                except Exception:
                    pass

    def save_coverage_maps(self):
        """Persist all coverage maps to disk."""
        if not self._storage_dir:
            return
        os.makedirs(self._storage_dir, exist_ok=True)
        for node_id, cmap in self.coverage_maps.items():
            if cmap.entries:
                cmap.save_to_file(self._coverage_map_path(node_id))
        self._last_save_time = time.time()

    def maybe_auto_save(self):
        """Auto-save coverage maps if enough time has passed."""
        if self._storage_dir and (time.time() - self._last_save_time) > self._save_interval_s:
            self.save_coverage_maps()
