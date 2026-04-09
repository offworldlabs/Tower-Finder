"""Detection area characterisation from observed delay/Doppler bounds."""

import heapq
import math
import time as _time
from dataclasses import dataclass, field

from analytics.constants import C_KM_US, YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM


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
    # Furthest verified detections (min-heap by distance, capped at 10)
    # Each entry: (dist_km, counter, dict) — counter breaks ties to avoid dict comparison
    furthest_detections: list = field(default_factory=list)
    _FURTHEST_MAX: int = 10
    _furthest_counter: int = field(default=0, repr=False, compare=False)

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

    def record_verified_detection(self, lat: float, lon: float, ac_hex: str = ""):
        """Record a detection with verified ADS-B position for range tracking."""
        dist_km = self._haversine_km(self.rx_lat, self.rx_lon, lat, lon)
        if dist_km < 0.5:
            return
        entry = {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "distance_km": round(dist_km, 2),
            "hex": ac_hex,
            "ts": round(_time.time(), 1),
        }
        # Use a counter as tiebreaker so heapq never compares dict entries
        self._furthest_counter += 1
        heap_entry = (dist_km, self._furthest_counter, entry)
        if len(self.furthest_detections) < self._FURTHEST_MAX:
            heapq.heappush(self.furthest_detections, heap_entry)
        elif dist_km > self.furthest_detections[0][0]:
            heapq.heapreplace(self.furthest_detections, heap_entry)

    @staticmethod
    def _haversine_km(lat1, lon1, lat2, lon2):
        dlat = (lat1 - lat2) * 111.0
        dlon = (lon1 - lon2) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
        return math.sqrt(dlat ** 2 + dlon ** 2)

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
        # Sort furthest detections by distance descending for output
        furthest = sorted(
            [e for _, _cnt, e in self.furthest_detections],
            key=lambda x: x["distance_km"],
            reverse=True,
        )
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
            "furthest_detections": furthest,
        }
