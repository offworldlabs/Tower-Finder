import asyncio
import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import event

from tests.migration_helpers import _alembic

# Must be set before any backend module imports auth.py or routes/radar.py
os.environ.setdefault("RETINA_ENV", "test")
# Needed so the /api/radar/detections auth guard is active in tests.
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")
# The suite truncates tables and creates schema, so it must never be pointed at
# a real database. A hard assignment, not setdefault: the README tells readers
# to export RETINA_DB_PATH to try a migration against a scratch file, and a
# setdefault would leave that value in place for a suite run in the same shell,
# which would then truncate whatever database the developer just pointed at.
# The pid makes the name unique per run: tempfile.gettempdir() resolves to the
# same per-user directory for every worktree of this repo on the machine, so a
# fixed filename is shared by concurrent suite runs. The autouse _clean_db
# fixture then DELETEs from another run's tables mid-test, and SQLite's WAL
# mode adds locking contention on top, producing nondeterministic failures.
_TEST_DB_PATH = Path(tempfile.gettempdir()) / f"retina-test-users-{os.getpid()}.db"
os.environ["RETINA_DB_PATH"] = str(_TEST_DB_PATH)


def _cleanup_test_db() -> None:
    # Safe to unlink unconditionally: the path is generated above from the
    # tempdir and this process's pid, so it can never alias a developer's real
    # database. WAL mode also leaves -wal/-shm siblings, and a crashed run can
    # leave a -journal; missing_ok covers a suite that never created the file.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{_TEST_DB_PATH}{suffix}").unlink(missing_ok=True)


atexit.register(_cleanup_test_db)
# The suite builds its schema with create_all rather than a migration run per
# session. tests/test_migrations.py asserts the two agree.
os.environ.setdefault("RETINA_SCHEMA_SOURCE", "create_all")


@pytest.fixture(autouse=True)
def _clean_db():
    """Truncate auth and node tables before each test.

    Uses asyncio.run() for the setup, then immediately restores a fresh event
    loop. asyncio.run() calls set_event_loop(None) on exit (Python 3.12), which
    would make asyncio.get_event_loop() raise RuntimeError in the subsequent
    async test — pytest-asyncio 0.23.x calls get_event_loop() directly before
    handing control to each async test function.
    """
    from sqlalchemy import delete

    from core.nodes import Node, NodeConfig, NodeToken
    from core.users import ClaimCode, Invite, NodeOwner, async_session_maker, create_db_and_tables

    async def _setup():
        await create_db_and_tables()
        async with async_session_maker() as session:
            await session.execute(delete(ClaimCode))
            await session.execute(delete(NodeOwner))
            await session.execute(delete(Invite))
            # Children before parent: node_configs and node_tokens both carry a
            # foreign key to nodes, and PRAGMA foreign_keys=ON (core/users.py)
            # enforces it on every connection.
            await session.execute(delete(NodeConfig))
            await session.execute(delete(NodeToken))
            await session.execute(delete(Node))
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


@pytest.fixture(scope="session")
def _node_schema_template(tmp_path_factory):
    """Migrate once into a template database that node_session copies per test.

    A full `alembic upgrade head` subprocess per test made suite time grow
    linearly as node-model tests were added. Session scope makes pytest build
    this exactly once regardless of test order or which test asks for it
    first, and a failure here surfaces as this fixture's own error rather than
    a confusing per-test one.
    """
    db_path = tmp_path_factory.mktemp("node_schema_template") / "nodes.db"
    result = _alembic("upgrade", "head", db_path=db_path)
    assert result.returncode == 0, result.stderr
    return db_path


@pytest.fixture
async def node_session(tmp_path, _node_schema_template):
    """An AsyncSession against a per-test database with migrations applied.

    Copied (shutil.copyfile) from the session-scoped template built by
    _node_schema_template rather than migrated fresh, so each test still gets
    its own file and the node tests still exercise the migrated schema rather
    than the one create_all builds, without paying for a fresh subprocess per
    test. Nothing is shared between tests, so ordering cannot matter.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_path = tmp_path / "nodes.db"
    shutil.copyfile(_node_schema_template, db_path)

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

