"""Domain-specific auth helpers: invites, node ownership, claim codes.

All data is stored in the shared SQLite database (users.db) via SQLAlchemy
async sessions. On first startup, migrate_json_to_db() imports any existing
JSON files and renames them to *.json.migrated so they are not re-imported.

JWT, user storage, and session management are handled by fastapi-users
(see core/users.py). This module contains only the business logic that
has no equivalent in a general-purpose auth library.
"""

import json
import logging
import secrets
import time
from pathlib import Path

from sqlalchemy import delete, select

from core.users import ClaimCode, Invite, NodeOwner, async_session_maker

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INVITES_FILE = _DATA_DIR / "invites.json"
NODE_OWNERS_FILE = _DATA_DIR / "node_owners.json"
CLAIM_CODES_FILE = _DATA_DIR / "claim_codes.json"

INVITE_EXPIRY_S = 86400 * 14    # 14 days
CLAIM_CODE_EXPIRY_S = 86400 * 30  # 30 days

_MAX_ACTIVE_CLAIM_CODES_PER_USER = 10


# ── One-time JSON → SQLite migration ─────────────────────────────────────────

async def migrate_json_to_db() -> None:
    """Import existing JSON stores into SQLite on first startup (idempotent)."""
    async with async_session_maker() as session:
        async with session.begin():
            await _migrate_invites(session)
            await _migrate_node_owners(session)
            await _migrate_claim_codes(session)


