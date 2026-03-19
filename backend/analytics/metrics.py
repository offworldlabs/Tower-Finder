"""Per-node uptime, SNR, and track quality metrics."""

import time
from dataclasses import dataclass, field


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
    _max_frame_ts: int = 500
    gap_threshold_s: float = 60.0

    def record_frame(self, frame: dict):
        self.total_frames += 1
        delays = frame.get("delay", [])
        self.total_detections += len(delays)
        for s in frame.get("snr", []):
            self._snr_sum += s
            self._snr_count += 1
            if s > self._snr_max:
                self._snr_max = s
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
