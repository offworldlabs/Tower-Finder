"""Shared task staleness registry.

Single source of truth for expected task intervals — and, since two routers had
grown byte-identical copies of the check, for the staleness rule itself.
"""

import time

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
    "storage_refresh": 720,  # expected every 300 s; alert if >2× late
    "track_archive_flush": 180,  # flush every 60 s; alert if >3× late
    "users_db_backup": 86400 * 2,  # daily; alert if it hasn't run in 2 days
    # The blah2 bridge registers one key per configured live node at startup
    # (see services/blah2_bridge.load_nodes) — its node list is config-driven,
    # so those keys cannot be enumerated here.
}


def register_task(name: str, expected_interval_s: int) -> None:
    """Add a dynamically-discovered task to the staleness registry.

    For tasks whose number is not known until config is read. Idempotent, so
    re-reading a config file does not disturb an already-registered task.
    """
    TASK_EXPECTED_INTERVAL_S.setdefault(name, expected_interval_s)


def get_stale_tasks() -> list[str]:
    """Tasks that have not reported success within 2x their expected interval.

    A task with no recorded success has not started yet and is not stale.
    """
    from core import state

    now = time.time()
    return [
        name
        for name, expected_s in TASK_EXPECTED_INTERVAL_S.items()
        if (last := state.task_last_success.get(name)) is not None and (now - last) > expected_s * 2
    ]
