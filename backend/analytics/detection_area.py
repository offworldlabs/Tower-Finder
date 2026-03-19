"""Detection area characterisation from observed delay/Doppler bounds."""

from dataclasses import dataclass

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
