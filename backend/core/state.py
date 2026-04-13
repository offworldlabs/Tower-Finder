"""Centralised mutable state shared across modules.

Every global dict / set / queue that multiple parts of the server touch
lives here so imports are unambiguous and circular-dependency-free.
"""

import asyncio
import os
import threading
import time
from collections import defaultdict, deque

from retina_analytics.manager import NodeAnalyticsManager
from retina_analytics.association import InterNodeAssociator
from retina_custody.crypto_backend import SignatureVerifier
from retina_custody.models import NodeIdentity
from config.constants import (
    TRACK_HISTORY_MAX,
    GROUND_TRUTH_MAX,
    ANOMALY_LOG_MAX,
    ASSOC_GRID_STEP_KM,
)

# ── Coverage / analytics persistence ──────────────────────────────────────────
COVERAGE_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data")

# ── Connected node state tracking ─────────────────────────────────────────────
connected_nodes: dict[str, dict] = {}
# node_id → {config_hash, config, status, last_heartbeat, peer, is_synthetic, capabilities}

node_analytics = NodeAnalyticsManager(storage_dir=COVERAGE_STORAGE_DIR)
node_associator = InterNodeAssociator(grid_step_km=ASSOC_GRID_STEP_KM)

# ── Per-node tracker pipelines (lazy-created per connecting node) ─────────────
node_pipelines: dict = {}  # node_id → PassiveRadarPipeline

# ── Pre-aggregated geolocated aircraft (hex → (GeolocatedTrack, config dict))
# Updated incrementally by _run_geolocation() during frame processing so the
# flush task doesn't need to iterate all 915 pipelines × their tracks.
active_geo_aircraft: dict = {}

# ── Multi-node solver results ─────────────────────────────────────────────────
multinode_tracks: dict[str, dict] = {}

# ── ADS-B positions reported inside detection frames ──────────────────────────
adsb_aircraft: dict[str, dict] = {}

# ── Track history: rolling position buffer per aircraft hex ───────────────────
track_histories: dict[str, deque] = {}

# ── Ground truth trails from fleet_orchestrator ──────────────────────────
ground_truth_trails: dict[str, deque] = {}
ground_truth_meta: dict[str, dict] = {}   # hex → {object_type, is_anomalous}

# ── Chain of Custody ──────────────────────────────────────────────────────────
sig_verifier = SignatureVerifier()
node_identities: dict[str, NodeIdentity] = {}
chain_entries: dict[str, list[dict]] = {}   # node_id → append-only list
iq_commitments: dict[str, list[dict]] = {}

# ── Anomaly flagging ─────────────────────────────────────────────────────────
anomaly_log: list[dict] = []               # append-only timestamped anomaly events
anomaly_hexes: set[str] = set()            # hex codes currently flagged as anomalous

# ── External ADS-B truth (OpenSky cache) ──────────────────────────────────────
external_adsb_cache: dict[str, dict] = {}

# ── WebSocket broadcast infrastructure ────────────────────────────────────────
from fastapi import WebSocket  # noqa: E402  (deferred to avoid import loops)
ws_clients: set[WebSocket] = set()       # all aircraft (simulated fleet)
ws_live_clients: set[WebSocket] = set()  # real-node-only aircraft (map.retina.fm)
latest_aircraft_json: dict = {"now": 0, "aircraft": [], "messages": 0}
latest_aircraft_json_bytes: bytes = b'{"now":0,"aircraft":[],"messages":0}'
aircraft_dirty: bool = False
latest_real_aircraft_json_bytes: bytes = b'{"now":0,"aircraft":[],"messages":0}'

# ── Pre-serialized analytics / nodes / overlaps (refreshed by background task)
latest_analytics_bytes: bytes = b'{"nodes":{},"cross_node":{"pair_overlaps":[],"coverage_suggestions":[],"blocked_nodes":[]}}'
latest_analytics_real_bytes: bytes = b'{"nodes":{},"cross_node":{"pair_overlaps":[],"coverage_suggestions":[],"blocked_nodes":[]}}'
latest_nodes_bytes: bytes = b'{"nodes":{},"connected":0,"total":0,"synthetic":0}'
latest_overlaps_bytes: bytes = b'{"overlaps":[],"registered_nodes":[]}'

# ── Async frame queue (TCP → processor) ──────────────────────────────────────
_FRAME_QUEUE_SIZE = int(os.getenv("FRAME_QUEUE_SIZE", "10000"))
frame_queue: asyncio.Queue = asyncio.Queue(maxsize=_FRAME_QUEUE_SIZE)

# ── Background multinode solver queue (frame workers → solver threads) ────────
import queue as _stdlib_queue
# Bounded: if solver threads can't keep up, excess candidates are dropped.
_SOLVER_QUEUE_SIZE = int(os.getenv("SOLVER_QUEUE_SIZE", "200"))
solver_queue: _stdlib_queue.Queue = _stdlib_queue.Queue(maxsize=_SOLVER_QUEUE_SIZE)

# Monotonic counter for dropped frames (useful for monitoring)
frames_dropped: int = 0
frames_processed: int = 0
solver_successes: int = 0
solver_failures: int = 0

# ── Thread safety locks ──────────────────────────────────────────────────────
connected_nodes_lock = threading.Lock()
geo_aircraft_lock = threading.Lock()
anomaly_lock = threading.Lock()

# ── Task health tracking ─────────────────────────────────────────────────────
task_last_success: dict[str, float] = {}   # task_name → last success epoch
task_error_counts: dict[str, int] = defaultdict(int)  # task_name → cumulative errors

# ── Accuracy tracking (haversine solver vs ADS-B) ────────────────────────────
# Rolling buffer of {hex, error_km, position_source, ts} samples.
ACCURACY_MAX_SAMPLES = 5000
accuracy_samples: deque = deque(maxlen=ACCURACY_MAX_SAMPLES)

# Pre-serialised accuracy stats (refreshed by background task alongside analytics)
latest_accuracy_bytes: bytes = b'{}'

# Pre-serialised radar3 solver verification (refreshed by background task)
latest_radar3_verification_bytes: bytes = b'{}'

# ── Rate limiter buckets ──────────────────────────────────────────────────────
rate_buckets: dict[str, list] = defaultdict(list)

# ── Simulation physics config (read by fleet orchestrator, written by UI) ─────
simulation_config: dict = {
    "frac_anomalous": 0.05,
    "frac_drone": 0.10,
    "frac_dark": 0.15,
    # aircraft (commercial) fraction = 1 - sum of above
    "max_range_km": 140,
    "min_aircraft": 60,
    "max_aircraft": 100,
    "_updated_at": 0.0,
}
