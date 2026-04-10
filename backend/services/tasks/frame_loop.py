"""Frame processor loop — drains state.frame_queue via thread-pool."""

import asyncio
import logging
import time

from core import state
from services.frame_processor import process_one_frame


async def frame_processor_loop(default_pipeline):
    """Process queued detection frames sequentially in a thread pool."""
    loop = asyncio.get_event_loop()
    while True:
        node_id, frame = await state.frame_queue.get()
        try:
            await loop.run_in_executor(
                None, process_one_frame, node_id, frame, default_pipeline,
            )
            state.aircraft_dirty = True
            state.frames_processed += 1
            state.task_last_success["frame_processor"] = time.time()
        except Exception:
            state.task_error_counts["frame_processor"] += 1
            logging.exception("Frame processing failed")
        finally:
            state.frame_queue.task_done()
        await asyncio.sleep(0)
