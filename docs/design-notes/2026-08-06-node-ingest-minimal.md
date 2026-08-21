# Node Ingest, Minimal: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get twelve real nodes posting detections into production over the v1 API, behind Cloudflare, with the frames reaching the existing pipeline. Server side only.

**Supersedes** `2026-08-06-node-api-v1.md`, which is the full-fidelity version of the same work. Keep that file: it is where the dropped pieces are written down with their reasons, and it is what to read when any of them comes back.

**Architecture:** New SQLAlchemy models in `core/nodes.py` on the existing `Base` and engine, migrated by Alembic. Three tables, not seven. Authentication reads the database on each request rather than a cache. The Mender management API is called in-request behind one function. A v1 node joins the pipeline through exactly the two calls `services/blah2_bridge.py` already uses, so its frames reach the map without touching the solver.

**Tech Stack:** Python 3.12, FastAPI 0.109 + uvicorn, SQLAlchemy 2 async with `aiosqlite`, Alembic, httpx, pytest, ruff, nginx, ufw.

## Global Constraints

- Working directory for all commands is `~/owl/Tower-Finder`.
- Branch first, off the default branch.
- Stage the named paths per task. This clone carries unrelated working-tree cruft (dirty submodule pointers under `libs/`, local data files); never `git add -A`.
- Tests: `cd backend && uv run pytest tests/ -q`, or `uv run --directory backend pytest tests/ -q` from the
  repo root. `uv run` resolves `backend/.venv` from that directory; the `requires-python` warning it prints is
  expected and harmless.
- Coverage gate is `--cov-fail-under=55` and runs on every pytest invocation. Add `--no-cov` when running a single file.
- Lint is two commands and both must pass: `uv run --directory backend ruff check .` and
  `uv run --directory backend ruff format .`. `ruff check` alone leaves formatting diffs.
- `line-length = 120`, `target-version = "py312"`, ruff rule set `E,W,F,I,B,UP,S,SIM`.
- Install with `uv pip install --python backend/.venv/bin/python <pkg>` and pin in `backend/requirements.txt`.
  `backend/pyproject.toml` carries no `[project]` table, so `uv add` does not apply here.
- Every migration uses `op.batch_alter_table` for anything other than `create_table`. SQLite cannot `ALTER`.
- The wire contract is `~/owl/nodes_api_v1.yml`. Where this plan and the spec disagree, the spec wins.
- Writing style for any doc or comment: no em-dashes, British spellings, state each fact once.

## Scope

**Solver correctness is explicitly not in scope.** The goal is that the numbers arrive and are attributed to the right node. Whether the geometry produces a good fix is a separate problem, and no task here should be extended to chase it. If a node's detections reach `state.frame_queue` under its own `node_id`, that task is done.

**In.** Alembic. Three tables. Registration against a live Mender lookup. `PUT /nodes/config` with real versioning, `config_stale` and the 409. Heartbeat. Detections. The Cloudflare origin boundary.

**Out, with the reason.** Each of these is written up in the superseded plan.

| Dropped | Because |
|---|---|
| The solver process boundary | Twelve nodes at 2 Hz is 24 frames a second, and nothing in this plan depends on it |
| CLI packaging and the four operator subcommands | `sqlite3` on the droplet covers twelve nodes |
| The three in-memory caches | A database read per request is affordable at this rate, and it removes the `--workers 1` constraint rather than entrenching it |
| `node_registration_limits` and the escalating cooldown | An in-process counter is enough with Cloudflare in front |
| `mender_devices`, `mender_webhook_events`, `node_fleets` | Nothing reads them: the Mender lookup is in-request, and synthetic fleets are out of scope |
| Reactivation gating on re-registration | Without the operator subcommands there is no way to trigger reactivation, so the gate would permanently brick any reflashed board. See task 7 for what replaces it |
| `node_ref` rotation, `archive_ref`, display offsets, owner and claiming | Archive and claiming are not in this phase |
| The `is_synthetic` accessor refactor | No synthetic fleets run here. The five prefix predicates stay as they are |
| The registration latency floor and its timing tests | Kept: one shared 403 body. Dropped: the constant-latency guarantee |
| Retiring `blah2_bridge` and `:3012` | They stay up. They are the rollback |

## Error taxonomy

| Condition | Status | Body |
|---|---|---|
| Registered | 200 | `RegisterResponse` |
| Config failed validation | 400 | `{"error": "invalid_config", "detail": "<field>"}` |
| Unknown device, not accepted by Mender, ambiguous auth sets, Mender unreachable | 403 | `{"error": "forbidden"}` with `Retry-After` |
| Bearer absent, unknown or revoked | 401 | `{"error": "unauthorized"}` |
| Detection frame carries an unknown `config_version` | 409 | `{"error": "unknown_config_version"}` |
| Per-token rate limit | 429 | `{"error": "rate_limited"}` with `Retry-After` |

One ordering rule survives from the ADR and is a requirement rather than a preference: **configuration validation runs after identity resolution.** A 400 reachable before it turns the difference between 400 and 403 into an oracle for which identities exist.

## File structure

| File | Responsibility |
|---|---|
| `backend/alembic.ini`, `backend/migrations/` | Migrations |
| `backend/core/nodes.py` | `Node`, `NodeConfig`, `NodeToken` |
| `backend/services/node_config.py` | The shared validator |
| `backend/services/node_auth.py` | Token minting and the bearer dependency |
| `backend/services/mender.py` | The in-request lookup |
| `backend/services/node_pipeline.py` | The handoff into `state` |
| `backend/routes/node_schemas.py` | Pydantic request and response models |
| `backend/routes/nodes.py` | The four endpoints |
| `deploy/nginx/snippets/cloudflare.conf` | `real_ip` and origin pulls |
| `deploy/setup-server.sh` | ufw narrowing |

---

### Task 1: Close the auth bypass and make alerts reach a person

**Files:**
- Modify: `backend/core/users.py:43-47`
- Modify: droplet environment on both `retina-staging` and `retina-prod`
- Test: `backend/tests/test_auth_bypass.py`

- [ ] **Step 1: Write the failing test**

```python
import importlib

import pytest


@pytest.mark.parametrize("env", ["production", "prod", "staging", ""])
def test_the_bypass_is_off_outside_dev_and_test(monkeypatch, env):
    monkeypatch.setenv("RETINA_ENV", env)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    users = importlib.reload(importlib.import_module("core.users"))
    assert users.AUTH_BYPASS is False


@pytest.mark.parametrize("env", ["dev", "test"])
def test_the_bypass_is_available_in_dev_and_test(monkeypatch, env):
    monkeypatch.setenv("RETINA_ENV", env)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    users = importlib.reload(importlib.import_module("core.users"))
    assert users.AUTH_BYPASS is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_auth_bypass.py -v --no-cov`
