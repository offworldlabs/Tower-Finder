import os

import pytest

# Must be set before any backend module imports auth.py or routes/radar.py
os.environ.setdefault("RETINA_ENV", "test")
# Needed so the /api/radar/detections auth guard is active in tests.
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")


@pytest.fixture(autouse=True)
async def _clean_db():
    """Truncate auth tables before each test.

    Async so pytest-asyncio manages the event loop — avoids the asyncio.run()
    pattern that closes the event loop and breaks subsequent async tests.
    Runs for the whole suite (cheap SQLite no-op for non-auth tests) so
    test_tcp_claim and test_auth_routes need no local duplicate.
    """
    from sqlalchemy import delete

    from core.users import ClaimCode, Invite, NodeOwner, async_session_maker, create_db_and_tables

    await create_db_and_tables()
    async with async_session_maker() as session:
        await session.execute(delete(ClaimCode))
        await session.execute(delete(NodeOwner))
        await session.execute(delete(Invite))
        await session.commit()
    yield


@pytest.fixture(autouse=True)
def _isolate_task_timestamps():
    """Clear shared state before/after every test.

    Several state fields accumulate across tests and can corrupt health checks
    in later tests if not reset:
    - task_last_success: set by background workers, causes stale_task health issues
    - accuracy_samples: grows via _record_accuracy_sample during geolocation;
      >20 samples with mean_km>10 triggers solver_accuracy_degraded in /api/health
    - latest_accuracy_bytes: cached result from _refresh_accuracy_stats; must be
      reset alongside the sample buffer so health checks see a clean slate
    """
    from core import state

    state.task_last_success.clear()
    state.accuracy_samples.clear()
    state.latest_accuracy_bytes = b"{}"
    yield
    state.task_last_success.clear()
    state.accuracy_samples.clear()
    state.latest_accuracy_bytes = b"{}"

