"""Registering a node must not stall the event loop, whichever path it arrives by.

`InterNodeAssociator.register_node` pre-computes an overlap grid against every
node already registered, which is seconds of CPU once a fleet is up. Called
straight from an `async def` handler that cost lands on the event loop, so it
does not merely slow the request that triggered it: every other request in
flight waits behind it. That is how a slow registration turned into a map with
nothing on it (86cb5hef4) — the ingest POSTs stopped returning inside the
client's 30 s timeout.

`services/tcp_handler.py` already dispatches registration to a dedicated
executor. These tests hold every other path to the same rule: both HTTP ingest
endpoints, the v1 pipeline path in `services/node_pipeline.py`, and the blah2
bridge.
"""

import asyncio
import time

import httpx
import pytest

from main import app

VALID_KEY = "test-key-abc123"
HEADERS_OK = {"X-API-Key": VALID_KEY}

# Long enough to dwarf scheduling noise, short enough to keep the suite quick.
BLOCK_S = 1.0
# A request that waits on the blocked registration takes ~BLOCK_S; one that does
# not is sub-millisecond. Anything under half the block is unambiguously the latter.
RESPONSIVE_S = BLOCK_S / 2


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def slow_registration(monkeypatch):
    """Stand in for the real overlap-grid build with a plain, blocking sleep.

    `time.sleep` holds the event loop exactly as the grid computation does, so
    it discriminates the thing under test: on the loop the whole app stops, in
    an executor thread nothing else notices.
    """
    from core import state

    def slow(node_id, config):
        time.sleep(BLOCK_S)

    monkeypatch.setattr(state.node_associator, "register_node", slow)
    monkeypatch.setattr(state.node_analytics, "register_node", slow)


@pytest.fixture(autouse=True)
def _clean_nodes():
    from core import state

    yield
    for node_id in list(state.connected_nodes):
        if node_id.startswith("test-"):
            state.connected_nodes.pop(node_id, None)
            # The associator and analytics keep their own stores, so dropping
            # only connected_nodes leaks every test node into the rest of the
            # session — and each leaked node is one more pair the next
            # registration has to grid.
            state.node_associator.unregister_node(node_id)
            state.node_analytics.retire_node(node_id)


async def _finished_at(coro, t0):
    response = await coro
    return response, time.perf_counter() - t0


async def _time_a_concurrent_get(client, registering) -> float:
    """Seconds until an unrelated GET completes, racing `registering`.

    Both are queued before either runs, and the registering POST is queued
    first, so it reaches the handler first. Measured from a shared t0 rather
    than from just before the GET: a blocked loop delays the *scheduling* of
    the GET, not its execution, so timing the await alone would sit entirely
    inside the stall and report zero.
    """
    t0 = time.perf_counter()
    post = asyncio.create_task(_finished_at(registering, t0))
    get = asyncio.create_task(_finished_at(client.get("/api/radar/data/aircraft.json"), t0))
    _, (response, elapsed) = await asyncio.gather(post, get)
    assert response.status_code == 200
    return elapsed


class TestIngestDoesNotBlockTheLoop:
    async def test_single_node_registration_leaves_the_app_responsive(self, client, slow_registration):
        elapsed = await _time_a_concurrent_get(
            client,
            client.post(
                "/api/radar/detections",
                headers=HEADERS_OK,
                json={"node_id": "test-slow-single"},
            ),
        )
        assert elapsed < RESPONSIVE_S

    async def test_bulk_registration_leaves_the_app_responsive(self, client, slow_registration):
        elapsed = await _time_a_concurrent_get(
            client,
            client.post(
                "/api/radar/detections/bulk",
                headers=HEADERS_OK,
                json={"nodes": [{"node_id": "test-slow-bulk", "frames": []}]},
            ),
        )
        assert elapsed < RESPONSIVE_S


class TestRegistrationStillHappens:
    """Moving the work must not lose it."""

    async def test_single_node_is_registered(self, client):
        from core import state

        r = await client.post(
            "/api/radar/detections",
            headers=HEADERS_OK,
            json={"node_id": "test-registered-single"},
        )

        assert r.status_code == 200
        assert "test-registered-single" in state.connected_nodes

    async def test_bulk_node_is_registered(self, client):
        from core import state

        r = await client.post(
            "/api/radar/detections/bulk",
            headers=HEADERS_OK,
            json={"nodes": [{"node_id": "test-registered-bulk", "frames": []}]},
        )

        assert r.status_code == 200
        assert "test-registered-bulk" in state.connected_nodes

    async def test_the_registration_reaches_the_associator(self, client, monkeypatch):
        """The executor hop must still pass node id and config through."""
        from core import state

        seen = []
        monkeypatch.setattr(state.node_associator, "register_node", lambda nid, cfg: seen.append((nid, cfg)))
        monkeypatch.setattr(state.node_analytics, "register_node", lambda nid, cfg: None)

        await client.post(
            "/api/radar/detections",
            headers=HEADERS_OK,
            json={"node_id": "test-passthrough"},
        )

        assert [nid for nid, _ in seen] == ["test-passthrough"]