Expected: FAIL on the `staging` case. `core/users.py:47` currently includes `"staging"` in the bypass set, and staging is internet-facing.

- [ ] **Step 3: Remove staging from the bypass**

```python
# Anonymous admin bypass is available only where the service is not reachable
# from the internet. Staging is, so it authenticates like production.
AUTH_BYPASS = not AUTH_ENABLED and _RETINA_ENV in ("dev", "test")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_auth_bypass.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Confirm staging still admits a real operator**

Deploy to staging, sign in with OAuth, and confirm the admin surfaces load. If `GOOGLE_CLIENT_ID` and `GITHUB_CLIENT_ID` are unset on staging, this change locks it out entirely, which is the correct behaviour and a configuration task rather than a reason to revert.

- [ ] **Step 6: Set the alert webhook on both droplets**

```bash
ssh retina-staging 'grep -c ALERT_WEBHOOK_URL /opt/retina-server/.env || true'
ssh retina-prod 'grep -c ALERT_WEBHOOK_URL /opt/retina-server/.env || true'
```

Set it on both, restart, then raise one by hand and confirm it arrives somewhere a person sees it:

```bash
ssh retina-staging 'cd /opt/retina-server && docker compose exec api python -c "
from services.alerting import send_alert
send_alert(\"test\", \"phase 1 bring-up: alert path check\")
"'
```

Until this is set, every alert in the design returns quietly and any test asserting one fires passes against nothing.

- [ ] **Step 7: Lint and commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/core/users.py backend/tests/test_auth_bypass.py
git commit -m "fix(auth): staging authenticates like production

The anonymous admin bypass covered staging, which is internet-facing. The bypass
is for environments that are not reachable, which is dev and test only."
```

---

### Task 2: Alembic, with the existing tables as the baseline

**Files:**
- Create: `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/script.py.mako`, `backend/migrations/versions/0001_baseline.py`
- Modify: `backend/requirements.txt`, `backend/core/users.py:49-51,135-137`
- Test: `backend/tests/test_migrations.py`

Follow tasks 1 of the superseded plan verbatim: it is unchanged by the scope cut. The four points that matter:

- `render_as_batch=True` in `env.py`, or autogenerated alters fail on SQLite.
- `env.py` reads `DATABASE_URL` from `core.users` so there is one source of truth.
- Make the database path overridable via `RETINA_DB_PATH` so the round-trip test does not use the developer's database.
- The test runs upgrade, downgrade, upgrade. Downgrades get assumed rather than run, and this is the cheapest window there will be to find out they do not work.

- [ ] **Step 1: Install and pin Alembic**
- [ ] **Step 2: Write `tests/test_migrations.py` with the round-trip test**
- [ ] **Step 3: Run it and watch it fail**
- [ ] **Step 4: Write `alembic.ini`, `env.py`, `script.py.mako`**
- [ ] **Step 5: Write `0001_baseline.py` creating `user`, `invites`, `node_owners`, `claim_codes`**
- [ ] **Step 6: Guard `create_all` behind `RETINA_SCHEMA_SOURCE`, defaulting to alembic**
- [ ] **Step 7: Run the whole suite, lint, commit**

Verify the baseline by running `uv run --directory backend alembic upgrade head` against an empty file and comparing `sqlite3 <db> .schema` against a database built by `create_all`. A drift here passes the round-trip test and diverges on a fresh deploy.

---

### Task 3: Three tables

**Files:**
- Create: `backend/core/nodes.py`, `backend/migrations/versions/0002_nodes.py`
- Test: `backend/tests/test_node_models.py`

**Interfaces:**
- Produces: `core.nodes.Node`, `core.nodes.NodeConfig`, `core.nodes.NodeToken`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.nodes import Node, NodeConfig, NodeToken


async def test_a_node_round_trips(node_session):
    node_session.add(Node(node_id="ret1a2b3c4d", node_ref="nde4f2k9xq7m3b8",
                          board_model="raspberrypi5-4gb", status="active"))
    await node_session.commit()
    found = (await node_session.execute(select(Node).where(Node.node_id == "ret1a2b3c4d"))).scalar_one()
    assert found.active_config_version == 0
    assert found.publication == "public"


async def test_config_versions_are_unique_per_node(node_session):
    node_session.add_all([_config("ret1a2b3c4d", 1), _config("ret1a2b3c4d", 1)])
    with pytest.raises(IntegrityError):
        await node_session.commit()


async def test_beam_azimuth_null_survives_a_round_trip(node_session):
    node_session.add(_config("ret9f8e7d6c", 1, beam_azimuth_deg=None))
    await node_session.commit()
    found = (
        await node_session.execute(select(NodeConfig).where(NodeConfig.node_id == "ret9f8e7d6c"))
    ).scalar_one()
    assert found.beam_azimuth_deg is None


async def test_a_token_hash_is_unique(node_session):
    node_session.add_all([NodeToken(node_id="ret1a2b3c4d", token_hash="a" * 64),
                          NodeToken(node_id="ret9f8e7d6c", token_hash="a" * 64)])
    with pytest.raises(IntegrityError):
        await node_session.commit()
```

`test_beam_azimuth_null_survives_a_round_trip` is not decoration. `null` means broadside and `0.0` means aimed due north, and conflating them silently re-aims every omnidirectional node in the fleet.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_models.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.nodes'`.

- [ ] **Step 3: Write the models**

`backend/core/nodes.py`:

```python
"""Node data model, minimal.

This is ADR §6 with the archive, rotation, reactivation, ownership and mirror
columns left out. They are in the superseded plan with their reasons; adding one
back later is a migration, which is what Alembic is here for.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.users import Base

NODE_STATUSES = ("active", "retired", "blocked")


class Node(Base):
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_ref: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    board_model: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    active_config_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    licence_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    licence_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_management_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote_management_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication: Mapped[str] = mapped_column(String(8), default="public", server_default="public")
    publication_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    publication_chosen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeConfig(Base):
    __tablename__ = "node_configs"
    __table_args__ = (UniqueConstraint("node_id", "version", name="uq_node_configs_node_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rx_lat: Mapped[float] = mapped_column(Float)
    rx_lon: Mapped[float] = mapped_column(Float)
    rx_alt_ft: Mapped[float] = mapped_column(Float)
    tx_lat: Mapped[float] = mapped_column(Float)
    tx_lon: Mapped[float] = mapped_column(Float)
    tx_alt_ft: Mapped[float] = mapped_column(Float)
    tx_callsign: Mapped[str] = mapped_column(String(32))
    fc_hz: Mapped[float] = mapped_column(Float)
    fs_hz: Mapped[float] = mapped_column(Float)
    beam_width_deg: Mapped[float] = mapped_column(Float)
    beam_azimuth_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_range_km: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeToken(Base):
    __tablename__ = "node_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

There is no `expires_at`. Authentication is `revoked_at IS NULL` and nothing else.

- [ ] **Step 4: Write the migration**

`backend/migrations/versions/0002_nodes.py`, `revision = "0002"`, `down_revision = "0001"`, three `create_table` calls mirroring the models with their indexes and the unique constraint, and a `downgrade` dropping them in reverse.

- [ ] **Step 5: Add the `node_session` fixture**

In `backend/tests/conftest.py`, a fixture yielding an `AsyncSession` against a per-test SQLite file with `alembic upgrade head` applied.

- [ ] **Step 6: Run tests, lint, commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend pytest tests/test_node_models.py tests/test_migrations.py -v --no-cov
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/core/nodes.py backend/migrations/versions/0002_nodes.py backend/tests/test_node_models.py backend/tests/conftest.py
git commit -m "feat(db): nodes, node_configs and node_tokens

ADR §6 minus the archive, rotation, reactivation, ownership and mirror columns.
Those are migrations when they are wanted, which is what Alembic is here for.
beam_azimuth_deg stays nullable: null is broadside, 0.0 is aimed due north."
```

---

### Task 4: The shared configuration validator

**Files:**
- Create: `backend/services/node_config.py`
- Test: `backend/tests/test_node_config_validation.py`

Take this task verbatim from task 6 of the superseded plan, including the full test list and the implementation. It is unchanged by the scope cut, because it is the whole content of the 400 on both endpoints that survive.

The two checks worth not losing under time pressure, since a JSON schema cannot express either: a receiver and illuminator at the same point, and `bool` masquerading as a number because `bool` subclasses `int` in Python.

- [ ] **Step 1: Write `tests/test_node_config_validation.py`**
- [ ] **Step 2: Run it and watch it fail**
- [ ] **Step 3: Write `services/node_config.py` exposing `validate_config(payload) -> dict` and `ConfigInvalid` carrying `.field`**
- [ ] **Step 4: Run tests, lint, commit**

---

### Task 5: Tokens and bearer authentication

**Files:**
- Create: `backend/services/node_auth.py`
- Test: `backend/tests/test_node_auth.py`

**Interfaces:**
- Produces:
  - `async mint_token(session, node_id) -> str`
  - `async revoke_tokens(session, node_id, reason) -> int`
  - `async bearer_node(request, session) -> str`, a FastAPI dependency returning a `node_id`
  - `mint_node_ref() -> str`

There is no cache. Every authenticated request reads `node_tokens` by hash. At twelve nodes and 2 Hz that is 24 indexed reads a second against WAL-mode SQLite, and it removes the `--workers 1` constraint the cached design would have imposed.

- [ ] **Step 1: Write the failing test**

```python
import hashlib

import pytest
from fastapi import HTTPException

from services.node_auth import bearer_node, mint_node_ref, mint_token, revoke_tokens


async def test_only_the_hash_is_stored(node_session, seeded_node):
    token = await mint_token(node_session, "ret1a2b3c4d")
    rows = await _tokens(node_session, "ret1a2b3c4d")
    assert rows[0].token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert all(token not in r.token_hash for r in rows)


async def test_a_minted_token_resolves(node_session, seeded_node):
    token = await mint_token(node_session, "ret1a2b3c4d")
    assert await bearer_node(_request(token), node_session) == "ret1a2b3c4d"


async def test_a_revoked_token_does_not_resolve(node_session, seeded_node):
    token = await mint_token(node_session, "ret1a2b3c4d")
    await revoke_tokens(node_session, "ret1a2b3c4d", reason="reissue")
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request(token), node_session)
    assert excinfo.value.status_code == 401


@pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Bearer ", "Token xyz"])
async def test_a_malformed_authorization_header_is_401(node_session, header):
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request_raw(header), node_session)
    assert excinfo.value.status_code == 401


async def test_an_unknown_token_is_401(node_session):
    with pytest.raises(HTTPException) as excinfo:
        await bearer_node(_request("nonsense"), node_session)
    assert excinfo.value.status_code == 401


def test_a_minted_node_ref_matches_the_wire_pattern():
    ref = mint_node_ref()
    assert ref.startswith("nde")
    assert len(ref) == 15
    assert ref[3:].isalnum() and ref[3:].islower()


def test_minting_does_not_repeat():
    assert len({mint_node_ref() for _ in range(1000)}) == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_auth.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""Node bearer tokens and the public identifier.

Only the SHA-256 of a bearer is stored: a per-row salted hash could carry neither
a unique index nor a lookup. There is no cache, so revocation takes effect on the
next request rather than needing an invalidation path.
"""

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeToken

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def mint_node_ref() -> str:
    """Fifteen characters, roughly 62 bits after the prefix."""
    return "nde" + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def mint_token(session: AsyncSession, node_id: str) -> str:
    token = secrets.token_urlsafe(32)
    session.add(NodeToken(node_id=node_id, token_hash=token_hash(token)))
    await session.commit()
    return token


async def revoke_tokens(session: AsyncSession, node_id: str, *, reason: str) -> int:
    rows = (
        await session.execute(
            select(NodeToken).where(NodeToken.node_id == node_id, NodeToken.revoked_at.is_(None))
        )
    ).scalars().all()
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
        row.revoked_reason = reason
    await session.commit()
    return len(rows)


async def bearer_node(request: Request, session: AsyncSession) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise HTTPException(status_code=401, detail="unauthorized")
    row = (
        await session.execute(
            select(NodeToken).where(
                NodeToken.token_hash == token_hash(header[7:].strip()),
                NodeToken.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return row.node_id
```

- [ ] **Step 4: Render 401 as the wire's error body**

Add an exception handler on the router so `HTTPException(401)` renders `{"error": "unauthorized"}` rather than FastAPI's `{"detail": ...}`.

- [ ] **Step 5: Run tests, lint, commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend pytest tests/test_node_auth.py -v --no-cov
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/services/node_auth.py backend/tests/test_node_auth.py
git commit -m "feat(nodes): bearer tokens, resolved from the database per request

No token cache. Twelve nodes at 2 Hz is 24 indexed reads a second against
WAL-mode SQLite, and doing without the cache means revocation needs no
invalidation path and the service is not pinned to a single uvicorn worker."
```

---

### Task 6: The Mender lookup

**Files:**
- Create: `backend/services/mender.py`
- Test: `backend/tests/test_mender_lookup.py`

Take this task verbatim from task 8 of the superseded plan: the module, the six tests and the live-tenant verification step are unchanged.

The one thing not to simplify away: `MenderUnreachable` is a distinct exception from a `None` return. Both are the same 403 on the wire, but only one of them means every enrolment in the fleet is blocked, and that is what `mender_unreachable` alerts on.

- [ ] **Step 1: Write `tests/test_mender_lookup.py` with the `httpx.MockTransport` cases**
- [ ] **Step 2: Run it and watch it fail**
- [ ] **Step 3: Write `services/mender.py` exposing `lookup_device`, `MenderDeviceRecord`, `MenderUnreachable`**
- [ ] **Step 4: Run tests, lint, commit**
- [ ] **Step 5: Verify against the live tenant with a real `MENDER_MANAGEMENT_TOKEN` and a known enrolled `node_id`**

Step 5 is the only thing in this plan blocked on an external credential. Everything downstream runs against the fake.

---

### Task 7: `POST /v1/nodes/register`

**Files:**
- Create: `backend/routes/node_schemas.py`, `backend/routes/nodes.py`
- Modify: `backend/main.py:234`
- Test: `backend/tests/test_node_register.py`

**Re-registration is allowed.** The ADR gates it on operator reactivation, and this plan has no operator subcommands, so that gate would permanently brick any reflashed board. What replaces it: a node Mender still accepts may register again, the previous token is revoked with `revoked_reason='reflash'`, and the event raises `registration_held` so it is on the record. The cost is that anyone who can enrol a key under a known identity can take a node over, which is the exposure D31 was deferred against. It is written here so putting the gate back is a decision rather than a rediscovery.

- [ ] **Step 1: Write the failing test**

```python
VALID_CONFIG = {
    "rx_lat": 51.42, "rx_lon": -0.91, "rx_alt_ft": 120,
    "tx_lat": 51.37, "tx_lon": -0.88, "tx_alt_ft": 900, "tx_callsign": "CRYSTAL_PALACE",
    "fc_hz": 5.7e8, "fs_hz": 2.0e6,
    "beam_width_deg": 60, "beam_azimuth_deg": None, "max_range_km": 150,
}
AGREEMENTS = {
    "licence": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "remote_management": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "publication": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z", "choice": "public"},
}


def _body(node_id="ret1a2b3c4d", **overrides):
    return {"node_id": node_id, "board_model": "raspberrypi5-4gb",
            "agreements": AGREEMENTS, "config": VALID_CONFIG} | overrides


async def test_an_accepted_device_registers(node_client, accepted_in_mender):
    accepted_in_mender("ret1a2b3c4d")
    response = await node_client.post("/v1/nodes/register", json=_body())
    assert response.status_code == 200
    payload = response.json()
    assert 32 <= len(payload["token"]) <= 128
    assert payload["node_ref"].startswith("nde") and len(payload["node_ref"]) == 15
    assert payload["config_version"] == 1
    assert payload["server_time"].endswith("Z")


async def test_the_agreement_records_are_stored(node_client, accepted_in_mender, node_session):
    accepted_in_mender("ret1a2b3c4d")
    await node_client.post("/v1/nodes/register", json=_body())
    node = await _load_node(node_session, "ret1a2b3c4d")
    assert node.licence_version == "2026-07-01"
    assert node.publication == "public"


async def test_a_registration_with_no_publication_choice_resolves_to_public(node_client,
                                                                           accepted_in_mender,
                                                                           node_session):
    accepted_in_mender("ret1a2b3c4d")
    agreements = {k: v for k, v in AGREEMENTS.items() if k != "publication"}
    await node_client.post("/v1/nodes/register", json=_body(agreements=agreements))
    node = await _load_node(node_session, "ret1a2b3c4d")
    assert node.publication == "public"


async def test_every_refusal_class_shares_one_body(node_client, unknown_in_mender,
                                                   pending_in_mender, mender_down):
    unknown_in_mender("retdeadbeef")
    unknown = await node_client.post("/v1/nodes/register", json=_body("retdeadbeef"))
    pending_in_mender("retpending1")
    pending = await node_client.post("/v1/nodes/register", json=_body("retpending1"))
    mender_down()
    down = await node_client.post("/v1/nodes/register", json=_body("retanything1"))
    responses = [unknown, pending, down]
    assert {r.status_code for r in responses} == {403}
    assert len({r.content for r in responses}) == 1
    assert all(r.headers.get("retry-after") for r in responses)


async def test_an_invalid_config_is_a_400_only_after_identity_resolves(node_client,
                                                                      accepted_in_mender,
                                                                      unknown_in_mender):
    accepted_in_mender("ret1a2b3c4d")
    bad = await node_client.post("/v1/nodes/register",
                                 json=_body(config=dict(VALID_CONFIG, rx_lat=999)))
    assert bad.status_code == 400
    assert bad.json() == {"error": "invalid_config", "detail": "rx_lat"}

    unknown_in_mender("retdeadbeef")
    both_wrong = await node_client.post("/v1/nodes/register",
                                        json=_body("retdeadbeef", config=dict(VALID_CONFIG, rx_lat=999)))
    assert both_wrong.status_code == 403


async def test_mender_unreachable_raises_the_alert(node_client, mender_down, alerts):
    mender_down()
    await node_client.post("/v1/nodes/register", json=_body())
    assert any(a[0] == "mender_unreachable" for a in alerts)


async def test_re_registration_revokes_the_previous_token(node_client, accepted_in_mender, alerts):
    accepted_in_mender("ret1a2b3c4d")
    first = (await node_client.post("/v1/nodes/register", json=_body())).json()["token"]
    second = (await node_client.post("/v1/nodes/register", json=_body())).json()["token"]
    assert second != first
    stale = await node_client.post("/v1/nodes/heartbeat",
                                   headers={"Authorization": f"Bearer {first}"},
                                   json={"state": "streaming", "uptime_s": 1, "config_version": 1})
    assert stale.status_code == 401
    assert any(a[0] == "registration_held" for a in alerts)


async def test_re_registration_keeps_the_same_node_ref_and_config_version(node_client,
                                                                         accepted_in_mender):
    accepted_in_mender("ret1a2b3c4d")
    first = (await node_client.post("/v1/nodes/register", json=_body())).json()
    second = (await node_client.post("/v1/nodes/register", json=_body())).json()
    assert first["node_ref"] == second["node_ref"]
    assert first["config_version"] == second["config_version"]
```

The last test matters because `config_version` is returned rather than assumed: a re-registering board must not be told its version is 1 when the server holds 4, or its next frame gets a 409 forever.

Add fixtures to `conftest.py`: `node_client`, `accepted_in_mender` / `pending_in_mender` / `unknown_in_mender` / `mender_down` (each swapping `services.mender._transport`), and `alerts` (capturing `services.alerting.send_alert` calls).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_register.py -v --no-cov`
Expected: FAIL with 404 on every request.

- [ ] **Step 3: Write the request and response models**

`backend/routes/node_schemas.py`, mirroring `nodes_api_v1.yml`. One rule that is a requirement rather than a preference:

```python
class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^ret[0-9a-f]{8}$")
    board_model: str = Field(max_length=64)
    agreements: Agreements
    # Deliberately untyped. A Pydantic model here would 422 on a bad value before
    # the handler runs, putting a config-shaped rejection in front of identity
    # resolution and making the response an oracle for which identities exist.
    config: dict[str, Any]
```

- [ ] **Step 4: Write the endpoint**

```python
REFUSAL_BODY = {"error": "forbidden"}
RETRY_AFTER_BASE_S = 300
RETRY_AFTER_JITTER_S = 60


def _refuse() -> JSONResponse:
    """One body and one jittered Retry-After for every refusal class.

    A Retry-After that varied with the reason would say what the status code
    deliberately does not. The constant-latency guarantee of D32 is not
    implemented here; see the superseded plan.
    """
    retry_after = RETRY_AFTER_BASE_S + secrets.randbelow(2 * RETRY_AFTER_JITTER_S) - RETRY_AFTER_JITTER_S
    return JSONResponse(REFUSAL_BODY, status_code=403, headers={"Retry-After": str(retry_after)})


@router.post("/register")
async def register_node(request: RegisterRequest, session: AsyncSession = Depends(get_async_session)):
    try:
        device = await lookup_device(request.node_id)
    except MenderUnreachable:
        send_alert("mender_unreachable", f"registration for {request.node_id} could not reach Mender")
        return _refuse()
    if device is None or device.auth_status != "accepted":
        return _refuse()

    # Identity has resolved, so a 400 is now safe to return.
    try:
        config = validate_config(request.config)
    except ConfigInvalid as exc:
        return JSONResponse({"error": "invalid_config", "detail": exc.field}, status_code=400)

    node = await _load_node(session, request.node_id)
    if node is None:
        node = Node(node_id=request.node_id, node_ref=mint_node_ref(), board_model=request.board_model)
        session.add(node)
    else:
        send_alert("registration_held", f"{request.node_id} re-registered; previous token revoked")
        await revoke_tokens(session, request.node_id, reason="reflash")

    _apply_agreements(node, request.agreements)
    version = await upsert_config(session, request.node_id, config)
    node.active_config_version = version
    await session.commit()

    await register_with_pipeline(session, node)
    token = await mint_token(session, request.node_id)
    return RegisterResponse(token=token, node_ref=node.node_ref, config_version=version,
                            server_time=_now_z())
```

- [ ] **Step 5: Mount the router**

`from routes.nodes import router as nodes_router` and `app.include_router(nodes_router)` near `main.py:234`. The router carries `prefix="/v1/nodes"`.

- [ ] **Step 6: Run tests, lint, commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend pytest tests/test_node_register.py -v --no-cov
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/routes/nodes.py backend/routes/node_schemas.py backend/main.py backend/tests/test_node_register.py backend/tests/conftest.py
git commit -m "feat(api): POST /v1/nodes/register

Configuration validation runs after identity resolution: a 400 reachable before
it turns the difference between 400 and 403 into an oracle for which identities
exist. RegisterRequest.config is deliberately untyped for the same reason, since
a Pydantic model would 422 ahead of the handler.

Re-registration is allowed rather than gated on operator reactivation, because
this build has no way to trigger a reactivation and the gate would brick any
reflashed board. The takeover exposure that opens is recorded in the plan."
```

---

### Task 8: `PUT /v1/nodes/config`

**Files:**
- Modify: `backend/routes/nodes.py`
- Test: `backend/tests/test_node_config_endpoint.py`

**Interfaces:**
- Produces: `upsert_config(session, node_id, config) -> int`, used by registration and by this endpoint.

- [ ] **Step 1: Write the failing test**

```python
async def test_an_unchanged_config_does_not_create_a_version(registered_node, node_client):
    token, _ = registered_node
    first = await node_client.put("/v1/nodes/config", headers=_auth(token), json=VALID_CONFIG)
    second = await node_client.put("/v1/nodes/config", headers=_auth(token), json=VALID_CONFIG)
    assert first.json()["config_version"] == second.json()["config_version"]


async def test_a_changed_config_creates_the_next_version(registered_node, node_client):
    token, _ = registered_node
    before = (await node_client.put("/v1/nodes/config", headers=_auth(token), json=VALID_CONFIG)).json()
    after = (await node_client.put("/v1/nodes/config", headers=_auth(token),
                                   json=dict(VALID_CONFIG, rx_lat=51.50))).json()
    assert after["config_version"] == before["config_version"] + 1


async def test_null_beam_azimuth_is_not_a_change_against_null(registered_node, node_client):
    token, _ = registered_node
    first = (await node_client.put("/v1/nodes/config", headers=_auth(token), json=VALID_CONFIG)).json()
    again = (await node_client.put("/v1/nodes/config", headers=_auth(token), json=VALID_CONFIG)).json()
    assert first["config_version"] == again["config_version"]


async def test_the_superseded_version_is_stamped(registered_node, node_client, node_session):
    token, node_id = registered_node
    await node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(VALID_CONFIG, rx_lat=51.50))
    rows = await _configs(node_session, node_id)
    assert rows[0].superseded_at is not None
    assert rows[-1].superseded_at is None


