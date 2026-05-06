"""fastapi-users: battle-tested JWT + cookie auth backed by SQLite.

User model, auth backend, and FastAPI dependency helpers live here.
All JWT issuance/verification is delegated to fastapi-users' JWTStrategy.
"""

import hashlib
import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users.exceptions import UserAlreadyExists, UserNotExists
from sqlalchemy import DateTime, Float, String, event, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ── Config ────────────────────────────────────────────────────────────────────

_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()
_jwt_from_env = os.getenv("JWT_SECRET", "")
if not _jwt_from_env and _RETINA_ENV not in ("dev", "test"):
    raise RuntimeError(
        "JWT_SECRET environment variable is required in production "
        f"(RETINA_ENV={_RETINA_ENV!r}). Set it to a random ≥32-byte string."
    )

JWT_SECRET = _jwt_from_env or "retina-dev-secret-change-me-in-prod-32b!"
JWT_LIFETIME_SECONDS = 86400 * 7  # 7 days

ADMIN_EMAILS: set[str] = {
    e.strip().lower()
    for e in os.getenv("AUTH_ADMIN_EMAILS", "").split(",")
    if e.strip()
}

AUTH_ENABLED = bool(os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GITHUB_CLIENT_ID"))

# Anonymous admin bypass is available in dev/test/staging when no OAuth is configured.
# In production, missing OAuth keys must yield 401 — not open admin access.
AUTH_BYPASS = not AUTH_ENABLED and _RETINA_ENV in ("dev", "test", "staging")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite+aiosqlite:///{_DATA_DIR}/users.db"

# ── SQLAlchemy setup ─────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """Extends fastapi-users base with radar-specific profile fields."""

    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    avatar: Mapped[str] = mapped_column(String(512), default="", server_default="")
    provider: Mapped[str] = mapped_column(String(50), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Invite(Base):
    __tablename__ = "invites"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)


class NodeOwner(Base):
    __tablename__ = "node_owners"

    node_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)


class ClaimCode(Base):
    __tablename__ = "claim_codes"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    used_by_node_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)


engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _conn_rec):
    """Apply WAL mode + safety pragmas on every new SQLite connection.

    WAL mode is the single biggest correctness improvement we can make: it
    survives `kill -9` mid-write without corrupting the database (rollback
    journal mode can leave the file in a half-written state). Combined with
    `synchronous=NORMAL` it also lets readers proceed concurrently with a
    single writer instead of serialising everything behind a global lock.

    `busy_timeout` lets writers wait briefly for a contending lock instead
    of returning SQLITE_BUSY immediately — a much better default for a
    web app where the alternative is a 500 to the user.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, User)


# ── fastapi-users schemas ─────────────────────────────────────────────────────


class UserRead(schemas.BaseUser[uuid.UUID]):
    name: str
    avatar: str
    provider: str
    created_at: datetime


class UserCreate(schemas.BaseUserCreate):
    name: str = ""
    avatar: str = ""
    provider: str = ""


class UserUpdate(schemas.BaseUserUpdate):
    name: str | None = None
    avatar: str | None = None
    provider: str | None = None


# ── UserManager ──────────────────────────────────────────────────────────────

# Derive distinct secrets so reset and verify tokens can't be cross-used
_RESET_SECRET = hashlib.sha256(b"reset:" + JWT_SECRET.encode()).hexdigest()
_VERIFY_SECRET = hashlib.sha256(b"verify:" + JWT_SECRET.encode()).hexdigest()


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = _RESET_SECRET
    verification_token_secret = _VERIFY_SECRET


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


# ── Auth backend: Cookie transport + JWT strategy ─────────────────────────────

cookie_transport = CookieTransport(
    cookie_name="auth_token",
    cookie_max_age=JWT_LIFETIME_SECONDS,
    cookie_httponly=True,
    cookie_secure=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=JWT_SECRET, lifetime_seconds=JWT_LIFETIME_SECONDS)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

# ── Helper: anonymous user (auth disabled in dev) ─────────────────────────────

ANONYMOUS_USER: dict = {
    "id": "00000000-0000-0000-0000-000000000000",
    "email": "admin@retina.fm",
    "name": "Admin (no auth)",
    "avatar": "",
    "provider": "none",
    "role": "admin",
    "is_superuser": True,
    "created_at": 0,
}


def user_to_dict(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "avatar": user.avatar,
        "provider": user.provider,
        "role": "admin" if user.is_superuser else "user",
        "is_superuser": user.is_superuser,
        "created_at": user.created_at.timestamp() if user.created_at else 0,
    }


# ── FastAPI dependency helpers ────────────────────────────────────────────────
# These wrap fastapi-users' JWT strategy so the rest of the codebase can call
# them with just a Request — no change to route signatures needed.

_SENTINEL = object()


async def _read_user_from_request(request: Request) -> User | None:
    """Validate the auth_token cookie using fastapi-users' JWTStrategy.

    Result is cached on request.state to avoid repeated DB lookups per request.
    """
    cached = getattr(request.state, "_auth_user", _SENTINEL)
    if cached is not _SENTINEL:
        return cached
    token = request.cookies.get("auth_token")
    if not token:
        request.state._auth_user = None
        return None
    strategy = get_jwt_strategy()
    async with async_session_maker() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)
        user = await strategy.read_token(token, user_manager)
    request.state._auth_user = user
    return user


async def get_current_user(request: Request) -> dict:
    """Return user dict or raise 401. In dev/test with no OAuth configured, returns anonymous admin."""
    if AUTH_BYPASS:
        return dict(ANONYMOUS_USER)
    user = await _read_user_from_request(request)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_to_dict(user)


async def require_admin(request: Request) -> dict:
    """Like get_current_user but also enforces superuser/admin role."""
    if AUTH_BYPASS:
        return dict(ANONYMOUS_USER)
    user = await _read_user_from_request(request)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user.is_superuser:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_to_dict(user)


# ── OAuth user creation helper ────────────────────────────────────────────────


async def get_or_create_oauth_user(
    *,
    email: str,
    name: str,
    avatar: str,
    provider: str,
    consume_invite_fn,
) -> User:
    """Find an existing user by email or create a new one via OAuth.

    Consumes a pending invite for the email to determine the initial role.
    Updates name/avatar on every login so the profile stays current.
    """

    email = email.lower().strip()

    async with async_session_maker() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)

        try:
            user = await user_manager.get_by_email(email)
            # Update mutable profile fields on every OAuth login
            user.name = name
            user.avatar = avatar
            user.provider = provider
            # Allow a pending "admin" invite to upgrade an existing user's role
            invited_role = await consume_invite_fn(email)
            if invited_role == "admin" and not user.is_superuser:
                user.is_superuser = True
            await session.commit()
            await session.refresh(user)
            return user

        except UserNotExists:
            invited_role = await consume_invite_fn(email)
            is_superuser = email in ADMIN_EMAILS or invited_role == "admin"
            user_create = UserCreate(
                email=email,
                password=secrets.token_urlsafe(32),  # unused — OAuth-only account
                name=name,
                avatar=avatar,
                provider=provider,
                is_verified=True,
                is_superuser=is_superuser,
            )
            try:
                return await user_manager.create(user_create)
            except UserAlreadyExists:
                # Race: another request created this user between our get and create.
                return await user_manager.get_by_email(email)
