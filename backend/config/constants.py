"""Centralised constants shared across the RETINA backend.

Import from here instead of scattering magic numbers through services.
Values that are tunable per-deployment stay as env vars (FRAME_WORKERS,
SOLVER_WORKERS, etc.) — this file is for compile-time constants only.

retina_tracker YAML config stays separate (loaded at runtime via config.yaml).
"""

# ── Physics ──────────────────────────────────────────────────────────────────
C_KM_US = 0.299792458  # Speed of light (km/µs)
R_EARTH_KM = 6371.0  # Mean Earth radius (km)
FT_TO_M = 0.3048  # Feet → metres

# ── Association gates ────────────────────────────────────────────────────────
DELAY_MATCH_THRESHOLD_US = 15.0  # Bistatic delay tolerance for matching
ASSOC_GRID_STEP_KM = 3.0  # Overlap zone grid resolution (km)
ASSOC_MIN_INTERVAL_S = 30.0  # Per-node association rate limit (s)
ASSOC_MAX_NEIGHBORS = 50  # CPU budget cap for neighbor checks
# Track pairings emitted per association round when the constant-velocity fit
# is deferred to the solver worker, which is the production configuration.
# Distinct from the associator's inline-fit budget (8): that one bounds ~86 ms
# LM solves, this one bounds queue pressure — a _merge_epochs, a candidate
# carrying those epochs, and a solver-queue slot each.  They were the same
# number by accident, and since the fit budget only decrements when a fit runs,
# on the deferred path it silently capped pairings at 8 per node pair.
ASSOC_MAX_PAIRS_PER_ROUND = 64

# ── n=2 confirmation (track-to-track association) ────────────────────────────
# The frame path pairs confirmed single-node tracks; the solver worker runs the
# constant-velocity fit.
#
# Measured blind, track association takes the n=2-only ghost rate from 92.9% to
# 55.4% at no cost in real tracks.  The fit that makes that work is an ~86 ms LM
# solve, and running it inside the frame worker took staging to 92% frame-queue
# depth with the processor 21 s behind a 6 frame/s feed.  It now runs on the
# solver's own threads, behind its own queue and staleness drop, so the frame
# path keeps only the coarse delay gate — one BLAS contraction per node pair.
#
# This now controls only solver.py's _N2_REQUIRE_CONFIRMED — whether an n=2
# solve must carry a chi2 confirmation before its track is published.  The
# detection-level fallback it used to select has moved out of the production
# import graph entirely (retina_analytics.detection_association, reachable from
# the offline bench), so there is no second path left to switch to.
N2_TRACK_ASSOCIATION = True

# At n=2 a single-epoch solve has 5 unknowns against 4 residuals, so rms_delay
# and rms_doppler go to zero for a cross pairing exactly as for a real target
# and neither can gate it.  A pairing of two *confirmed single-node tracks*
# supplies 4K measurements against 6 unknowns instead, and the constant-velocity
# fit over them produces a real chi2.  See retina_analytics.association.
N2_CONFIRM_CHI2_MAX = 2.0  # chi2/dof ceiling for an n=2 track pairing
N2_CONFIRM_MIN_SPAN_S = 12.0  # Observation span before a pairing is fitted
N2_CONFIRM_MIN_EPOCHS = 4  # Floor on samples; span is the real gate
N2_TRACK_HISTORY_MAX = 20  # Per-node track samples fed to the fit

# Oldest ADS-B fix still usable as a coverage calibration point.  At 250 m/s a
# 10 s fix is 2.5 km stale, which is already coarse against a 5°/72-bin polar
# grid; beyond that the point stops describing where the target was when the
# node detected it.  The frame path used to apply no age gate at all and took
# whatever _fresh_adsb returned — up to 60 s, i.e. 15 km.  See
# services/calibration.py, which both recording sites now go through.
CAL_MAX_ADSB_AGE_S = 10.0

# ── Default antenna parameters ───────────────────────────────────────────────
YAGI_BEAM_WIDTH_DEG = 41.0  # Default half-power beamwidth (°)
YAGI_MAX_RANGE_KM = 50.0  # Default Yagi max range (km)

# ── Track & history limits ───────────────────────────────────────────────────
TRACK_HISTORY_MAX = 60  # Rolling position buffer per aircraft
GROUND_TRUTH_MAX = 120  # Ground truth trail length
ANOMALY_LOG_MAX = 500  # Max anomaly log entries

# ── Flush & refresh intervals (seconds) ──────────────────────────────────────
AIRCRAFT_FLUSH_INTERVAL_S = 1.0  # aircraft.json write + WS broadcast cadence (1 Hz)
ANALYTICS_REFRESH_INTERVAL_S = 30  # Background analytics recompute
# Detection archive batch write — one Parquet file per node per hour.
# At ~1 fps that's ~3600 frames/hour ≈ 300 KB zstd Parquet, which is the
# minimum size we want for analytics queries; smaller files (the previous
# 30 s cadence) create the small-files problem at scale.
ARCHIVE_FLUSH_INTERVAL_S = 3600
ARCHIVE_BATCH_MAX = 10000  # Safety cap; should not normally trigger
TRACK_ARCHIVE_FLUSH_INTERVAL_S = 60  # Multi-node solver track archive flush cadence

# ── Archive lifecycle (R2 offload + local disk cleanup) ──────────────────────
ARCHIVE_OFFLOAD_AGE_DAYS = 1  # Upload to R2 after this many days
# Must be > ARCHIVE_OFFLOAD_AGE_DAYS. Files land in R2 after 1 day, then local
# copies are deleted after 3 days (2-day retry window if R2 upload fails).
ARCHIVE_RETENTION_DAYS = 3  # Delete local copy 3 days after creation
ARCHIVE_LIFECYCLE_INTERVAL_S = 3600  # Run lifecycle check every hour

