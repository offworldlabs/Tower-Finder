"""Centralised constants shared across the RETINA backend.

Import from here instead of scattering magic numbers through services.
Values that are tunable per-deployment stay as env vars (FRAME_WORKERS,
SOLVER_WORKERS, etc.) — this file is for compile-time constants only.

retina_tracker YAML config stays separate (loaded at runtime via config.yaml).
"""

# ── Physics ──────────────────────────────────────────────────────────────────
C_M_S = 299_792_458.0                  # Speed of light (m/s)
C_KM_US = 0.299792458                  # Speed of light (km/µs)
R_EARTH_KM = 6371.0                    # Mean Earth radius (km)
FT_TO_M = 0.3048                       # Feet → metres

# ── Association gates ────────────────────────────────────────────────────────
DELAY_GATE_US = 5.0                    # Max bistatic delay mismatch (µs)
DOPPLER_GATE_HZ = 30.0                 # Max Doppler mismatch (Hz)
DELAY_MATCH_THRESHOLD_US = 15.0        # Bistatic delay tolerance for matching
ASSOC_GRID_STEP_KM = 3.0              # Overlap zone grid resolution (km)
ASSOC_MIN_INTERVAL_S = 30.0           # Per-node association rate limit (s)
ASSOC_MAX_NEIGHBORS = 50              # CPU budget cap for neighbor checks

# ── Default antenna parameters ───────────────────────────────────────────────
YAGI_BEAM_WIDTH_DEG = 41.0            # Default half-power beamwidth (°)
YAGI_MAX_RANGE_KM = 50.0             # Default Yagi max range (km)

# ── Track & history limits ───────────────────────────────────────────────────
TRACK_HISTORY_MAX = 60                # Rolling position buffer per aircraft
GROUND_TRUTH_MAX = 120                # Ground truth trail length
ANOMALY_LOG_MAX = 500                 # Max anomaly log entries

# ── Flush & refresh intervals (seconds) ──────────────────────────────────────
AIRCRAFT_FLUSH_INTERVAL_S = 1.0       # aircraft.json write cadence
ANALYTICS_REFRESH_INTERVAL_S = 30     # Background analytics recompute
# Detection archive batch write — one Parquet file per node per hour.
# At ~1 fps that's ~3600 frames/hour ≈ 300 KB zstd Parquet, which is the
# minimum size we want for analytics queries; smaller files (the previous
# 30 s cadence) create the small-files problem at scale.
ARCHIVE_FLUSH_INTERVAL_S = 3600
ARCHIVE_BATCH_MAX = 10000             # Safety cap; should not normally trigger
TRACK_ARCHIVE_FLUSH_INTERVAL_S = 60   # Multi-node solver track archive flush cadence

# ── Archive lifecycle (R2 offload + local disk cleanup) ──────────────────────
ARCHIVE_OFFLOAD_AGE_DAYS = 1          # Upload to R2 after this many days
# Set to 0 (or any value <= 0) to disable local-disk deletion entirely.
# R2 retains everything indefinitely, so this controls only the local cache.
ARCHIVE_RETENTION_DAYS = 0            # 0 = never delete locally
ARCHIVE_LIFECYCLE_INTERVAL_S = 3600   # Run lifecycle check every hour

# ── Reputation thresholds ────────────────────────────────────────────────────
TRUST_WARN_THRESHOLD = 0.3            # Trust score warning level
TRUST_BLOCK_THRESHOLD = 0.1           # Trust score block level
REPUTATION_BLOCK_THRESHOLD = 0.2      # Reputation block level

# ── Geolocation solver ───────────────────────────────────────────────────────
GEO_INTERVAL_S = 10.0                 # Per-track solver rate limit (seconds)
PRUNE_INTERVAL_S = 60.0               # Stale-entry pruning interval (seconds)
STALE_TRACK_S = 120.0                 # Remove tracks not updated in this window

# ── Target classification (drone detection) ──────────────────────────────────
DRONE_ALTITUDE_BOUNDS = [0, 500]       # metres ASL
DRONE_VELOCITY_BOUNDS = [-60, 60]      # m/s per component
DRONE_INITIAL_ALT_M = 80              # Solver initial guess altitude (m)
DRONE_MAX_SPEED_MS = 60.0             # Drone classification speed threshold (m/s)
DRONE_MAX_ALT_M = 600.0               # Drone classification altitude threshold (m)

# ── Frame processor cadences ─────────────────────────────────────────────────
ARC_REFRESH_S = 5.0                   # Detection arc recompute cadence (s)
GT_REFRESH_S = 5.0                    # Ground-truth snapshot refresh cadence (s)

# ── Periodic task intervals ──────────────────────────────────────────────────
REPUTATION_INTERVAL_S = 60            # Reputation evaluator sleep (s)
ADSB_TRUTH_INTERVAL_S = 120           # ADS-B truth fetcher sleep (s)
ADSB_BACKOFF_S = 300                  # Rate-limit backoff (s)
OPENSKY_BUFFER_DEG = 1.0              # lat/lon margin for OpenSky bbox (degrees)

# ── Admin / ops ──────────────────────────────────────────────────────────────
EVENT_LOG_MAX = 2000                  # Event log buffer capacity
NODE_OFFLINE_THRESHOLD_S = 120        # Heartbeat timeout → offline (s)
NODE_HEALTH_CHECK_INTERVAL_S = 30     # How often to check node liveness (s)
STORAGE_CACHE_TTL_S = 300.0           # Archive storage stats cache TTL (s)
CONFIG_LIVE_CACHE_TTL_S = 60.0        # Live node/tower config cache TTL (s)

# ── Chain of custody limits ──────────────────────────────────────────────────
CHAIN_ENTRIES_MAX_PER_NODE = 500      # Max chain entries per node (rolling)
IQ_COMMITMENTS_MAX_PER_NODE = 200     # Max IQ commitments per node (rolling)
RATE_BUCKETS_MAX_IPS = 10_000         # Max unique IPs in rate limiter

# ── blah2 bridge ─────────────────────────────────────────────────────────────
BLAH2_POLL_INTERVAL_S = 1.0           # blah2 API poll cadence (s)
BLAH2_STALE_THRESHOLD_S = 10.0        # Ignore frames older than this (s)
BLAH2_RECONNECT_DELAY_S = 5.0         # Backoff after failures (s)
BLAH2_MAX_FAILURES = 5                # Failures before backing off
