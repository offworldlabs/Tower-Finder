"""Background task sub-modules split from services/background.py.

All public names previously exported by background.py are re-exported here
so ``from services.tasks import frame_processor_loop`` works.
"""

from services.tasks.aircraft_flush import aircraft_flush_task
from services.tasks.analytics_refresh import analytics_refresh_task
from services.tasks.frame_loop import frame_processor_loop
from services.tasks.periodic import (
    adsb_truth_fetcher,
    archive_flush_task,
    archive_lifecycle_task,
    prune_synthetic_nodes,
    reputation_evaluator,
)
from services.tasks.solver import start_solver_workers
from services.tasks.storage_refresh import storage_refresh_task
from services.tasks.track_archive import track_flush_task

__all__ = [
    "analytics_refresh_task",
    "start_solver_workers",
    "frame_processor_loop",
    "aircraft_flush_task",
    "storage_refresh_task",
    "archive_flush_task",
    "archive_lifecycle_task",
    "reputation_evaluator",
    "prune_synthetic_nodes",
    "adsb_truth_fetcher",
    "track_flush_task",
]