async def test_an_invalid_config_is_a_400_naming_the_field(registered_node, node_client):
    token, _ = registered_node
    response = await node_client.put("/v1/nodes/config", headers=_auth(token),
                                     json=dict(VALID_CONFIG, fs_hz=1))
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_config", "detail": "fs_hz"}


async def test_a_revoked_token_is_401(registered_node, node_client, revoke):
    token, node_id = registered_node
    await revoke(node_id)
    response = await node_client.put("/v1/nodes/config", headers=_auth(token), json=VALID_CONFIG)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_a_new_version_reaches_the_pipeline(registered_node, node_client, pipeline_calls):
    token, node_id = registered_node
    await node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(VALID_CONFIG, rx_lat=51.50))
    assert pipeline_calls[-1][0] == node_id
```

`test_null_beam_azimuth_is_not_a_change_against_null` is the one that catches the naive implementation. `NULL = NULL` is never true in SQL, so comparing in the database mints a new version on every resend for every broadside node in the fleet, and every one of them then gets `config_stale` forever.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_config_endpoint.py -v --no-cov`
Expected: FAIL with 404.

- [ ] **Step 3: Write `upsert_config` and the endpoint**

```python
_CONFIG_FIELDS = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft", "tx_callsign",
                  "fc_hz", "fs_hz", "beam_width_deg", "beam_azimuth_deg", "max_range_km")


async def upsert_config(session: AsyncSession, node_id: str, config: dict) -> int:
    """Return the active version, creating a new one only if the values differ.

    The comparison is field by field in Python rather than in SQL, because
    NULL = NULL is never true and beam_azimuth_deg is null for every node that is
    not aimed. Comparing in the database mints a version per resend for all of them.
    """
    active = (
        await session.execute(
            select(NodeConfig)
            .where(NodeConfig.node_id == node_id, NodeConfig.superseded_at.is_(None))
            .order_by(NodeConfig.version.desc())
        )
    ).scalars().first()

    if active is not None and all(getattr(active, f) == config[f] for f in _CONFIG_FIELDS):
        return active.version

    now = datetime.now(UTC)
    version = 1 if active is None else active.version + 1
    if active is not None:
        active.superseded_at = now
    session.add(NodeConfig(node_id=node_id, version=version, **{f: config[f] for f in _CONFIG_FIELDS}))
    await session.commit()
    return version


@router.put("/config")
async def put_config(payload: dict[str, Any], node_id: str = Depends(bearer_node_dep),
                     session: AsyncSession = Depends(get_async_session)):
    try:
        config = validate_config(payload)
    except ConfigInvalid as exc:
        return JSONResponse({"error": "invalid_config", "detail": exc.field}, status_code=400)
    version = await upsert_config(session, node_id, config)
    node = await _load_node(session, node_id)
    node.active_config_version = version
    await session.commit()
    await register_with_pipeline(session, node)
    return ConfigResponse(config_version=version)
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend pytest tests/test_node_config_endpoint.py -v --no-cov
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/routes/nodes.py backend/tests/test_node_config_endpoint.py
git commit -m "feat(api): PUT /v1/nodes/config with real versioning

Version comparison happens field by field in Python rather than in SQL. NULL =
NULL is never true, beam_azimuth_deg is null for every node that is not aimed,
and comparing in the database would mint a version per resend for all of them
and then report config_stale to each of them forever."
```

