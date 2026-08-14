"""Persisting a node's configuration, one append-only row per version.

Separate from `services/node_config.py`, which is a leaf on purpose: it takes a
dict and returns a dict, knowing nothing of identity or the database, and both
its callers depend on it staying that way. Versioning is the other half of the
job and needs a session, so it lives here.

Nothing commits. `mint_token`, `revoke_tokens` and this all flush and leave the
transaction to the route, so registration can write a node, an agreement set, a
config version, a revocation and a token as one unit.

Registration is the only caller today, and PUT /v1/nodes/config is the other one
this exists for: a node that re-registers unchanged must be told the version the
server already holds rather than a fresh one, and the two endpoints cannot be
allowed to answer that differently.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeConfig


async def upsert_config(session: AsyncSession, node_id: str, config: dict[str, Any]) -> int:
    """Return the node's active configuration version, minting one if it moved.

    `config` is `validate_config`'s output: exactly the wire fields, normalised,
    and every one of them a column on `node_configs`. Passing a raw request body
    would write whatever keys it happened to carry.

    The comparison is field by field in Python rather than in SQL, because
    `NULL = NULL` is never true and `beam_azimuth_deg` is null for every node
    that is not aimed, which is all of them: comparing in the database would
    mint a version per resend for the whole fleet.
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

    if active is not None and all(getattr(active, field) == value for field, value in config.items()):
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
