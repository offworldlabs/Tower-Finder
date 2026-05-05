"""Background async tasks — thin re-export shim.

The actual implementations live in ``services.tasks.*`` sub-modules.
This file keeps the original import surface so ``main.py`` and tests
continue to work without changes::

    from services.background import frame_processor_loop, start_solver_workers
"""

from services.tasks import (  # noqa: F401
    adsb_truth_fetcher,
    aircraft_flush_task,
    analytics_refresh_task,
    archive_flush_task,
    archive_lifecycle_task,
    frame_processor_loop,
    prune_synthetic_nodes,
    reputation_evaluator,
    start_solver_workers,
    storage_refresh_task,
    track_flush_task,
)
