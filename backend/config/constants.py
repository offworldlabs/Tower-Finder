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
ARCHIVE_FLUSH_INTERVAL_S = 30         # Detection archive batch write
ARCHIVE_BATCH_MAX = 200               # Max frames per archive flush

# ── Reputation thresholds ────────────────────────────────────────────────────
TRUST_WARN_THRESHOLD = 0.3            # Trust score warning level
TRUST_BLOCK_THRESHOLD = 0.1           # Trust score block level
REPUTATION_BLOCK_THRESHOLD = 0.2      # Reputation block level