# ── users.db backup to R2 ────────────────────────────────────────────────────
# Daily snapshot via VACUUM INTO + upload to R2 under backups/users-db/.
# 30 days is the cheapest "sane" retention: enough to recover from a
# realised-too-late deletion (typical "I revoked the wrong invite" scenario
# manifests within a week), short enough that the bucket doesn't grow
# unbounded as the user table itself does.
USERS_DB_BACKUP_INTERVAL_S = 86400  # Once per day
USERS_DB_BACKUP_RETENTION_DAYS = 30  # Keep last N daily snapshots in R2

# ── Geolocation solver ───────────────────────────────────────────────────────
GEO_INTERVAL_S = 10.0  # Per-track solver rate limit (seconds)
PRUNE_INTERVAL_S = 60.0  # Stale-entry pruning interval (seconds)
STALE_TRACK_S = 120.0  # Remove tracks not updated in this window

# How long a track with no fresh detections keeps appearing in aircraft.json.
# Distinct from STALE_TRACK_S, which controls when the tracker *forgets* the
# track entirely.  Single-node detections are intermittent, so dropping a track
# the instant detections pause makes aircraft flicker; but STALE_TRACK_S=120 s
# meant a stationary icon was painted for two minutes while the real aircraft
# flew ~17 km away.  Within this window the position is dead-reckoned from the
# track's own velocity so it keeps pace instead of freezing; past it the entry
# stops rendering while the tracker keeps its state for re-acquisition.
DISPLAY_STALE_TRACK_S = 15.0

# Maximum time the arc speed/RMS gates may hold a track at its last emitted
# position.  The gates exist to absorb a single mis-associated frame; without a
# bound they latch (the reverted position becomes the next frame's reference)
# and freeze the icon indefinitely.  ~3-5 frames: long enough for the intended
# job, short enough to cap the resulting error at speed x 10 s (~1.5 km).
GATE_MAX_HOLD_S = 10.0

# How long a ground-truth trail keeps being served after its last push.
# The simulation orchestrator pushes ground truth every 2 s, so a live
# aircraft's trail tip is never more than ~4 s old.  This was previously
# folded into a shared 300 s constant, which meant an aircraft that despawned
# (lifetime expiry in world.step) stayed on the map as a stationary blue dot
# for five minutes — measured at 3 of 16 dots being such ghosts, with trail
# tips 243-284 s old and exactly 0.00 km of movement.  10 s is five push
# intervals: tolerant of a dropped push or two, without the ghosting.
# Same reasoning as DISPLAY_STALE_TRACK_S, which fixes this for radar tracks.
GT_DISPLAY_STALE_S = 10.0

# How long render trails (track_histories) survive without updates.  Longer on
# purpose: these draw the path *behind* an aircraft, so a generous tail is the
# desired behaviour rather than a staleness bug.
TRAIL_STALE_S = 300.0

# ── Target classification (drone detection) ──────────────────────────────────
DRONE_ALTITUDE_BOUNDS = [0, 500]  # metres ASL
DRONE_VELOCITY_BOUNDS = [-60, 60]  # m/s per component
DRONE_INITIAL_ALT_M = 80  # Solver initial guess altitude (m)
DRONE_MAX_SPEED_MS = 60.0  # Drone classification speed threshold (m/s)
DRONE_MAX_ALT_M = 600.0  # Drone classification altitude threshold (m)

# ── Frame processor cadences ─────────────────────────────────────────────────
ARC_REFRESH_S = 1.0  # Detection arc recompute cadence (s) — keep
# short so a deleted track's arc stops
# being broadcast quickly; otherwise the
# cached entry lingers up to this many
# seconds after the underlying detection
# ends, producing visible ghost arcs.
GT_REFRESH_S = 5.0  # Ground-truth snapshot refresh cadence (s)

# ── Periodic task intervals ──────────────────────────────────────────────────
REPUTATION_INTERVAL_S = 60  # Reputation evaluator sleep (s)
ADSB_TRUTH_INTERVAL_S = 120  # ADS-B truth fetcher sleep (s)
ADSB_BACKOFF_S = 300  # Rate-limit backoff (s)
OPENSKY_BUFFER_DEG = 1.0  # lat/lon margin for OpenSky bbox (degrees)

# ── Admin / ops ──────────────────────────────────────────────────────────────
EVENT_LOG_MAX = 2000  # Event log buffer capacity
NODE_OFFLINE_THRESHOLD_S = 120  # Heartbeat timeout → offline (s)
NODE_HEALTH_CHECK_INTERVAL_S = 30  # How often to check node liveness (s)
STORAGE_CACHE_TTL_S = 300.0  # Archive storage stats cache TTL (s)
CONFIG_LIVE_CACHE_TTL_S = 60.0  # Live node/tower config cache TTL (s)

# ── Chain of custody limits ──────────────────────────────────────────────────
CHAIN_ENTRIES_MAX_PER_NODE = 500  # Max chain entries per node (rolling)
IQ_COMMITMENTS_MAX_PER_NODE = 200  # Max IQ commitments per node (rolling)
RATE_BUCKETS_MAX_IPS = 10_000  # Max unique IPs in rate limiter

# ── blah2 bridge ─────────────────────────────────────────────────────────────
BLAH2_POLL_INTERVAL_S = 1.0  # blah2 API poll cadence (s)
BLAH2_STALE_THRESHOLD_S = 10.0  # Ignore frames older than this (s)
BLAH2_RECONNECT_DELAY_S = 5.0  # Backoff after failures (s)
BLAH2_MAX_FAILURES = 5  # Failures before backing off
