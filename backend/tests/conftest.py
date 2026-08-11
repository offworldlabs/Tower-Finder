import asyncio
import os

import pytest

# Must be set before any backend module imports auth.py or routes/radar.py
os.environ.setdefault("RETINA_ENV", "test")
# Needed so the /api/radar/detections auth guard is active in tests.
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")


@pytest.fixture(autouse=True)
def _clean_db():
    """Truncate auth tables before each test.

    Uses asyncio.run() for the setup, then immediately restores a fresh event
    loop. asyncio.run() calls set_event_loop(None) on exit (Python 3.12), which
    would make asyncio.get_event_loop() raise RuntimeError in the subsequent
    async test — pytest-asyncio 0.23.x calls get_event_loop() directly before
    handing control to each async test function.
    """
    from sqlalchemy import delete

    from core.users import ClaimCode, Invite, NodeOwner, async_session_maker, create_db_and_tables

    async def _setup():
        await create_db_and_tables()
        async with async_session_maker() as session:
            await session.execute(delete(ClaimCode))
            await session.execute(delete(NodeOwner))
            await session.execute(delete(Invite))
            await session.commit()

    asyncio.run(_setup())
    asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture(autouse=True)
def _isolate_state_snapshot(tmp_path):
    """Point the snapshot at a per-test path so runs cannot pollute each other.

    restore_snapshot() runs in the app lifespan and save_snapshot() on its way
    out, so any test that builds a TestClient was reading — and rewriting —
    the developer's real backend/data/state_snapshot.json.  That was invisible
    while the snapshot held only trust/reputation data, but simulation_config
    is now persisted too, so one run's PUTs came back as the next run's "fresh
    backend" and broke the only-if-set assertions in test_sim_ingest.
    Function-scoped rather than session-scoped: a shared path just relocates
    the leak from the repo into tmp.
    """
    from services import state_snapshot

    orig = state_snapshot._SNAPSHOT_PATH
    state_snapshot._SNAPSHOT_PATH = str(tmp_path / "state_snapshot.json")
    yield
    state_snapshot._SNAPSHOT_PATH = orig


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset every module-level mutable store before each test.

    Each module owns a `_reset_for_tests()` beside the stores it declares, so
    the authoritative list lives with the code, not here.  The predecessor of
    this fixture reset 3 of ~50 stores; everything else leaked across tests —
    most dangerously frame_processor's wall-clock feed caches, which handed
    one test's detection_arcs/ground_truth to the next test verbatim.
    """
    from core import state
    from services import (
        aircraft_feed,
        alerting,
        feed_helpers,
        frame_processor,
        tcp_handler,
        track_gates,
    )
    from services.tasks import analytics_refresh, solver

    for mod in (state, frame_processor, aircraft_feed, track_gates,
                feed_helpers, solver, analytics_refresh, alerting,
                tcp_handler):
        mod._reset_for_tests()
    yield