# ── The paths that do not go through an HTTP handler ──────────────────────────
#
# The two below reach the same registration from elsewhere: `node_pipeline` on
# the v1 path (POST /v1/nodes/register in-request, and once per node at
# startup), and `blah2_bridge` for the real receivers. Neither can be driven
# through the ASGI client, so they are timed directly.

# The v1 node's stored configuration. Of these only beam_width_deg is nullable,
# and it is given a value here rather than the null a real node carries: a null
# width reaches the associator's geometry and raises, which is 86cb5dh4d and is
# being fixed on its own branch. These tests are about where registration runs,
# not what it is handed, so they take the config shape that already works.
_V1_CONFIG = {
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
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}
_V1_NODE_ID = "test-loop-v1"

# Coarse enough that the heartbeat is not itself the load, fine enough that a
# stall of RESPONSIVE_S cannot hide between two ticks.
_TICK_S = 0.01


async def _longest_stall_during(coro) -> float:
    """The longest the event loop went unserviced while `coro` ran.

    A bystander that merely waits its turn is not enough here: `register_with_pipeline`
    awaits a database read before it reaches the registration, and that await hands
    the bystander its turn early whether or not the registration blocks afterwards.
    A heartbeat ticking throughout measures the stall wherever in the coroutine
    it falls.

    The heartbeat is given its first turn before `coro` starts. Without that it
    would never run at all against a path with no suspension point of its own
    (`_register_node` is one), leaving no gaps to report and the stall measuring
    as zero — which is the one answer that must not come back by default.
    """
    gaps: list[float] = []
    running = True

    async def heartbeat():
        last = time.perf_counter()
        while running:
            await asyncio.sleep(_TICK_S)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    try:
        await coro
    finally:
        running = False
        await ticker
    assert gaps, "the heartbeat never ran, so nothing was measured"
    return max(gaps)


@pytest.fixture
async def v1_node(node_session):
    from core.nodes import Node, NodeConfig

    node = Node(
        node_id=_V1_NODE_ID,
        node_ref="ndetestloopv100",
        board_model="raspberrypi5-4gb",
        status="active",
        active_config_version=1,
    )
    node_session.add(node)
    node_session.add(NodeConfig(node_id=_V1_NODE_ID, version=1, **_V1_CONFIG))
    await node_session.flush()
    yield node
    # Teardown is the autouse _clean_nodes above: it clears every `test-` node
    # from all three stores, and _V1_NODE_ID is one. Repeating it here would be
    # a second cleanup path to keep in step with the first.


class TestTheNonHttpPathsDoNotBlockTheLoop:
    async def test_v1_pipeline_registration_leaves_the_loop_free(self, node_session, v1_node, slow_registration):
        from services.node_pipeline import register_with_pipeline

        stall = await _longest_stall_during(register_with_pipeline(node_session, v1_node))

        assert stall < RESPONSIVE_S

    async def test_blah2_bridge_registration_leaves_the_loop_free(self, slow_registration):
        from services.blah2_bridge import _build_node, _register_node

        node = _build_node(
            {
                "node_id": "test-loop-blah2",
                "detection_url": "https://example.test/api/detection",
                "rx_lat": 33.9,
                "rx_lon": -84.6,
                "tx_lat": 33.8,
                "tx_lon": -84.1,
                "fc_hz": 177_000_000,
            }
        )

        stall = await _longest_stall_during(_register_node(node))

        assert stall < RESPONSIVE_S


class TestTheNonHttpRegistrationsStillHappen:
    async def test_the_v1_node_reaches_the_registries(self, node_session, v1_node):
        from core import state
        from services.node_pipeline import register_with_pipeline

        await register_with_pipeline(node_session, v1_node)

        assert state.connected_nodes[_V1_NODE_ID]["peer"] == "v1"
        assert state.node_associator.node_geometries[_V1_NODE_ID].rx_lat == _V1_CONFIG["rx_lat"]
        assert state.node_analytics.detection_areas[_V1_NODE_ID].rx_lat == _V1_CONFIG["rx_lat"]

    async def test_the_blah2_node_reaches_the_registries(self):
        from core import state
        from services.blah2_bridge import _build_node, _register_node

        node = _build_node(
            {
                "node_id": "test-registered-blah2",
                "detection_url": "https://example.test/api/detection",
                "rx_lat": 33.9,
                "rx_lon": -84.6,
                "tx_lat": 33.8,
                "tx_lon": -84.1,
                "fc_hz": 177_000_000,
            }
        )

        await _register_node(node)

        assert state.connected_nodes["test-registered-blah2"]["peer"] == node.peer
        assert state.node_associator.node_geometries["test-registered-blah2"].rx_lat == 33.9
