"""Configuration versioning, which both registration and PUT /config answer from.

The endpoint tests reach these paths through a handler, which is the right level
for the wire behaviour and the wrong one for the null comparison: a store that
minted a version per resend would still look correct from outside for the first
few frames, and would then have every node in the fleet a version ahead of the
one it holds.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from core.nodes import Node, NodeConfig
from services.node_auth import mint_node_ref
from services.node_config import validate_config
from services.node_config_store import upsert_config

NODE_ID = "ret1a2b3c4d"

CONFIG = {
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


@pytest.fixture
async def node(node_session):
    """A node row to hang configurations off: node_configs.node_id is a foreign
    key and the fixture database has PRAGMA foreign_keys=ON."""
    row = Node(node_id=NODE_ID, node_ref=mint_node_ref(), board_model="raspberrypi5-4gb")
    node_session.add(row)
    await node_session.flush()
    return row


async def _versions(session) -> list[tuple[int, bool]]:
    rows = (
        (await session.execute(select(NodeConfig).where(NodeConfig.node_id == NODE_ID).order_by(NodeConfig.version)))
        .scalars()
        .all()
    )
    return [(row.version, row.superseded_at is not None) for row in rows]


async def test_the_first_configuration_is_version_one(node_session, node):
    assert await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG))) == 1
    assert await _versions(node_session) == [(1, False)]


async def test_an_unchanged_configuration_keeps_its_version(node_session, node):
    """The null trap: `beam_azimuth_deg` is null for every node that is not
    aimed, so a comparison done in SQL would never match and every resend would
    mint a version."""
    first = await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))

    again = await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))

    assert (first, again) == (1, 1)
    assert await _versions(node_session) == [(1, False)]


async def test_a_changed_configuration_supersedes_the_previous_version(node_session, node):
    await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))

    version = await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG, max_range_km=60.0)))

    assert version == 2
    # The old row is kept and marked rather than updated: a detection frame
    # arrives stamped with the version it was computed under, so the geometry
    # behind an archived detection has to stay readable.
    assert await _versions(node_session) == [(1, True), (2, False)]


async def test_an_azimuth_appearing_is_a_new_version(node_session, node):
    """Null to a value is a real change: broadside to aimed due north."""
    await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))

    version = await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG, beam_azimuth_deg=0.0)))

    assert version == 2


async def test_a_version_is_never_reused_once_the_active_row_is_superseded(node_session, node):
    """Counting from the active row rather than from the highest one ever held
    restarts at 1 for a node whose only configuration has been superseded, and
    the insert then collides with uq_node_configs_node_version. Nothing
    supersedes without inserting today, which is exactly why it would be found
    by a 500 in production rather than here."""
    await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))
    row = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == NODE_ID))).scalars().one()
    row.superseded_at = datetime.now(UTC)
    await node_session.flush()

    version = await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))

    assert version == 2
    assert await _versions(node_session) == [(1, True), (2, False)]


async def test_nothing_is_committed(node_session, node):
    """The route owns the transaction, so a rollback takes the version with it."""
    await upsert_config(node_session, NODE_ID, validate_config(dict(CONFIG)))

    await node_session.rollback()

    assert await _versions(node_session) == []
