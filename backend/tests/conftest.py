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
# No solver process pool under pytest: tests monkeypatch the compute functions,
# and an unpicklable closure cannot be shipped to a pool child.  See the
# _POOL_ENABLED comment in services/tasks/solver.py.
os.environ.setdefault("SOLVER_POOL", "0")
# Needed so the /api/radar/detections auth guard is active in tests.
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")
# The suite has no OAuth provider to log in against, so the route tests reach the
# admin endpoints through core.users' anonymous-admin bypass. That bypass is an
# explicit opt-in and no longer follows from RETINA_ENV=test, so ask for it here.
# Set before core.users is imported: AUTH_BYPASS is derived once, at import.
os.environ.setdefault("AUTH_ALLOW_ANONYMOUS_ADMIN", "1")
# The arc-ux sim-ingest tests exercise routes main.py now mounts only behind
# this flag (the compose overlays all set it); the mount-gate test itself spawns
# child interpreters with an explicit env, so this parent-process default does
# not reach it.
os.environ.setdefault("SYNTHETIC_FLEET_ENABLED", "1")
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
        known_claiming,
        node_bias,
        tcp_handler,
        track_gates,
    )
    from services.tasks import analytics_refresh, known_lane, solver

    for mod in (
        state,
        frame_processor,
        aircraft_feed,
        track_gates,
        feed_helpers,
        solver,
        known_lane,
        analytics_refresh,
        alerting,
        tcp_handler,
        known_claiming,
        node_bias,
    ):
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
        #
        # This deliberately stops short of core.users.engine's full pragma set.
        # WAL and synchronous=NORMAL exist there to survive a hard kill mid-write
        # and to let readers proceed alongside a writer; this fixture's database
        # is a fresh per-test temporary file with one connection and no
        # concurrent access, deleted with the tmp_path at test end, so neither
        # property has anything to buy. busy_timeout exists there to tolerate
        # contention from other processes, which a private per-test file never
        # has. Only the foreign-key enforcement this fixture exists to test is
        # worth reproducing.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def seeded_node(node_session):
    """One active node, flushed but not committed, for tests that need a token's
    foreign key to hold.

    Flushed rather than committed so a test can still exercise the rollback path
    the flush-not-commit contract in services/node_auth.py rests on.
    """
    from core.nodes import Node

    node = Node(node_id="ret1a2b3c4d", node_ref="nde1a2b3c4d00", board_model="raspberrypi5-4gb")
    node_session.add(node)
    await node_session.flush()
    return node


# ── v1 node API fixtures ─────────────────────────────────────────────────────
#
# Shared by the registration, config, detection and heartbeat suites, which are
# written against these signatures in parallel, so a name or an arity here is a
# contract rather than a local choice. tests/test_node_fixtures.py exercises
# each one, so a drift fails there rather than across four suites at once.
#
# Everything below imports backend modules inside the function body, per the
# module preamble: those environment variables have to be in place before the
# first backend import happens.

_NODE_ID = "ret1a2b3c4d"

# The column values a seeded configuration carries, transcribed from
# tests/test_node_pipeline.py so both agree on the geometry the pipeline
# assertions read. beam_azimuth_deg is present and null: null is broadside,
# and validate_config requires the key rather than defaulting it.
_NODE_CONFIG_VALUES = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": None,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

# Enough of a PEM for lookup_device to carry through to auth_set_pubkey. The
# fingerprint it derives is incidental here; tests/test_mender_lookup.py owns
# pinning that against a real key.
_MENDER_PUBKEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"


def _mender_device(node_id: str, status: str, auth_sets: list) -> dict:
    """One entry of the management API's device list.

    Same shape as tests/test_mender_lookup.py's `_device`, which is where the
    wire format is pinned.
    """
    return {"id": "b7e2", "identity_data": {"node_id": node_id}, "status": status, "auth_sets": auth_sets}


def _serve_mender_devices(monkeypatch, devices: list) -> None:
    """Answer every Mender request with `devices`.

    A list rather than a single device because lookup_device fetches the whole
    device list and filters client side on identity_data.node_id, which is also
    why the fixtures below need the node_id they are meant to describe.
    """
    import httpx

    from services import mender

    async def _handler(_request: "httpx.Request") -> "httpx.Response":
        return httpx.Response(200, json=devices)

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(_handler))


