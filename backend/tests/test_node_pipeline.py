"""A v1 node has to look to the pipeline exactly like a blah2_bridge node.

The assertions here read the real registries rather than spying on calls:
what matters is that analytics and the associator end up knowing the node's
geometry, not that a particular method was invoked.

Fixtures are local to this module rather than in conftest. More than one
package is editing conftest for this phase, and nothing outside this file
needs a node with a configuration row attached.
"""

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from retina_analytics.constants import resolve_beam_azimuth_deg
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.users
from core import state
from core.nodes import Node, NodeConfig
from services.node_pipeline import (
    prime_pipeline,
    prime_pipeline_at_startup,
    register_with_pipeline,
    submit_frame,
)

NODE_ID = "ret1a2b3c4d"

# Geometry shared by the fixtures. beam_azimuth_deg is absent deliberately:
# null means broadside, and each fixture that cares supplies its own.
_GEOMETRY = {
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
}


async def _seed(session, node_id, *, version=1, status="active", with_config=True, **overrides):
    """One node, and unless asked otherwise one active configuration for it."""
    node = Node(
        node_id=node_id,
        node_ref=f"nde{node_id[3:]}00",
        board_model="raspberrypi5-4gb",
        status=status,
        active_config_version=version if with_config else 0,
    )
    session.add(node)
    if with_config:
        session.add(NodeConfig(node_id=node_id, version=version, **{**_GEOMETRY, **overrides}))
    await session.flush()
    return node


@pytest.fixture
async def node(node_session):
    return await _seed(node_session, NODE_ID)


@pytest.fixture
async def nodes(node_session):
    return [await _seed(node_session, f"ret{i}b2c3d4e5") for i in range(3)]


# ── Registration ─────────────────────────────────────────────────────────────


async def test_a_registered_node_appears_in_connected_nodes(node_session, node):
    await register_with_pipeline(node_session, node)

    entry = state.connected_nodes[NODE_ID]
    assert entry["status"] == "active"
    assert entry["is_synthetic"] is False
    assert entry["config"]["rx_lat"] == 51.42


async def test_registration_reaches_analytics_and_the_associator(node_session, node):
    await register_with_pipeline(node_session, node)

    assert state.node_analytics.detection_areas[NODE_ID].rx_lat == 51.42
    assert state.node_associator.node_geometries[NODE_ID].rx_lat == 51.42


async def test_the_pipeline_config_carries_the_defaults_blah2_bridge_supplies(node_session, node):
    await register_with_pipeline(node_session, node)

    config = state.connected_nodes[NODE_ID]["config"]
    assert config["doppler_min"] == -300
    assert config["doppler_max"] == 300
    assert config["min_doppler"] == 15


async def test_an_aimed_node_keeps_the_azimuth_it_was_configured_with(node_session):
    aimed = await _seed(node_session, NODE_ID, beam_azimuth_deg=200.0)

    await register_with_pipeline(node_session, aimed)

    assert state.node_associator.node_geometries[NODE_ID].beam_azimuth_deg == pytest.approx(200.0)


async def test_a_node_with_no_azimuth_is_registered_broadside_not_due_north(node_session, node):
    await register_with_pipeline(node_session, node)

    broadside = resolve_beam_azimuth_deg(
        {}, _GEOMETRY["rx_lat"], _GEOMETRY["rx_lon"], _GEOMETRY["tx_lat"], _GEOMETRY["tx_lon"]
    )
    azimuth = state.node_associator.node_geometries[NODE_ID].beam_azimuth_deg
    assert azimuth != 0.0
    assert azimuth == pytest.approx(broadside)


async def test_re_registering_picks_up_the_new_active_configuration(node_session, node):
    await register_with_pipeline(node_session, node)

    first = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == NODE_ID))).scalars().one()
    first.superseded_at = datetime.now(UTC)
    node_session.add(NodeConfig(node_id=NODE_ID, version=2, **{**_GEOMETRY, "rx_lat": 52.5}))
    await node_session.flush()

    await register_with_pipeline(node_session, node)

    assert state.connected_nodes[NODE_ID]["config"]["rx_lat"] == 52.5
    assert state.node_associator.node_geometries[NODE_ID].rx_lat == 52.5


