import os

import pytest

# Must be set before any backend module imports auth.py or routes/radar.py
os.environ.setdefault("RETINA_ENV", "test")
# Needed so the /api/radar/detections auth guard is active in tests.
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")


@pytest.fixture(autouse=True)
def _isolate_task_timestamps():
    """Clear task_last_success before/after every test.

    frame_processor_loop (started by TestClient lifespan) sets
    task_last_success["frame_processor"] whenever it processes a frame.
    Without this fixture, that timestamp leaks into subsequent tests and
    causes /api/health to report 'stale_task:frame_processor'.
    """
    from core import state

    state.task_last_success.clear()
    yield
    state.task_last_success.clear()

