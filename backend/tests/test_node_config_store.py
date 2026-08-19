"""Configuration versioning, which both registration and PUT /config answer from.

The endpoint tests reach these paths through a handler, which is the right level
for the wire behaviour and the wrong one for the null comparison: a store that
minted a version per resend would still look correct from outside for the first
few frames, and would then have every node in the fleet a version ahead of the
one it holds.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select

from core.nodes import Node, NodeConfig
from services.node_auth import mint_node_ref
from services.node_config import validate_config
from services.node_config_store import _CONFIG_FIELDS, upsert_config

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

# One different value per field, each inside the validator's bounds so that what
# the test observes is the comparison and never a rejection. Every field appears,
# so a name dropped from _CONFIG_FIELDS fails here rather than in production,
# where it would show up as a node whose changed geometry is never adopted.
CHANGES = {
    "rx_lat": 51.50,
    "rx_lon": -0.90,
    "rx_alt_ft": 130.0,
    "tx_lat": 51.30,
    "tx_lon": -0.80,
    "tx_alt_ft": 950.0,
    "tx_callsign": "Wrotham",
    "fc_hz": 545_000_000.0,
    "fs_hz": 2_400_000.0,
    "beam_width_deg": 60.0,
    "beam_azimuth_deg": 0.0,
    "max_range_km": 150.0,
    "cpi_s": 1.0,
    "delay_tolerance_us": 7.5,
    "doppler_tolerance_hz": 6.0,
}


@pytest.fixture
async def node(node_session):
    """A node row to hang configurations off: node_configs.node_id is a foreign
    key and the fixture database has PRAGMA foreign_keys=ON."""
    row = Node(node_id=NODE_ID, node_ref=mint_node_ref(), board_model="raspberrypi5-4gb")
    node_session.add(row)
    await node_session.flush()
    return row


def _config(**overrides) -> dict:
    return validate_config(dict(CONFIG, **overrides))


async def _rows(session, node_id: str = NODE_ID) -> list[NodeConfig]:
    rows = (
        (await session.execute(select(NodeConfig).where(NodeConfig.node_id == node_id).order_by(NodeConfig.version)))
        .scalars()
        .all()
    )
    return list(rows)


async def _versions(session, node_id: str = NODE_ID) -> list[tuple[int, bool]]:
    return [(row.version, row.superseded_at is not None) for row in await _rows(session, node_id)]


async def test_the_first_configuration_is_version_one(node_session, node):
    assert await upsert_config(node_session, NODE_ID, _config()) == 1
    assert await _versions(node_session) == [(1, False)]


async def test_an_unchanged_configuration_keeps_its_version(node_session, node):
    """The null trap: `beam_azimuth_deg` is null for every node that is not
    aimed, so a comparison done in SQL would never match and every resend would
    mint a version.

    On what this does and does not catch: it fails against hand-written SQL and
    against anything that binds the value as a parameter. It does not fail
    against a SQLAlchemy `column == value`, which renders `IS NULL` for a None
    and is accidentally correct. Comparing in Python is what makes the property
    hold whichever way the query is later written.
    """
    first = await upsert_config(node_session, NODE_ID, _config())

    again = await upsert_config(node_session, NODE_ID, _config())

    assert (first, again) == (1, 1)
    assert await _versions(node_session) == [(1, False)]


async def test_a_resend_is_unchanged_however_many_times_it_arrives(node_session, node):
    """A node told `config_stale` resends, so the loop this closes is unbounded
    and once is not a convincing demonstration.

    Both antenna fields are null here, which is the fleet's real state: retina-gui
    collects neither, so this is the pair every node sends.
    """
    # A fresh dict per call, so that what is compared is the values and never the
    # identity of the object they arrived in.
    await upsert_config(node_session, NODE_ID, _config(beam_width_deg=None))

    versions = [await upsert_config(node_session, NODE_ID, _config(beam_width_deg=None)) for _ in range(5)]

    assert versions == [1, 1, 1, 1, 1]
    assert await _versions(node_session) == [(1, False)]


@pytest.mark.parametrize("field", sorted(CHANGES))
async def test_a_change_to_any_one_field_mints_the_next_version(field, node_session, node):
    await upsert_config(node_session, NODE_ID, _config())

    version = await upsert_config(node_session, NODE_ID, _config(**{field: CHANGES[field]}))

    assert version == 2, f"{field} is not being compared"
    assert getattr((await _rows(node_session))[-1], field) == CHANGES[field]


async def test_a_changed_configuration_supersedes_the_previous_version(node_session, node):
    await upsert_config(node_session, NODE_ID, _config())

    version = await upsert_config(node_session, NODE_ID, _config(max_range_km=60.0))

    assert version == 2
    # The old row is kept and marked rather than updated: a detection frame
    # arrives stamped with the version it was computed under, so the geometry
    # behind an archived detection has to stay readable.
    assert await _versions(node_session) == [(1, True), (2, False)]
    assert (await _rows(node_session))[0].max_range_km == pytest.approx(50.0)


async def test_an_azimuth_appearing_is_a_new_version(node_session, node):
    """Null to a value is a real change: broadside to aimed due north. 0.0 and
    null are different configurations even though both are falsey."""
    await upsert_config(node_session, NODE_ID, _config())

    version = await upsert_config(node_session, NODE_ID, _config(beam_azimuth_deg=0.0))

    assert version == 2
    assert (await _rows(node_session))[-1].beam_azimuth_deg == 0.0


async def test_filling_in_a_null_is_a_change_in_both_directions(node_session, node):
    """Null is a value here, not a wildcard. Treating it as "unchanged, whatever
    arrives" would hide the one edit a node ever makes to its antenna geometry."""
    await upsert_config(node_session, NODE_ID, _config())

    aimed = await upsert_config(node_session, NODE_ID, _config(beam_azimuth_deg=90.0))
    unaimed = await upsert_config(node_session, NODE_ID, _config())

    assert (aimed, unaimed) == (2, 3)


