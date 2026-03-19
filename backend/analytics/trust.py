"""ADS-B correlation trust scoring per node."""

import math
from dataclasses import dataclass, field


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
        """Trust score 0-1.  Higher = better ADS-B correlation."""
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
