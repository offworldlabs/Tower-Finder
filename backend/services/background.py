"""Background async tasks — thin re-export shim.

The actual implementations live in ``services.tasks.*`` sub-modules.
This file keeps the original import surface so ``main.py`` and tests
continue to work without changes::

    from services.background import frame_processor_loop, start_solver_workers
"""

from services.tasks import (  # noqa: F401
    analytics_refresh_task,
    start_solver_workers,
    frame_processor_loop,
    aircraft_flush_task,
    archive_flush_task,
    reputation_evaluator,
    adsb_truth_fetcher,
)
