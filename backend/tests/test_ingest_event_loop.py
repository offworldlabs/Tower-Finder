"""Registering a node over HTTP must not stall the event loop.

`InterNodeAssociator.register_node` pre-computes an overlap grid against every
node already registered, which is seconds of CPU once a fleet is up. Called
straight from an `async def` handler that cost lands on the event loop, so it
does not merely slow the request that triggered it: every other request in
flight waits behind it. That is how a slow registration turned into a map with
nothing on it (86cb5hef4) — the ingest POSTs stopped returning inside the
client's 30 s timeout.

`services/tcp_handler.py` already dispatches registration to a dedicated
executor. These tests hold the HTTP paths to the same rule.
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