@pytest.fixture
def node_client(node_session):
    """A TestClient whose requests read and write the per-test node database.

    Constructed bare rather than as `with TestClient(...)`. The context-manager
    form runs the app lifespan, which starts the frame processor workers that
    drain state.frame_queue; those would race any detection test asserting on
    what a handler queued.

    The get_async_session override is the point of the fixture: without it a
    request would reach core.users' own session maker and the main database,
    while the test seeds and asserts against node_session's migrated one.

    One constraint that constrains the handlers too: a bare TestClient runs each
    request on its own portal, thread and event loop, so node_session is driven
    from a different loop than the test body. That holds on the pinned aiosqlite
    and SQLAlchemy, and a test that awaits the same session from both sides is
    resting on it rather than on anything guaranteed. Assert through a fresh
    query after the request rather than on objects the handler left attached.
    """
    from fastapi.testclient import TestClient

    import main
    from core.users import get_async_session

    async def _override_session():
        yield node_session

    saved = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_async_session] = _override_session
    client = TestClient(main.app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()
        # Mutated in place rather than rebound: the app is a module-level
        # singleton shared with every other suite, so what matters is that the
        # dict it holds ends the test as it started.
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(saved)


@pytest.fixture
def accepted_in_mender(monkeypatch):
    """accepted_in_mender(node_id): Mender knows the node and has accepted it."""

    def _accept(node_id: str) -> None:
        auth_set = {"id": "as-1", "status": "accepted", "pubkey": _MENDER_PUBKEY}
        _serve_mender_devices(monkeypatch, [_mender_device(node_id, "accepted", [auth_set])])

    return _accept


@pytest.fixture
def pending_in_mender(monkeypatch):
    """pending_in_mender(node_id): the node has enrolled but nothing has accepted it.

    Distinct from unknown_in_mender by design: the two are the same 403 on the
    wire but different operator actions, and during bring-up most of the fleet
    sits here.
    """

    def _pend(node_id: str) -> None:
        _serve_mender_devices(monkeypatch, [_mender_device(node_id, "pending", [])])

    return _pend


@pytest.fixture
def unknown_in_mender(monkeypatch):
    """unknown_in_mender(node_id): Mender answers, and has never heard of the node.

    A device with another identity rather than an empty list, so the fixture
    also stands on the client-side identity filter rather than on there being
    nothing to filter. The decoy identity is derived from the requested one so
    that no caller can collide with it by choosing a particular node_id.
    """

    def _unknown(node_id: str) -> None:
        decoy = "retffffffff" if node_id != "retffffffff" else "ret00000000"
        _serve_mender_devices(monkeypatch, [_mender_device(decoy, "accepted", [])])

    return _unknown


@pytest.fixture
def mender_down(monkeypatch):
    """mender_down(): Mender cannot be asked at all, so lookup_device raises.

    No node_id argument, unlike the three above: nothing gets as far as the
    identity filter.
    """

    def _down() -> None:
        import httpx

        from services import mender

        async def _handler(_request: "httpx.Request") -> "httpx.Response":
            raise httpx.ConnectTimeout("no route")

        monkeypatch.setattr(mender, "_transport", httpx.MockTransport(_handler))

    return _down


@pytest.fixture
def alerts(monkeypatch):
    """Every send_alert call, as (alert_type, message, meta) tuples in order.

    Only a handler that resolves send_alert at call time is captured, which
    means a function-local `from services.alerting import send_alert` the way
    services/node_pipeline.py does it. A module-level import in a handler binds
    the real function before this patch lands, so the call would fire for real
    and the list would stay silently empty.
    """
    from services import alerting

    captured: list[tuple[str, str, dict | None]] = []

    def _capture(alert_type: str, message: str, meta: dict | None = None) -> None:
        captured.append((alert_type, message, meta))

    monkeypatch.setattr(alerting, "send_alert", _capture)
    return captured


@pytest.fixture
async def registered_node(node_session):
    """(token, node_id) for one active node at config version 1, seeded directly.

    Deliberately does not go through POST /v1/nodes/register. The config,
    detection and heartbeat suites are written before the registration handler
    exists, and a bug in registration should fail the registration tests rather
    than all four suites at once.

    The configuration is passed through services.node_config.validate_config so
    the fixture cannot drift into a config the real validator would reject.
    register_with_pipeline puts the node into state.connected_nodes, without
    which the heartbeat handler raises KeyError; see prime_pipeline's docstring
    for why nothing else would put it back.

    Committed rather than flushed, unlike seeded_node above: a handler that
    rolls back its own transaction would otherwise take the seed with it.
    """
    from core.nodes import Node, NodeConfig
    from services import node_auth, node_config, node_pipeline

    config = node_config.validate_config(dict(_NODE_CONFIG_VALUES))
    node = Node(
        node_id=_NODE_ID,
        node_ref=node_auth.mint_node_ref(),
        board_model="raspberrypi5-4gb",
        status="active",
        active_config_version=1,
    )
    node_session.add(node)
    node_session.add(NodeConfig(node_id=_NODE_ID, version=1, **config))
    await node_session.flush()
    token = await node_auth.mint_token(node_session, _NODE_ID)
    await node_pipeline.register_with_pipeline(node_session, node)
    await node_session.commit()
    return token, _NODE_ID
