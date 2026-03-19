"""Node analytics package — trust, reputation, coverage, cross-node analysis."""

from analytics.manager import NodeAnalyticsManager
from analytics.trust import AdsReportEntry, TrustScoreState
from analytics.detection_area import DetectionAreaState
from analytics.metrics import NodeMetrics
from analytics.reputation import NodeReputation
from analytics.coverage import HistoricalCoverageMap, CoverageMapEntry
from analytics.cross_node import compute_delay_bin_overlap, coverage_suggestion
from analytics.constants import (
    C_KM_US, R_EARTH, YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM, haversine_km,
)

__all__ = [
    "NodeAnalyticsManager",
    "AdsReportEntry",
    "TrustScoreState",
    "DetectionAreaState",
    "NodeMetrics",
    "NodeReputation",
    "HistoricalCoverageMap",
    "CoverageMapEntry",
    "compute_delay_bin_overlap",
    "coverage_suggestion",
    "C_KM_US",
    "R_EARTH",
    "YAGI_BEAM_WIDTH_DEG",
    "YAGI_MAX_RANGE_KM",
    "haversine_km",
]