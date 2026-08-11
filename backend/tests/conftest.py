import asyncio
import os

import pytest
from sqlalchemy import event

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


@pytest.fixture
async def node_session(tmp_path):
    """An AsyncSession against a per-test database with migrations applied.

    A separate engine from the suite's shared one, so each test gets a clean
    file and the node tests exercise the migrated schema rather than the one
    create_all builds. Nothing is shared between tests, so ordering cannot
    matter.
    """
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    backend = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "nodes.db"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend,
        env=os.environ | {"RETINA_ENV": "test", "RETINA_DB_PATH": str(db_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        # test_a_config_for_an_unknown_node_is_rejected depends on this. SQLite
        # does not enforce foreign keys unless asked, per connection.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()