---

### Task 9: The pipeline handoff

**Files:**
- Create: `backend/services/node_pipeline.py`
- Test: `backend/tests/test_node_pipeline.py`

**Interfaces:**
- Produces: `async register_with_pipeline(session, node) -> None` and `submit_frame(node_id, frame) -> bool`.

This is where "the numbers reach the server" becomes "the numbers reach the map". A v1 node has to look to the pipeline exactly like a `blah2_bridge` node, which means the same three calls at `services/blah2_bridge.py:215-232` and the same queue push at line 314.

Solver correctness is not in scope. If a frame lands in `state.frame_queue` under the right `node_id`, this task is done. Do not extend it to chase a good fix.

- [ ] **Step 1: Write the failing test**

```python
async def test_a_registered_node_appears_in_connected_nodes(node_session, seeded_node):
    await register_with_pipeline(node_session, seeded_node)
    entry = state.connected_nodes["ret1a2b3c4d"]
    assert entry["status"] == "active"
    assert entry["is_synthetic"] is False
    assert entry["config"]["rx_lat"] == 51.42


async def test_registration_reaches_analytics_and_the_associator(node_session, seeded_node, spies):
    await register_with_pipeline(node_session, seeded_node)
    assert spies.analytics == [("ret1a2b3c4d",)]
    assert spies.associator == [("ret1a2b3c4d",)]


async def test_the_pipeline_config_carries_the_defaults_blah2_bridge_supplies(node_session, seeded_node):
    await register_with_pipeline(node_session, seeded_node)
    config = state.connected_nodes["ret1a2b3c4d"]["config"]
    assert config["doppler_min"] == -300
    assert config["doppler_max"] == 300
    assert config["min_doppler"] == 15


async def test_a_frame_reaches_the_queue_under_its_node_id():
    assert submit_frame("ret1a2b3c4d", {"timestamp": 1753900000123, "delay": [1.0],
                                        "doppler": [2.0], "snr": [3.0]}) is True
    node_id, frame = state.frame_queue.get_nowait()
    assert node_id == "ret1a2b3c4d"
    assert frame["_node_id"] == "ret1a2b3c4d"


def test_a_full_queue_drops_the_frame_and_bumps_the_counter(full_queue):
    before = state.get_counter("frames_dropped")
    assert submit_frame("ret1a2b3c4d", {"timestamp": 1, "delay": [], "doppler": [], "snr": []}) is False
    assert state.get_counter("frames_dropped") == before + 1


async def test_re_registering_updates_the_config_rather_than_duplicating(node_session, seeded_node):
    await register_with_pipeline(node_session, seeded_node)
    seeded_node.active_config_version = 2
    await register_with_pipeline(node_session, seeded_node)
    assert len([k for k in state.connected_nodes if k == "ret1a2b3c4d"]) == 1
```