async def test_a_node_with_no_active_configuration_cannot_be_registered(node_session):
    bare = await _seed(node_session, NODE_ID, with_config=False)

    with pytest.raises(ValueError):
        await register_with_pipeline(node_session, bare)


# ── Priming ──────────────────────────────────────────────────────────────────


async def test_every_node_is_primed_from_the_database_at_startup(node_session, nodes):
    state.connected_nodes.clear()

    primed = await prime_pipeline(node_session)

    assert primed == len(nodes)
    assert set(state.connected_nodes) == {n.node_id for n in nodes}


async def test_priming_skips_a_node_with_no_active_config(node_session, node):
    bare = await _seed(node_session, "ret9z8y7x6w", with_config=False)
    state.connected_nodes.clear()

    primed = await prime_pipeline(node_session)

    assert primed == 1
    assert bare.node_id not in state.connected_nodes


async def test_priming_skips_a_retired_node(node_session, node):
    retired = await _seed(node_session, "ret5t4r3e2d", status="retired")
    state.connected_nodes.clear()

    primed = await prime_pipeline(node_session)

    assert primed == 1
    assert retired.node_id not in state.connected_nodes


async def test_the_priming_summary_is_logged_where_it_can_actually_be_seen(node_session, node, caplog):
    """Under uvicorn the root logger sits at WARNING, so an INFO summary here is
    invisible in every deployed environment. This is the line that says whether
    the fleet came back after a deploy, so it has to clear that bar."""
    with caplog.at_level(logging.WARNING, logger="services.node_pipeline"):
        await prime_pipeline(node_session)

    assert any("primed 1 node(s)" in r.message for r in caplog.records)


async def test_startup_priming_loads_the_fleet_from_the_app_session(tmp_path, node_session, node, monkeypatch):
    # Committed, and read back through a second engine on the same file: that
    # is the shape of the real thing, where the priming session is opened by
    # the lifespan rather than handed in.
    await node_session.commit()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nodes.db'}")
    monkeypatch.setattr(core.users, "async_session_maker", async_sessionmaker(engine, expire_on_commit=False))
    state.connected_nodes.clear()

    try:
        assert await prime_pipeline_at_startup() == 1
        assert NODE_ID in state.connected_nodes
    finally:
        await engine.dispose()


async def test_startup_priming_survives_a_database_failure(monkeypatch):
    """A nodes table that is not there yet must not take the whole API down.

    blah2_bridge is this phase's rollback and runs in the same process, so a
    priming failure that killed startup would take the fallback with it.
    """

    def _no_such_table():
        raise OperationalError("SELECT nodes.node_id FROM nodes", {}, Exception("no such table: nodes"))

    monkeypatch.setattr(core.users, "async_session_maker", _no_such_table)

    assert await prime_pipeline_at_startup() == 0


# ── Frames ───────────────────────────────────────────────────────────────────


async def test_a_frame_reaches_the_queue_under_its_node_id():
    assert submit_frame(NODE_ID, {"timestamp": 1753900000123, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}) is True

    node_id, frame = state.frame_queue.get_nowait()
    assert node_id == NODE_ID
    assert frame["_node_id"] == NODE_ID


async def test_a_full_queue_drops_the_frame_and_bumps_the_counter(monkeypatch):
    monkeypatch.setattr(state, "frame_queue", asyncio.Queue(maxsize=1))
    state.frame_queue.put_nowait(("filler", {}))
    before = state.frames_dropped

    assert submit_frame(NODE_ID, {"timestamp": 1, "delay": [], "doppler": [], "snr": []}) is False

    assert state.frames_dropped == before + 1
