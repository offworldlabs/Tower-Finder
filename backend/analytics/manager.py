"""Central analytics aggregator for all connected nodes."""

import os
import time
import threading

from analytics.trust import AdsReportEntry, TrustScoreState
from analytics.detection_area import DetectionAreaState
from analytics.metrics import NodeMetrics
from analytics.reputation import NodeReputation
from analytics.coverage import HistoricalCoverageMap
from analytics.empirical_coverage import EmpiricalCoverageState
from analytics.cross_node import compute_delay_bin_overlap, coverage_suggestion
from analytics.constants import YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM, haversine_km, bearing_deg


class NodeAnalyticsManager:
    """Central analytics aggregator for all connected nodes."""

    _ANALYSIS_CACHE_TTL = 60  # seconds

    def __init__(self, storage_dir: str = ""):
        self.trust_scores: dict[str, TrustScoreState] = {}
        self.detection_areas: dict[str, DetectionAreaState] = {}
        self.metrics: dict[str, NodeMetrics] = {}
        self.reputations: dict[str, NodeReputation] = {}
        self.coverage_maps: dict[str, HistoricalCoverageMap] = {}
        self.empirical_coverages: dict[str, EmpiricalCoverageState] = {}
        self._storage_dir = storage_dir
        self._last_save_time = 0.0
        self._save_interval_s = 300.0
        self._cross_node_cache: dict | None = None
        self._cross_node_cache_ts: float = 0.0
        self._summaries_cache: dict | None = None
        self._summaries_cache_ts: float = 0.0
        self._analytics_lock = threading.Lock()
        self._save_lock = threading.Lock()
        if storage_dir:
            self._load_coverage_maps()

    def register_node(self, node_id: str, config: dict):
        if node_id not in self.trust_scores:
            self.trust_scores[node_id] = TrustScoreState(node_id=node_id)

        rx_lat = config.get("rx_lat", 0)
        rx_lon = config.get("rx_lon", 0)
        tx_lat = config.get("tx_lat", 0)
        tx_lon = config.get("tx_lon", 0)
        # Yagi points broadside — perpendicular to RX→TX baseline
        beam_az = (bearing_deg(rx_lat, rx_lon, tx_lat, tx_lon) + 90.0) % 360.0
        self.detection_areas[node_id] = DetectionAreaState(
            node_id=node_id,
            rx_lat=rx_lat,
            rx_lon=rx_lon,
            tx_lat=tx_lat,
            tx_lon=tx_lon,
            fc_hz=config.get("fc_hz", config.get("FC", 195e6)),
            beam_azimuth_deg=beam_az,
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

        if node_id not in self.empirical_coverages:
            self.empirical_coverages[node_id] = EmpiricalCoverageState(
                rx_lat=rx_lat, rx_lon=rx_lon,
            )

    def is_node_blocked(self, node_id: str) -> bool:
        rep = self.reputations.get(node_id)
        return rep.blocked if rep else False

    def record_calibration_point(self, node_id: str, lat: float, lon: float) -> None:
        """Record a confirmed detection at a known target position (ADS-B or multinode)."""
        ec = self.empirical_coverages.get(node_id)
        if ec is not None:
            ec.add_point(lat, lon)

    def record_detection_frame(self, node_id: str, frame: dict):
        if self.is_node_blocked(node_id):
            return False
        if node_id in self.detection_areas:
            self.detection_areas[node_id].update_from_frame(frame)
        if node_id in self.metrics:
            self.metrics[node_id].record_frame(frame)
        return True

    def record_adsb_correlation(self, node_id: str, entry: AdsReportEntry):
        if node_id not in self.trust_scores:
            self.trust_scores[node_id] = TrustScoreState(node_id=node_id)
        self.trust_scores[node_id].add_sample(entry)

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
        for node_id, rep in self.reputations.items():
            ts = self.trust_scores.get(node_id)
            if ts and ts.samples:
                rep.evaluate_trust(ts.score)

            metrics = self.metrics.get(node_id)
            if metrics:
                rep.evaluate_heartbeat(metrics.last_heartbeat)
                rep.evaluate_detection_rate(metrics.avg_detections_per_frame)

        node_ids = sorted(self.reputations.keys())
        for i, a_id in enumerate(node_ids):
            for b_id in node_ids[i + 1:]:
                area_a = self.detection_areas.get(a_id)
                area_b = self.detection_areas.get(b_id)
                if area_a and area_b and area_a.n_detections > 0 and area_b.n_detections > 0:
                    dist = haversine_km(area_a.rx_lat, area_a.rx_lon,
                                        area_b.rx_lat, area_b.rx_lon)
                    if dist > area_a.max_range_km + area_b.max_range_km:
                        continue
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
        ec = self.empirical_coverages.get(node_id)
        if ec is not None:
            da = self.detection_areas.get(node_id)
            poly_kwargs = {}
            if da is not None:
                poly_kwargs["beam_azimuth_deg"] = da.beam_azimuth_deg
                poly_kwargs["beam_width_deg"] = da.beam_width_deg
            result["empirical_coverage"] = {
                "n_points": ec.n_points,
                "n_filled_bins": ec.n_filled_bins,
                "polygon": ec.to_polygon(**poly_kwargs),
            }
        return result

    def get_all_summaries(self) -> dict:
        now = time.monotonic()
        if self._summaries_cache is not None and now - self._summaries_cache_ts < self._ANALYSIS_CACHE_TTL:
            return self._summaries_cache
        with self._analytics_lock:
            # Double-check after acquiring lock
            now = time.monotonic()
            if self._summaries_cache is not None and now - self._summaries_cache_ts < self._ANALYSIS_CACHE_TTL:
                return self._summaries_cache
            all_nodes = (set(self.trust_scores) | set(self.detection_areas)
                         | set(self.metrics) | set(self.reputations))
            result = {nid: self.get_node_summary(nid) for nid in sorted(all_nodes)}
            self._summaries_cache = result
            self._summaries_cache_ts = now
            return result

    def get_cross_node_analysis(self) -> dict:
        now = time.monotonic()
        if self._cross_node_cache is not None and now - self._cross_node_cache_ts < self._ANALYSIS_CACHE_TTL:
            return self._cross_node_cache
        with self._analytics_lock:
            # Double-check after acquiring lock
            now = time.monotonic()
            if self._cross_node_cache is not None and now - self._cross_node_cache_ts < self._ANALYSIS_CACHE_TTL:
                return self._cross_node_cache

            node_ids = sorted(self.detection_areas.keys())
            pair_overlaps = []
            # Only compute pair overlaps for nodes within range of each other
            for i, a_id in enumerate(node_ids):
                area_a = self.detection_areas[a_id]
                for b_id in node_ids[i + 1:]:
                    area_b = self.detection_areas[b_id]
                    dist = haversine_km(area_a.rx_lat, area_a.rx_lon,
                                        area_b.rx_lat, area_b.rx_lon)
                    if dist > area_a.max_range_km + area_b.max_range_km:
                        continue
                    overlap = compute_delay_bin_overlap(area_a, area_b)
                    if overlap["overlap_ratio"] > 0:
                        pair_overlaps.append({
                            "node_a": a_id,
                            "node_b": b_id,
                            **overlap,
                        })

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

            blocked = [
                nid for nid, rep in self.reputations.items() if rep.blocked
            ]

            result = {
                "pair_overlaps": pair_overlaps,
                "coverage_suggestions": suggestions,
                "blocked_nodes": blocked,
            }
            self._cross_node_cache = result
            self._cross_node_cache_ts = now
            return result

    # ── Persistent storage ────────────────────────────────────────────────

    def _coverage_map_path(self, node_id: str) -> str:
        safe_id = node_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._storage_dir, f"coverage_{safe_id}.json")

    def _empirical_path(self, node_id: str) -> str:
        safe_id = node_id.replace("/", "_").replace("\\", "_")
        return os.path.join(self._storage_dir, f"empirical_{safe_id}.json")

    def _load_coverage_maps(self):
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
            elif fname.startswith("empirical_") and fname.endswith(".json"):
                try:
                    path = os.path.join(self._storage_dir, fname)
                    ec = EmpiricalCoverageState.load_from_file(path)
                    # Derive node_id from filename: empirical_<safe_id>.json
                    node_id = fname[len("empirical_"):-len(".json")]
                    self.empirical_coverages[node_id] = ec
                except Exception:
                    pass

    def save_coverage_maps(self):
        if not self._storage_dir:
            return
        os.makedirs(self._storage_dir, exist_ok=True)
        for node_id, cmap in self.coverage_maps.items():
            if cmap.entries:
                cmap.save_to_file(self._coverage_map_path(node_id))
        for node_id, ec in self.empirical_coverages.items():
            if ec.n_points > 0:
                ec.save_to_file(self._empirical_path(node_id))
        self._last_save_time = time.time()

    def maybe_auto_save(self):
        if not self._storage_dir:
            return
        if (time.time() - self._last_save_time) <= self._save_interval_s:
            return
        # Non-blocking: only one thread runs the save; others skip immediately.
        if not self._save_lock.acquire(blocking=False):
            return
        try:
            # Double-check interval after acquiring lock (another thread may have
            # already updated _last_save_time inside save_coverage_maps).
            if (time.time() - self._last_save_time) <= self._save_interval_s:
                return
            self.save_coverage_maps()
        finally:
            self._save_lock.release()