async def _migrate_invites(session) -> None:
    if not INVITES_FILE.exists():
        return
    try:
        data = json.loads(INVITES_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s for migration", INVITES_FILE)
        return
    for token, inv in data.items():
        if await session.get(Invite, token):
            continue
        session.add(Invite(
            token=token,
            email=inv.get("email", "").lower(),
            role=inv.get("role", "user"),
            created_by=inv.get("created_by", ""),
            created_at=float(inv.get("created_at", 0)),
            expires_at=float(inv.get("expires_at", 0)),
            used_at=inv.get("used_at"),
        ))
    logger.info("Migrated invites from %s", INVITES_FILE)
    INVITES_FILE.rename(INVITES_FILE.with_suffix(".json.migrated"))


async def _migrate_node_owners(session) -> None:
    if not NODE_OWNERS_FILE.exists():
        return
    try:
        data = json.loads(NODE_OWNERS_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s for migration", NODE_OWNERS_FILE)
        return
    for node_id, user_id in data.items():
        if await session.get(NodeOwner, node_id):
            continue
        session.add(NodeOwner(node_id=node_id, user_id=user_id))
    logger.info("Migrated node owners from %s", NODE_OWNERS_FILE)
    NODE_OWNERS_FILE.rename(NODE_OWNERS_FILE.with_suffix(".json.migrated"))


async def _migrate_claim_codes(session) -> None:
    if not CLAIM_CODES_FILE.exists():
        return
    try:
        data = json.loads(CLAIM_CODES_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s for migration", CLAIM_CODES_FILE)
        return
    for code, rec in data.items():
        if await session.get(ClaimCode, code):
            continue
        session.add(ClaimCode(
            code=code,
            user_id=rec.get("user_id", ""),
            created_at=float(rec.get("created_at", 0)),
            expires_at=float(rec.get("expires_at", 0)),
            used_at=rec.get("used_at"),
            used_by_node_id=rec.get("used_by_node_id"),
        ))
    logger.info("Migrated claim codes from %s", CLAIM_CODES_FILE)
    CLAIM_CODES_FILE.rename(CLAIM_CODES_FILE.with_suffix(".json.migrated"))


# ── Invites ───────────────────────────────────────────────────────────────────

async def create_invite(email: str, role: str, created_by: str) -> dict:
    if role not in ("user", "admin"):
        raise ValueError("invalid role")
    email = email.lower().strip()
    if not email or "@" not in email:
        raise ValueError("invalid email")
    now = time.time()
    token = secrets.token_urlsafe(16)
    invite = Invite(
        token=token,
        email=email,
        role=role,
        created_by=created_by,
        created_at=now,
        expires_at=now + INVITE_EXPIRY_S,
        used_at=None,
    )
    async with async_session_maker() as session:
        session.add(invite)
        await session.commit()
    return _invite_to_dict(invite)


async def list_invites() -> list[dict]:
    async with async_session_maker() as session:
        result = await session.execute(select(Invite))
        return [_invite_to_dict(i) for i in result.scalars().all()]


async def revoke_invite(token: str) -> bool:
    async with async_session_maker() as session:
        invite = await session.get(Invite, token)
        if not invite:
            return False
        await session.delete(invite)
        await session.commit()
    return True


async def consume_invite_for_email(email: str) -> str | None:
    """Consume the oldest valid invite for this email. Returns role or None."""
    email = email.lower().strip()
    now = time.time()
    async with async_session_maker() as session:
        result = await session.execute(
            select(Invite)
            .where(
                Invite.email == email,
                Invite.used_at.is_(None),
                Invite.expires_at > now,
            )
            .order_by(Invite.created_at)
            .limit(1)
        )
        invite = result.scalar_one_or_none()
        if invite is None:
            return None
        invite.used_at = now
        role = invite.role
        await session.commit()
    return role


def _invite_to_dict(invite: Invite) -> dict:
    return {
        "token": invite.token,
        "email": invite.email,
        "role": invite.role,
        "created_by": invite.created_by,
        "created_at": invite.created_at,
        "expires_at": invite.expires_at,
        "used_at": invite.used_at,
    }


# ── Node ownership ────────────────────────────────────────────────────────────

async def get_node_owner(node_id: str) -> str | None:
    async with async_session_maker() as session:
        owner = await session.get(NodeOwner, node_id)
        return owner.user_id if owner else None


async def list_node_owners() -> dict[str, str]:
    async with async_session_maker() as session:
        result = await session.execute(select(NodeOwner))
        return {o.node_id: o.user_id for o in result.scalars().all()}


async def set_node_owner(node_id: str, user_id: str | None) -> None:
    async with async_session_maker() as session:
        if user_id is None:
            await session.execute(delete(NodeOwner).where(NodeOwner.node_id == node_id))
        else:
            owner = await session.get(NodeOwner, node_id)
            if owner:
                owner.user_id = user_id
            else:
                session.add(NodeOwner(node_id=node_id, user_id=user_id))
        await session.commit()


async def get_user_nodes(user_id: str) -> list[str]:
    async with async_session_maker() as session:
        result = await session.execute(
            select(NodeOwner.node_id).where(NodeOwner.user_id == user_id)
        )
        return list(result.scalars().all())


# ── Claim codes ───────────────────────────────────────────────────────────────

async def create_claim_code(user_id: str) -> dict:
    """Create a one-time claim code for the user.

    Raises ValueError if the user already has _MAX_ACTIVE_CLAIM_CODES_PER_USER
    active (unused, non-expired) codes.
    """
    now = time.time()
    async with async_session_maker() as session:
        result = await session.execute(
            select(ClaimCode).where(
                ClaimCode.user_id == user_id,
                ClaimCode.used_at.is_(None),
                ClaimCode.expires_at >= now,
            )
        )
        if len(result.scalars().all()) >= _MAX_ACTIVE_CLAIM_CODES_PER_USER:
            raise ValueError(
                f"Maximum of {_MAX_ACTIVE_CLAIM_CODES_PER_USER} active claim codes "
                "allowed per user. Revoke an existing code first."
            )
        code = secrets.token_hex(6).upper()  # 12 hex chars = 48 bits of entropy
        record = ClaimCode(
            code=code,
            user_id=user_id,
            created_at=now,
            expires_at=now + CLAIM_CODE_EXPIRY_S,
            used_at=None,
            used_by_node_id=None,
        )
        session.add(record)
        await session.commit()
    return _claim_code_to_dict(record)


async def list_claim_codes(user_id: str | None = None) -> list[dict]:
    async with async_session_maker() as session:
        q = select(ClaimCode)
        if user_id is not None:
            q = q.where(ClaimCode.user_id == user_id)
        result = await session.execute(q)
        return [_claim_code_to_dict(c) for c in result.scalars().all()]


async def revoke_claim_code(code: str, user_id: str | None = None) -> bool:
    async with async_session_maker() as session:
        rec = await session.get(ClaimCode, code)
        if not rec:
            return False
        if user_id is not None and rec.user_id != user_id:
            return False
        if rec.used_at is not None:
            return False
        await session.delete(rec)
        await session.commit()
    return True


async def consume_claim_code(code: str, node_id: str) -> str | None:
    """Mark a claim code used and assign node ownership atomically.

    Returns the user_id that now owns the node, or None on any failure.
    """
    if not code or not node_id:
        return None
    code = code.strip().upper()
    now = time.time()
    async with async_session_maker() as session:
        async with session.begin():
            rec = await session.get(ClaimCode, code)
            if not rec or rec.used_at is not None or rec.expires_at < now:
                return None
            user_id = rec.user_id
            rec.used_at = now
            rec.used_by_node_id = node_id
            owner = await session.get(NodeOwner, node_id)
            if owner:
                owner.user_id = user_id
            else:
                session.add(NodeOwner(node_id=node_id, user_id=user_id))
        return user_id


def _claim_code_to_dict(rec: ClaimCode) -> dict:
    return {
        "code": rec.code,
        "user_id": rec.user_id,
        "created_at": rec.created_at,
        "expires_at": rec.expires_at,
        "used_at": rec.used_at,
        "used_by_node_id": rec.used_by_node_id,
    }
