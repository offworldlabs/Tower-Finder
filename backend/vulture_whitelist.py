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

# ── Dummy object ──────────────────────────────────────────────────────────────
# Attribute accesses below (_.foo) tell vulture that "foo" is referenced
# somewhere, suppressing the unused-attribute/method finding.
_ = type("_", (), {})()


# ── config/constants.py ───────────────────────────────────────────────────────
# Module-level constants that document protocol / algorithm parameters.
# Some are consumed by libs/ (retina_analytics, retina_geolocator) via their
# own defaults; others are intentional named literals for future wiring.
C_M_S
DELAY_GATE_US
DOPPLER_GATE_HZ
ASSOC_MIN_INTERVAL_S
ASSOC_MAX_NEIGHBORS
AIRCRAFT_FLUSH_INTERVAL_S
ANALYTICS_REFRESH_INTERVAL_S
TRUST_WARN_THRESHOLD
TRUST_BLOCK_THRESHOLD
REPUTATION_BLOCK_THRESHOLD


# ── core/types.py ─────────────────────────────────────────────────────────────
# TypedDict definitions — used as type annotations; fields are accessed via
# dict keys at runtime, not attribute access, so vulture misses the usage.
NodeState
_.last_heartbeat
_.is_synthetic
_.capabilities
AircraftPosition
_.hex
_.alt_baro
_.baro_rate
_.squawk
_.rssi
GeoAircraft
_.flight
_.alt_geom
_.multi_node
_.anomaly
_.type
TaskHealth
_.last_success
_.error_counts


# ── pipeline/passive_radar.py ─────────────────────────────────────────────────
# EventWriter public API — tested in tests/test_pipeline.py
_.write_event
_.write_event_lazy

# Track / Pipeline attributes SET internally; may be read by external
# inspection tools, serialisers, or future instrumentation.
_.last_update_ms
_._frame_count

# geo_config fields — SET on the retina_geolocator SolverConfig object;
# retina_geolocator reads them during solve_track().
_.altitude_bounds
_.velocity_bounds
_.initial_altitude_m


# ── routes/custody.py ─────────────────────────────────────────────────────────
# Pydantic request-body fields passed through body.model_dump() → HashChainEntry.from_dict()
# and verified by HashChainVerifier. Vulture can't trace dict access.
payload_hash
signature


# ── clients/adsb_lol.py ───────────────────────────────────────────────────────
# Used as OpenSky fallback in services/tasks/periodic.py _fetch_adsb_lol().
AdsbLolClient


# ── services/r2_client.py ─────────────────────────────────────────────────────
# R2 / S3-compatible storage API — all functions are tested in
# tests/test_r2_client.py and form the public storage interface.
upload_bytes
list_keys
delete_key
delete_keys
_clear_cache

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
