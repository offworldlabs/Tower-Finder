"""Shared task staleness registry.

Single source of truth for expected task intervals.
Import from here in any module that needs to detect stale tasks.
"""

# Task name → expected success interval in seconds.
# A task is considered stale if it hasn't reported success within 2× this value.
TASK_EXPECTED_INTERVAL_S: dict[str, int] = {
    "frame_processor": 10,
    "analytics_refresh": 60,
    "aircraft_flush": 5,
    "archive_flush": 120,
    "archive_lifecycle": 3600,
    "reputation_evaluator": 120,
    "prune_synthetic_nodes": 21600,  # Every 6 hours
    "adsb_truth_fetcher": 300,
    "solver": 120,
    "storage_refresh": 720,   # expected every 300 s; alert if >2× late
    "blah2_bridge": 10,       # polls every 1 s; stale if >10 s
}
