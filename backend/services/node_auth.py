"""Node bearer tokens and the public identifier.

Only the SHA-256 of a bearer is stored: a per-row salted hash could carry neither
a unique index nor a lookup. There is no cache, so revocation takes effect on the
next request rather than needing an invalidation path, and the service is not
pinned to a single uvicorn worker. Twelve nodes at 2 Hz is 24 indexed reads a
second against WAL-mode SQLite, which the read budget absorbs.

Nothing here commits. Registration writes a node, an agreement set, a config
version, a revocation and a token; the route owns that transaction so a failure
part-way through cannot leave a node with its old token revoked and no new one.
"""

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeToken
from core.users import get_async_session

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"

# Declared purely so the generated contract carries `bearerAuth`. Without it the
# schema describes the four endpoints with no credential at all, and a client
# generated from the published file would not send the header the server
# requires. The routers that authenticate depend on this; `bearer_node` below
# still reads the header itself.
#
# `auto_error=False` because the refusal is not the framework's to make: its own
# is a 403 in FastAPI's shape, where the contract wants a 401 in the node error
# taxonomy, and it would fire ahead of `bearer_node` and hide it.
node_bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    auto_error=False,
    description=(
        "The token minted by `POST /v1/nodes/register`, persisted at mode 0600 under `/data`. "
        "Sent as `Authorization: Bearer <token>`. There is no expiry: a token dies by being "
        "revoked and by nothing else."
    ),
)


def mint_node_ref() -> str:
    """Fifteen characters, roughly 62 bits after the prefix."""
    return "nde" + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def mint_token(session: AsyncSession, node_id: str) -> str:
    """Mint a bearer and flush it. The caller commits."""
    token = secrets.token_urlsafe(32)
    session.add(NodeToken(node_id=node_id, token_hash=token_hash(token)))
    await session.flush()
    return token


async def revoke_tokens(session: AsyncSession, node_id: str, *, reason: str) -> int:
    """Revoke every live token for a node and flush. The caller commits."""
    rows = (
        (await session.execute(select(NodeToken).where(NodeToken.node_id == node_id, NodeToken.revoked_at.is_(None))))
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    await session.flush()
    return len(rows)


async def bearer_node(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
) -> str:
    """Resolve the Authorization header to a node_id, or raise 401.

    The only authentication predicate is a live row: there is no expiry, so a
    token dies by being revoked and by nothing else.
    """
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    # RFC 7235 makes auth-scheme case-insensitive, so `bearer` is as good as
    # `Bearer`. Our own nodes send the capitalised form the spec writes, but a
    # conformant client sending the other one is not an authentication failure.
    if scheme.lower() != "bearer" or not presented.strip():
        raise HTTPException(status_code=401, detail="unauthorized")
    row = (
        await session.execute(
            select(NodeToken).where(
                NodeToken.token_hash == token_hash(presented.strip()),
                NodeToken.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return row.node_id
