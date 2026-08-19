"""Persisting a node's configuration, one append-only row per version.

Separate from `services/node_config.py`, which is a leaf on purpose: it takes a
dict and returns a dict, knowing nothing of identity or the database, and both
its callers depend on it staying that way. Versioning is the other half of the
job and needs a session, so it lives here.

Nothing commits. `mint_token`, `revoke_tokens` and this all flush and leave the
transaction to the route, so registration can write a node, an agreement set, a
config version, a revocation and a token as one unit.

Registration and PUT /v1/nodes/config both answer from here: a node that resends
an unchanged configuration must be told the version the server already holds
rather than a fresh one, and the two endpoints cannot be allowed to answer that
differently.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeConfig

# The fifteen columns a configuration version consists of: everything on
# node_configs bar the surrogate key, the node it belongs to, the version number
# and the two timestamps. Exactly validate_config's output keys, and
# test_node_config_store.py pins the three lists against each other, so a column
# added later cannot be silently left out of the comparison below.
_CONFIG_FIELDS = (
    "rx_lat",
    "rx_lon",
    "rx_alt_ft",
    "tx_lat",
    "tx_lon",
    "tx_alt_ft",
    "tx_callsign",
    "fc_hz",
    "fs_hz",
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
    "cpi_s",
    "delay_tolerance_us",
    "doppler_tolerance_hz",
)


async def upsert_config(session: AsyncSession, node_id: str, config: dict[str, Any]) -> int:
    """Return the node's active configuration version, minting one if it moved.

    `config` is `validate_config`'s output: exactly the wire fields, normalised,
    and every one of them a column on `node_configs`. Passing a raw request body
    would write whatever keys it happened to carry.

    The comparison is field by field in Python rather than in SQL, because
    `NULL = NULL` is never true and both antenna fields are nullable:
    `beam_azimuth_deg` on every node that is not aimed, `beam_width_deg` on every
    node in the fleet, since retina-gui does not collect the geometry. Comparing
    in the database would mint a version per resend for the whole fleet, tell each
    node `config_stale` in perpetuity, and have each one resend in response.
    """
    active = (
        (
            await session.execute(
                select(NodeConfig)
                .where(NodeConfig.node_id == node_id, NodeConfig.superseded_at.is_(None))
                .order_by(NodeConfig.version.desc())
            )
        )
        .scalars()
        .first()
    )

    if active is not None and all(getattr(active, field) == config[field] for field in _CONFIG_FIELDS):
        return active.version

    now = datetime.now(UTC)
    # Counted from the highest version the node has ever held rather than from
    # the active one. They are the same today, since nothing else supersedes a
    # row, but anything that ever superseded one without inserting a replacement
    # would restart this at 1 and collide with the unique constraint.
    highest = (
        await session.execute(select(func.max(NodeConfig.version)).where(NodeConfig.node_id == node_id))
    ).scalar()
    version = 1 if highest is None else highest + 1
    if active is not None:
        # Superseded rather than updated: a detection frame arrives stamped with
        # the version it was computed under, so the geometry behind an archived
        # detection has to stay readable.
        active.superseded_at = now
    session.add(NodeConfig(node_id=node_id, version=version, **config))
    await session.flush()
    return version