`test_the_pipeline_config_carries_the_defaults_blah2_bridge_supplies` exists because the v1 wire config has no `doppler_min`, `doppler_max` or `min_doppler`, and the pipeline expects them. `services/blah2_bridge.py:66-68` holds the defaults; take them from there rather than inventing values.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_pipeline.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""Make a v1 node indistinguishable from a blah2_bridge node to the pipeline.

The bridge is the working reference: services/blah2_bridge.py registers a node
with three calls and pushes frames onto one queue. This does the same, so a v1
node reaches the map without anything downstream knowing the difference.
"""

import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import state
from core.nodes import NodeConfig

logger = logging.getLogger(__name__)

# The pipeline expects three fields the v1 wire config does not carry.
# Values copied from services/blah2_bridge.py:66-68 rather than invented.
_PIPELINE_DEFAULTS = {"doppler_min": -300, "doppler_max": 300, "min_doppler": 15}

_WIRE_FIELDS = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft",
                "fc_hz", "fs_hz", "beam_width_deg", "max_range_km")


async def _pipeline_config(session: AsyncSession, node_id: str) -> dict:
    row = (
        await session.execute(
            select(NodeConfig).where(NodeConfig.node_id == node_id, NodeConfig.superseded_at.is_(None))
        )
    ).scalars().first()
    if row is None:
        raise ValueError(f"{node_id} has no active configuration")
    config = {field: getattr(row, field) for field in _WIRE_FIELDS}
    config.update(_PIPELINE_DEFAULTS)
    return config


async def register_with_pipeline(session: AsyncSession, node) -> None:
    config = await _pipeline_config(session, node.node_id)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    with state.connected_nodes_lock:
        state.connected_nodes[node.node_id] = {
            "config_hash": config_hash,
            "config": config,
            "status": "active",
            "last_heartbeat": "",
            "peer": "v1",
            "is_synthetic": False,
            "capabilities": {"adsb_report": True},
        }
    state.node_analytics.register_node(node.node_id, config)
    state.node_associator.register_node(node.node_id, config)
    logger.info("node_api: registered %s with the pipeline", node.node_id)


def submit_frame(node_id: str, frame: dict) -> bool:
    """Push one frame. False means the queue was full and the frame was dropped."""
    frame["_node_id"] = node_id
    try:
        state.frame_queue.put_nowait((node_id, frame))
    except Exception:
        state.bump_counter("frames_dropped")
        return False
    return True
```

- [ ] **Step 4: Run tests, lint, commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend pytest tests/test_node_pipeline.py -v --no-cov
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/services/node_pipeline.py backend/tests/test_node_pipeline.py
git commit -m "feat(nodes): hand v1 nodes to the pipeline the way blah2_bridge does

Same three registration calls and the same queue push, so nothing downstream
knows the difference. The three pipeline fields the v1 wire config does not
carry take blah2_bridge's own defaults rather than invented ones."
```

---

### Task 10: `POST /v1/nodes/detection` and `POST /v1/nodes/heartbeat`

**Files:**
- Modify: `backend/routes/nodes.py`, `backend/main.py:191-211`
- Create: `backend/services/node_rate_limits.py`
- Test: `backend/tests/test_node_streaming.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_a_frame_is_accepted_whole_and_reaches_the_queue(registered_node, node_client):
    token, node_id = registered_node
    response = await node_client.post("/v1/nodes/detection", headers=_auth(token), json=_frame())
    assert response.status_code == 202
    assert response.json() == {"accepted": 2, "config_stale": False, "streaming_allowed": True}
    queued_id, frame = state.frame_queue.get_nowait()
    assert queued_id == node_id
    assert frame["delay"] == [12.4, 30.1]


async def test_an_empty_frame_is_valid(registered_node, node_client):
    token, _ = registered_node
    response = await node_client.post("/v1/nodes/detection", headers=_auth(token),
                                      json=_frame(delay=[], doppler=[], snr=[], adsb_hex=[]))
    assert response.status_code == 202
    assert response.json()["accepted"] == 0


async def test_parallel_arrays_of_different_lengths_are_rejected(registered_node, node_client):
    token, _ = registered_node
    response = await node_client.post("/v1/nodes/detection", headers=_auth(token), json=_frame(snr=[1.0]))
    assert response.status_code == 422


async def test_an_unknown_config_version_is_409(registered_node, node_client):
    token, _ = registered_node
    response = await node_client.post("/v1/nodes/detection", headers=_auth(token),
                                      json=_frame(config_version=99))
    assert response.status_code == 409
    assert response.json() == {"error": "unknown_config_version"}


async def test_the_frame_carries_no_node_identifier(registered_node, node_client):
    token, _ = registered_node
    response = await node_client.post("/v1/nodes/detection", headers=_auth(token),
                                      json=_frame(node_ref="nde000000000000"))
    assert response.status_code == 422


async def test_a_blocked_node_is_told_to_pause_rather_than_refused(registered_node, node_client,
                                                                   set_status):
    token, node_id = registered_node
    await set_status(node_id, "blocked")
    response = await node_client.post("/v1/nodes/detection", headers=_auth(token), json=_frame())
    assert response.status_code == 202
    assert response.json()["streaming_allowed"] is False


async def test_the_per_token_limit_fires(registered_node, node_client):
    token, _ = registered_node
    statuses = [
        (await node_client.post("/v1/nodes/detection", headers=_auth(token), json=_frame())).status_code
        for _ in range(60)
    ]
    assert 429 in statuses


async def test_a_heartbeat_returns_the_whole_downlink(registered_node, node_client):
    token, _ = registered_node
    response = await node_client.post("/v1/nodes/heartbeat", headers=_auth(token),
                                      json={"state": "starting", "uptime_s": 3, "config_version": 1})
    assert response.status_code == 200
    assert set(response.json()) == {"server_time", "config_stale", "streaming_allowed", "node_ref"}


async def test_a_stale_config_version_is_reported_on_both_endpoints(registered_node, node_client):
    token, _ = registered_node
    beat = await node_client.post("/v1/nodes/heartbeat", headers=_auth(token),
                                  json={"state": "streaming", "uptime_s": 9, "config_version": 1})
    await node_client.put("/v1/nodes/config", headers=_auth(token), json=dict(VALID_CONFIG, rx_lat=51.50))
    stale = await node_client.post("/v1/nodes/heartbeat", headers=_auth(token),
                                   json={"state": "streaming", "uptime_s": 9, "config_version": 1})
    assert beat.json()["config_stale"] is False
    assert stale.json()["config_stale"] is True


async def test_the_heartbeat_stamps_last_seen_and_the_node_list(registered_node, node_client,
                                                                node_session):
    token, node_id = registered_node
    await node_client.post("/v1/nodes/heartbeat", headers=_auth(token),
                           json={"state": "streaming", "uptime_s": 9, "config_version": 1})
    node = await _load_node(node_session, node_id)
    assert node.last_seen_at is not None
    assert state.connected_nodes[node_id]["last_heartbeat"] != ""


async def test_registration_bodies_are_capped(node_client):
    response = await node_client.post("/v1/nodes/register",
                                      json={"node_id": "ret1a2b3c4d", "board_model": "x" * 9000,
                                            "agreements": AGREEMENTS, "config": VALID_CONFIG})
    assert response.status_code == 413
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_node_streaming.py -v --no-cov`
Expected: FAIL with 404.

- [ ] **Step 3: Write the models with the parallel-array check**

In `node_schemas.py`, a `model_validator(mode="after")` on `DetectionFrame` asserting `len(delay) == len(doppler) == len(snr) == len(adsb_hex)`, and `extra="forbid"` so a `node_ref` in the body is a 422. The frame deliberately carries no identifier: the token resolves to a node, and a frame that could disagree with its own credential would need a rule for what a mismatch means, and every available rule is wrong.

- [ ] **Step 4: Write the limiter**

`backend/services/node_rate_limits.py`: a fixed-window counter keyed on `(node_id, endpoint)`. 8 per second for detections, which is a small multiple of the 2 Hz constant. 30 per minute each for heartbeat and config, which are both needed precisely when a node is in trouble. Bound the map by expected fleet size and log rather than evict, since flushing a victim's counter is the same as clearing it.

- [ ] **Step 5: Write the two endpoints**

Detection: resolve the bearer, check the limit, compare `config_version` against `node.active_config_version` and 409 on a mismatch, build the pipeline frame (`timestamp` in milliseconds from `t`, `delay`, `doppler`, `snr`), call `submit_frame`, and return the ack with `streaming_allowed = (node.status == "active")`. Nothing on this path writes to `nodes` or `node_configs`.

Heartbeat: stamp `nodes.last_seen_at` and `node_tokens.last_used_at`, update `state.connected_nodes[node_id]["last_heartbeat"]`, and return `server_time`, `config_stale`, `streaming_allowed` and `node_ref`.

- [ ] **Step 6: Extend the body cap middleware**

`LimitUploadSize` at `main.py:191` caps globally. Give it a per-path limit: 8 KiB for register, heartbeat and config, 64 KiB for detection, read from `Content-Length` before the body is.

- [ ] **Step 7: Run the whole suite, lint, commit**

```bash
cd ~/owl/Tower-Finder
uv run --directory backend pytest tests/ -q
uv run --directory backend ruff check . && uv run --directory backend ruff format .
git add backend/routes/nodes.py backend/routes/node_schemas.py backend/services/node_rate_limits.py backend/main.py backend/tests/test_node_streaming.py
git commit -m "feat(api): POST /v1/nodes/detection and /v1/nodes/heartbeat

The detection path writes nothing to the database: it resolves a token, checks
the config version and pushes onto the frame queue. The frame carries no node
identifier at all, so there is no case where it can disagree with its own
credential."
```

---

### Task 11: The Cloudflare origin boundary

**Files:**
- Create: `deploy/nginx/snippets/cloudflare.conf`
- Modify: `deploy/nginx/nginx.conf.template`, `deploy/setup-server.sh:52-59`
- Test: manual, on staging first

Until this lands the per-IP limiter buckets the whole internet into one edge address, and both the edge and application limits are bypassed by addressing the droplet directly. The droplet addresses are not secret.

**The origin certificate already exists.** `deploy/setup-server.sh:167` requires `/etc/ssl/cloudflare/{cert,key}.pem` and refuses to run without it, so the certificate half is done. What is missing is real client IPs, authenticated origin pulls, and a firewall that is not open to the world on 80 and 443.

**Ordering is load-bearing.** Enable Authenticated Origin Pulls in the Cloudflare dashboard for the zone *before* turning on `ssl_verify_client`, or every request 400s at the origin. Do the whole task on `retina-staging` first and leave it running for a day before touching prod.

- [ ] **Step 1: Fetch Cloudflare's ranges and the origin-pull CA**

```bash
curl -fsS https://www.cloudflare.com/ips-v4 -o /tmp/cf-v4.txt
curl -fsS https://www.cloudflare.com/ips-v6 -o /tmp/cf-v6.txt
curl -fsS https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem -o /tmp/cf-origin-pull-ca.pem
wc -l /tmp/cf-v4.txt /tmp/cf-v6.txt
```

These lists change. Note the date fetched in the commit message, and treat a stale list as an outage waiting to happen.

- [ ] **Step 2: Write the snippet**

`deploy/nginx/snippets/cloudflare.conf`, generated from the two lists:

```nginx
set_real_ip_from 173.245.48.0/20;
set_real_ip_from 103.21.244.0/22;
# ... one line per range from ips-v4 and ips-v6 ...
real_ip_header CF-Connecting-IP;
real_ip_recursive on;
```

Without `real_ip_header`, every request appears to come from a Cloudflare address and any per-IP limit buckets the entire internet into one client.

- [ ] **Step 3: Include it, and gate origin pulls behind an environment flag**

In `nginx.conf.template`, include the snippet in each `server` block that serves the API, and add origin pulls behind a flag so the laptop stack, which runs `TLS_ENABLED=false`, is unaffected:

```nginx
ssl_client_certificate /etc/ssl/cloudflare/origin-pull-ca.pem;
ssl_verify_client ${ORIGIN_PULLS_VERIFY};
```

with `ORIGIN_PULLS_VERIFY` defaulting to `off` and set to `on` per droplet.

- [ ] **Step 4: Install the CA on staging and turn the dashboard setting on first**

```bash
scp /tmp/cf-origin-pull-ca.pem retina-staging:/etc/ssl/cloudflare/origin-pull-ca.pem
ssh retina-staging 'chmod 644 /etc/ssl/cloudflare/origin-pull-ca.pem'
```

Then enable Authenticated Origin Pulls for the zone in the Cloudflare dashboard. Only after that, set `ORIGIN_PULLS_VERIFY=on` in the staging `.env` and restart.

- [ ] **Step 5: Verify from both sides**

```bash
curl -fsS https://staging-api.retina.fm/healthz && echo "through the edge: ok"
curl -fsS --max-time 5 https://<staging-droplet-ip>/healthz \
  && echo "DIRECT ACCESS STILL WORKS, task not done" \
  || echo "direct access refused: ok"
```

Then confirm real client IPs are reaching the application, not Cloudflare's:

```bash
ssh retina-staging 'docker compose -f /opt/retina-server/docker-compose.yml logs --tail=20 nginx | grep -o "^[0-9a-f.:]*"'
```

- [ ] **Step 6: Narrow the firewall**

Replace the blanket rules at `deploy/setup-server.sh:57-58` with per-range allows generated from the two lists, keeping `22/tcp` open:

```bash
ufw allow 22/tcp
while read -r cidr; do [ -n "$cidr" ] && ufw allow from "$cidr" to any port 443 proto tcp; done < cf-v4.txt
while read -r cidr; do [ -n "$cidr" ] && ufw allow from "$cidr" to any port 443 proto tcp; done < cf-v6.txt
```

Check before applying whether anything reaches the droplet directly on 80 or 443: deploy scripts, uptime checks, `retnode_poller.py`, or the `:3012` TCP path. Port 3012 is a separate ufw rule and stays as it is, since `blah2_bridge` and the legacy path are deliberately still running.

- [ ] **Step 7: Repeat on prod, then commit**

```bash
cd ~/owl/Tower-Finder
git add deploy/nginx/snippets/cloudflare.conf deploy/nginx/nginx.conf.template deploy/setup-server.sh
git commit -m "feat(deploy): make the origin reachable only through Cloudflare

Without real_ip_header the per-IP limiter buckets the whole internet into one
edge address, and without the firewall narrowing both the edge and application
limits are bypassed by addressing the droplet, whose address is not secret.

Origin pull verification is behind ORIGIN_PULLS_VERIFY so the laptop stack, which
runs without TLS, is unaffected. Cloudflare's ranges were fetched on 2026-08-06;
they change, and a stale list is an outage waiting to happen."
```

---

## Self-review

**Coverage against the goal.** A node registers (task 7), sends its configuration (task 8), heartbeats and streams (task 10), its frames reach `state.frame_queue` under its own `node_id` (task 9), and the origin is only reachable through Cloudflare (task 11). Alembic and `PUT /config` are kept in full as asked.

**What is specified by rule and test rather than by literal code.** The detection and heartbeat handler bodies (task 10 step 5), the rate limiter (task 10 step 4), the per-path body cap (task 10 step 6), and the `cloudflare.conf` range list, which is generated rather than written. The tests for each are concrete, which is what pins the behaviour, but these are the four places where an implementer is writing rather than transcribing.

**Two things this plan assumes about the existing code.** That `state.frame_queue` consumers tolerate a frame with no `adsb` key, which `_convert_frame` only sets when the source had one, so the shape is already optional. And that `state.node_analytics.register_node` and `state.node_associator.register_node` are safe to call again for a node already registered, which task 9's last test checks rather than assumes.

**Known exposure, deliberately taken.** Re-registration is not gated on operator reactivation (task 7), so anyone who can enrol a Mender key under a known `node_id` can take a node over. The alert fires on every re-registration, which makes it visible rather than silent, and putting the gate back means building the reactivation subcommand first.
