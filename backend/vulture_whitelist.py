"""Vulture dead-code whitelist.

Every name listed here is referenced dynamically — by FastAPI routing
machinery, Pydantic deserialization, Starlette middleware protocol, or by
tests that exercise the public API.  The gate runs with --min-confidence 60
so these suppressions are necessary to avoid false positives.

Add a name here only when you are certain it is NOT dead code but vulture
cannot see the usage statically.  Real dead code should be deleted, not
whitelisted.
"""
# ruff: noqa: B018, F821
# B018 — intentional bare-name expressions; this is how vulture whitelists work.
# F821 — names are defined in other modules; this file is only read by vulture.


# ── A note on discipline ──────────────────────────────────────────────────────
# (The "intentional named literals for future wiring" that used to sit here
# were dead config laundered through this whitelist — deleted, per this
# file's own preamble.  ANALYTICS_REFRESH_INTERVAL_S is now wired;
# ASSOC_MIN_INTERVAL_S is passed by core/state.)

# ── core/types.py ─────────────────────────────────────────────────────────────

# TypedDict definitions — used as type annotations; fields are accessed via
# dict keys at runtime, not attribute access, so vulture misses the usage.
NodeState
_.capabilities
AircraftPosition
_.hex
_.baro_rate
_.squawk
_.rssi
GeoAircraft
_.flight
_.alt_geom
_.multi_node
_.anomaly
TaskHealth
_.last_success
_.error_counts

# ── pipeline/passive_radar.py ─────────────────────────────────────────────────

# EventWriter public API — tested in tests/test_pipeline.py

# Track / Pipeline attributes SET internally; may be read by external
# inspection tools, serialisers, or future instrumentation.
_.last_update_ms
_._frame_count

# geo_config fields — SET on the retina_geolocator SolverConfig object;
# retina_geolocator reads them during solve_track().
_.velocity_bounds
_.initial_altitude_m

# ── Pydantic v2 model config ──────────────────────────────────────────────────

# model_config is consumed by Pydantic internals; vulture cannot see this.
_.model_config

# ── ASGI / Starlette middleware ───────────────────────────────────────────────

# dispatch() is the required override entry-point for BaseHTTPMiddleware.
_.dispatch

# ── core/users.py ─────────────────────────────────────────────────────────────

# fastapi-users schemas — UserRead/UserUpdate are the public API shapes
# consumed by fastapi-users' router factory and the OpenAPI schema.
UserRead
UserUpdate
# Class-level attributes on UserManager; fastapi-users reads them via
# class introspection, not direct assignment.
_.reset_password_token_secret
_.verification_token_secret
# fastapi_users is the top-level registry object; its sub-routers are
# mounted in main.py via fastapi_users.get_*_router() calls.
fastapi_users
# SQLAlchemy model attributes — accessed via ORM column descriptors,
# not direct attribute lookups that vulture can trace.

# ── services/storage.py ───────────────────────────────────────────────────────

# `tag` is a keyword-only parameter on archive_detections(); kept for
# callsite compatibility with callers that pass tag= explicitly.
tag

# ── core/users.py ─────────────────────────────────────────────────────────────

# Registered with SQLAlchemy via @event.listens_for(engine.sync_engine, "connect").
# Vulture doesn't follow the decorator's dynamic dispatch.
_set_sqlite_pragmas

# ── migrations/env.py ─────────────────────────────────────────────────────────

# The same @event.listens_for dispatch as _set_sqlite_pragmas above, on the
# engine Alembic runs migrations through. That engine takes the application's
# lock tolerance but deliberately not its foreign-key pragma, so it registers
# its own listener rather than reusing core.users'.
_set_busy_timeout

# ── services/tasks/solver.py ──────────────────────────────────────────────────

# Public alias consumed by scripts/association_bench.py (scripts/ is excluded
# from the vulture scan) — the bench resolves chi2 through the worker's own
# code path rather than a copy.
resolve_n2_chi2

# ── routes/node_schemas.py ──────────────────────────────────────────────────

# Pydantic model fields, read by Pydantic's own validation and serialisation
# rather than by any attribute access vulture can trace. These are the wire
# contract at contracts/nodes-v1.openapi.yaml expressed in Python: a field
# deleted because it looks unreferenced is a field the node sends and the
# server then rejects. The handlers that read them land with 86cb2cy3v.
#
# Only the names not already listed above appear here, and only those the four
# endpoints' request and response bodies actually carry.
remote_management
server_time
seq
accepted
config_stale
streaming_allowed
disk_free_mb
temp_c
blah2
owl_os
retina_node
blah2_image
uptime_s
detail
# The two model hooks are called by Pydantic during validation and
# serialisation, through the decorator rather than from a call site.
_arrays_are_parallel
_omit_absent_detail

# ── core/nodes.py ───────────────────────────────────────────────────────────

# SQLAlchemy's declarative mapper reads these class-level attributes to build
# the table metadata; that metadata is what create_all and the autogenerate
# comparison in migrations/env.py both consume, so vulture cannot see the
# usage statically. These are columns of a table that already exists, not
# speculative config kept alive through this file. The consumers (token
# minting, node registration, the config endpoint) land in later tickets.
NODE_STATUSES
node_ref
board_model
licence_version
licence_accepted_at
remote_management_version
remote_management_accepted_at
publication_version
publication_chosen_at
first_seen_at
rx_alt_ft
tx_alt_ft
tx_callsign
superseded_at
token_hash
issued_at
revoked_at
revoked_reason
last_used_at

# ── migrations/versions/*.py ──────────────────────────────────────────────

# Alembic loads each revision module by file path at runtime and reads these
# module-level names and the two functions reflectively (revision identity,
# chain order, upgrade/downgrade direction); nothing in the tree references
# them statically. Vulture whitelists match by name rather than by file, so
# these entries also cover every revision added in future, and the alternative
# is a whitelist entry per migration file, forever.
revision
down_revision
branch_labels
depends_on
upgrade
downgrade
# rollback_safety is read by deploy/classify-migration-gap.py via regex to
# determine whether a rollback across this revision is safe to serve.
rollback_safety


# Module-level counter incremented only through the string-keyed
# state.bump_counter("adsb_seed_frames_autotagged") in
# services/frame_processor.py, which vulture cannot resolve to this name.
adsb_seed_frames_autotagged

# Same string-keyed bump_counter shape, from services/known_claiming.py and
# frame_processor's binding-mode block.
known_claims_made
known_claim_contentions
known_claims_bound


# ── Framework attributes (previously CI --ignore-names) ───────────────────────
# Moved out of the vulture invocation so the reason lives with the name.
# `model_config` is the pydantic v2 class-level config attribute, read by
# pydantic itself. `dispatch` is the Starlette BaseHTTPMiddleware hook, called
# by the middleware stack. Neither has a call site in our code.
model_config
_.dispatch