async def test_versions_go_on_climbing_across_several_changes(node_session, node):
    versions = [await upsert_config(node_session, NODE_ID, _config(rx_lat=lat)) for lat in (51.42, 51.43, 51.44)]

    assert versions == [1, 2, 3]
    assert await _versions(node_session) == [(1, True), (2, True), (3, False)]


async def test_the_configuration_belongs_to_the_node_that_sent_it(node_session, node):
    """Version numbers are per node, and one node's resend is not another's change."""
    other = Node(node_id="ret9f8e7d6c", node_ref=mint_node_ref(), board_model="raspberrypi5-4gb")
    node_session.add(other)
    await node_session.flush()

    first = await upsert_config(node_session, NODE_ID, _config())
    second = await upsert_config(node_session, other.node_id, _config(rx_lat=51.50))

    assert (first, second) == (1, 1)
    assert await _versions(node_session) == [(1, False)]


async def test_a_version_is_never_reused_once_the_active_row_is_superseded(node_session, node):
    """Counting from the active row rather than from the highest one ever held
    restarts at 1 for a node whose only configuration has been superseded, and
    the insert then collides with uq_node_configs_node_version. Nothing
    supersedes without inserting today, which is exactly why it would be found
    by a 500 in production rather than here."""
    await upsert_config(node_session, NODE_ID, _config())
    row = (await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == NODE_ID))).scalars().one()
    row.superseded_at = datetime.now(UTC)
    await node_session.flush()

    version = await upsert_config(node_session, NODE_ID, _config())

    assert version == 2
    assert await _versions(node_session) == [(1, True), (2, False)]


async def test_nothing_is_committed(node_session, node):
    """The route owns the transaction: registration writes a node, an agreement
    set, a configuration, a revocation and a token as one unit, and a commit here
    would break it apart."""
    await upsert_config(node_session, NODE_ID, _config())

    await node_session.rollback()

    assert await _versions(node_session) == []


def test_the_compared_fields_are_every_geometry_column():
    """The three lists that have to agree: the columns, the validator's output and
    the comparison. A column added to node_configs without a line in
    _CONFIG_FIELDS would be written on a new version and then ignored when
    deciding to mint one."""
    columns = {c.key for c in inspect(NodeConfig).columns}
    bookkeeping = {"id", "node_id", "version", "created_at", "superseded_at"}

    assert set(_CONFIG_FIELDS) == columns - bookkeeping
    assert set(_CONFIG_FIELDS) == set(validate_config(dict(CONFIG)))
    assert len(_CONFIG_FIELDS) == 15
    assert set(CHANGES) == set(_CONFIG_FIELDS)
