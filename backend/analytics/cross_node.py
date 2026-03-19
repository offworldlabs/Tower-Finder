"""Cross-node comparison and coverage suggestion algorithms."""

import math

from analytics.constants import haversine_km, R_EARTH
from analytics.detection_area import DetectionAreaState
from analytics.trust import TrustScoreState


def compute_delay_bin_overlap(area_a: DetectionAreaState,
                              area_b: DetectionAreaState,
                              bin_width_us: float = 2.0) -> dict:
    """Compare two nodes' detection areas via delay-bin overlap (Jaccard)."""
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
    dist = haversine_km(area.rx_lat, area.rx_lon, lat, lon)
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
    return sum(1 for a in areas if _point_in_beam(a, lat, lon))


def coverage_suggestion(areas: list[DetectionAreaState],
                        center_lat: float, center_lon: float,
                        desired_range_km: float = 80.0,
                        trust_scores: dict | None = None,
                        solver_rms_history: list[float] | None = None,
                        ) -> list[dict]:
    """Suggest where to place additional nodes for better coverage."""
    suggestions = []
    directions = [
        ("N", 0), ("NE", 45), ("E", 90), ("SE", 135),
        ("S", 180), ("SW", 225), ("W", 270), ("NW", 315),
    ]

    saturated = False
    if solver_rms_history and len(solver_rms_history) >= 10:
        recent = solver_rms_history[-10:]
        improvement = (recent[0] - recent[-1]) / max(recent[0], 0.001)
        saturated = improvement < 0.05

    n_overlap_pairs = 0
    for i, a in enumerate(areas):
        for b in areas[i + 1:]:
            dist = haversine_km(a.rx_lat, a.rx_lon, b.rx_lat, b.rx_lon)
            if dist < a.max_range_km + b.max_range_km:
                n_overlap_pairs += 1
    max_pairs = len(areas) * (len(areas) - 1) / 2 if len(areas) > 1 else 1
    overlap_density = n_overlap_pairs / max_pairs if max_pairs else 0

    use_expansion = saturated and overlap_density > 0.3
    strategy_label = "expansion" if use_expansion else "densification"

    for label, bearing_deg in directions:
        bearing_rad = math.radians(bearing_deg)
        test_lat = center_lat + (desired_range_km / R_EARTH) * math.degrees(1) * math.cos(bearing_rad) / 111.32
        test_lon = center_lon + (desired_range_km / R_EARTH) * math.degrees(1) * math.sin(bearing_rad) / (111.32 * math.cos(math.radians(center_lat)))

        covered = any(_point_in_beam(a, test_lat, test_lon) for a in areas)

        if use_expansion:
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
            if covered:
                n_covering = _count_covering_nodes(areas, test_lat, test_lon)
                if n_covering < 3:
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
                suggestions.append({
                    "direction": label,
                    "bearing_deg": bearing_deg,
                    "test_point": {"lat": round(test_lat, 5), "lon": round(test_lon, 5)},
                    "gap_km": round(desired_range_km, 1),
                    "strategy": "expansion",
                    "overlap_count": 0,
                })

    suggestions.sort(key=lambda s: (-1 if s["strategy"] == "densification" else 0, -s.get("overlap_count", 0)))
    return suggestions
