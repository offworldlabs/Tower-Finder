"""Centralised mutable state shared across modules.

Every global dict / set / queue that multiple parts of the server touch
lives here so imports are unambiguous and circular-dependency-free.
"""

import asyncio
import os
import threading
from collections import defaultdict, deque

from retina_analytics.association import InterNodeAssociator
from retina_analytics.manager import NodeAnalyticsManager
from retina_custody.crypto_backend import SignatureVerifier
from retina_custody.models import NodeIdentity

from config.constants import (
    ANOMALY_LOG_MAX,  # noqa: F401 — re-exported, used via state.ANOMALY_LOG_MAX
    ASSOC_GRID_STEP_KM,
    ASSOC_MAX_NEIGHBORS,
    ASSOC_MAX_PAIRS_PER_ROUND,
    ASSOC_MIN_INTERVAL_S,
    GROUND_TRUTH_MAX,  # noqa: F401 — re-exported, used via state.GROUND_TRUTH_MAX
    N2_CONFIRM_CHI2_MAX,
    N2_CONFIRM_MIN_EPOCHS,
    N2_CONFIRM_MIN_SPAN_S,
    TRACK_HISTORY_MAX,  # noqa: F401 — re-exported, used via state.TRACK_HISTORY_MAX
)

# ── Coverage / analytics persistence ──────────────────────────────────────────
COVERAGE_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data")

# ── Connected node state tracking ─────────────────────────────────────────────
connected_nodes: dict[str, dict] = {}
# node_id → {config_hash, config, status, last_heartbeat, peer, is_synthetic, capabilities}

node_analytics = NodeAnalyticsManager(storage_dir=COVERAGE_STORAGE_DIR)

def _coverage_limit_for(node_id: str):
    """Shrink-only empirical prior for one node, from accumulated ADS-B fixes.

    Injected rather than imported so retina-analytics keeps knowing nothing
    about NodeAnalyticsManager, the same shape cv_fit used.
    """
    return node_analytics.coverage_limit_for(node_id)


node_associator = InterNodeAssociator(
    grid_step_km=ASSOC_GRID_STEP_KM,
    coverage_provider=_coverage_limit_for,
    # Passed explicitly because ASSOC_MIN_INTERVAL_S was dead config: defined
    # here but never reaching the associator, which used its own hardcoded
    # copy, so tuning it did nothing.
    assoc_interval_s=ASSOC_MIN_INTERVAL_S,
    # No inline fit: the epochs travel with the candidate and the solver
    # worker runs the fit on its own threads.  An 86 ms LM solve on the
    # frame path is frame latency.
    cv_fit=None,
    cv_chi2_max=N2_CONFIRM_CHI2_MAX,
    cv_min_epochs=N2_CONFIRM_MIN_EPOCHS,
    cv_min_span_s=N2_CONFIRM_MIN_SPAN_S,
    # Both were dead config in the same way assoc_interval_s had been: defined
    # here, never passed, with the library falling back to its own value.  The
    # neighbour cap in particular only ever applied on the detection path, so
    # the live one was uncapped.
    max_neighbors=ASSOC_MAX_NEIGHBORS,
    max_pairs_per_round=ASSOC_MAX_PAIRS_PER_ROUND,
)

# ── Per-node tracker pipelines (lazy-created per connecting node) ─────────────
node_pipelines: dict = {}  # node_id → PassiveRadarPipeline

# ── Pre-aggregated geolocated aircraft (hex → (GeolocatedTrack, config dict))
# Updated incrementally by _run_geolocation() during frame processing so the
# flush task doesn't need to iterate all 915 pipelines × their tracks.
active_geo_aircraft: dict = {}

# ── Multi-node solver results ─────────────────────────────────────────────────
multinode_tracks: dict[str, dict] = {}

# Append-only buffer of solve results for the track Parquet stream.
# Drained periodically by services.tasks.track_archive.track_flush_task.
# maxlen guards against runaway growth if the flush task ever stalls.
track_archive_buffer: deque[dict] = deque(maxlen=10000)

# ── ADS-B positions reported inside detection frames ──────────────────────────
adsb_aircraft: dict[str, dict] = {}

# ── Track history: rolling position buffer per aircraft hex ───────────────────
track_histories: dict[str, deque] = {}

# ── Last emitted position per hex, for the speed gate ─────────────────────────
# Updated EVERY emit (not deduped) so the gate's dt reflects actual emit cadence
# rather than the dedup'd history.  Without this, a track that sits at the same
# arc midpoint between sparse tracker updates ages the gate's dt to 20–60 s and
# a 20 km mis-association comes out under the 800 m/s threshold.
# Value: [lat, lon, ts]
track_last_emit: dict[str, list] = {}

# ── Gate hold start per hex ───────────────────────────────────────────────────
# The speed / RMS gates suppress a bad arc midpoint by reverting the emitted
# position to track_last_emit.  That reverted value is then written back into
# track_last_emit with a fresh timestamp, which makes both gates *absorbing*:
# once engaged they compare every future midpoint against a frozen reference
# and never release.  Observed on staging: arc tracks pinned for up to 141 s
# while their aircraft flew on, then a 19.6 km teleport on release.
# This records when a hold first engaged so it can be bounded in time — still
# absorbing the single mis-associated frame the gates exist for, without
# turning that into a permanent freeze.
# Value: (ts, lat, lon) captured when the hold engaged — the anchor the held
# position is dead-reckoned from.  Anchoring rather than extrapolating from
# track_last_emit matters: last_emit is rewritten with the dead-reckoned value
# every frame, so coasting off it would compound the offset.
# Key absent when not held.
track_gate_hold: dict[str, tuple[float, float, float]] = {}

# ── Distinct-position log per track, for gs/heading fallback ──────────────────
# Each entry is appended only when the emitted position moves > ARC_MOTION_MIN_M
# from the last logged point.  Used to recover ground speed for single-node
# ellipse-arc tracks without ADS-B: the LM solver alone can't constrain
# velocity from doppler, so track.speed_knots routinely comes back 0-5 kt and
# the aircraft sits frozen on the map between detections.  Arc-midpoint
# displacement between sparse detections is the only real velocity signal we
# have for those tracks.
# Value: list of (lat, lon, ts), capped at TRACK_MOTION_LOG_MAX entries.
track_arc_motion: dict[str, list] = {}

# ── Ground truth trails from fleet_orchestrator ──────────────────────────
ground_truth_trails: dict[str, deque] = {}
ground_truth_meta: dict[str, dict] = {}  # hex → {object_type, is_anomalous}

# ── Chain of Custody ──────────────────────────────────────────────────────────
sig_verifier = SignatureVerifier()
node_identities: dict[str, NodeIdentity] = {}
chain_entries: dict[str, list[dict]] = {}  # node_id → append-only list
iq_commitments: dict[str, list[dict]] = {}

# ── Anomaly flagging ─────────────────────────────────────────────────────────
anomaly_log: list[dict] = []  # append-only timestamped anomaly events
anomaly_hexes: set[str] = set()  # hex codes currently flagged as anomalous

# ── External ADS-B truth (OpenSky cache) ──────────────────────────────────────
external_adsb_cache: dict[str, dict] = {}

# ── WebSocket broadcast infrastructure ────────────────────────────────────────
from fastapi import WebSocket  # noqa: E402  (deferred to avoid import loops)

ws_clients: set[WebSocket] = set()  # all aircraft (simulated fleet)
ws_live_clients: set[WebSocket] = set()  # real-node-only aircraft (map.retina.fm)
# Per-owner feeds: each authenticated owner connection maps to the set of node
# ids it owns, so broadcast can send a payload filtered to just that owner's
# nodes (true data isolation, not a client-side view filter).
ws_owner_clients: dict[WebSocket, set[str]] = {}
latest_aircraft_json: dict = {"now": 0, "aircraft": [], "messages": 0}
latest_aircraft_json_bytes: bytes = b'{"now":0,"aircraft":[],"messages":0}'
aircraft_dirty: bool = False
latest_real_aircraft_json_bytes: bytes = b'{"now":0,"aircraft":[],"messages":0}'

# ── Pre-serialized analytics / nodes / overlaps (refreshed by background task)
latest_analytics_bytes: bytes = (
    b'{"nodes":{},"cross_node":{"pair_overlaps":[],"coverage_suggestions":[],"blocked_nodes":[]}}'
)
latest_analytics_real_bytes: bytes = (
    b'{"nodes":{},"cross_node":{"pair_overlaps":[],"coverage_suggestions":[],"blocked_nodes":[]}}'
)
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
# n=2 solves withheld from the map because their track pairing has not (yet)
# passed the constant-velocity fit.  Counted separately from solver_failures:
# the solve succeeded, it simply has not earned publication, and a real target
# is published as soon as it accumulates the observation span to justify itself.
n2_unconfirmed: int = 0
# Overlap-grid rebuilds triggered by a node's empirical coverage tightening.
# A counter rather than a log line: the server emits WARNING and above, so an
# INFO message about this is invisible in every deployed environment — the same
# trap that hid n2_unconfirmed.  Zero here with populated polygons means the
# constraint is not reaching the grids.
coverage_rebuilds: int = 0
coverage_rebuild_nodes: int = 0
solver_queue_drops: int = 0

# Velocity-plausibility counters.  The solver has RMS gates on delay and
# Doppler but none on the resulting velocity, and staging showed 11 of 42
# tracks reporting above the simulator's fastest aircraft (522 kt), peaking at
# 824 kt.  These count rather than correct: the speeds stay on the wire, and
# these make the rate measurable while the cause is tracked down.
implausible_velocity_count: int = 0   # solves whose speed exceeded the bound
arc_velocity_rejects: int = 0         # arc-motion estimates rejected as absurd

# Solver end-to-end latency (seconds from queue submission to solve completion)
solver_last_latency_s: float = 0.0
solver_total_latency_s: float = 0.0
solver_total_solved: int = 0

# Peak active node count since startup (high-water mark for dropout detection)
peak_connected_nodes: int = 0

# ── Thread safety locks ──────────────────────────────────────────────────────
connected_nodes_lock = threading.Lock()
geo_aircraft_lock = threading.Lock()
anomaly_lock = threading.Lock()
# Guards solver_last_latency_s / solver_total_latency_s / solver_total_solved
solver_latency_lock = threading.Lock()
# Guards the plain int counters above.  `x += 1` on a module global is
# LOAD/ADD/STORE — solver workers and frame workers were losing updates.
counters_lock = threading.Lock()


def bump_counter(name: str, n: int = 1) -> None:
    """Thread-safe increment for a module-level int counter."""
    with counters_lock:
        globals()[name] += n

# ── Task health tracking ─────────────────────────────────────────────────────
task_last_success: dict[str, float] = {}  # task_name → last success epoch
task_error_counts: dict[str, int] = defaultdict(int)  # task_name → cumulative errors

# ── Accuracy tracking (haversine solver vs ADS-B) ────────────────────────────
# Rolling buffer of {hex, error_km, position_source, ts} samples.
ACCURACY_MAX_SAMPLES = 5000
accuracy_samples: deque = deque(maxlen=ACCURACY_MAX_SAMPLES)

# Pre-serialised accuracy stats (refreshed by background task alongside analytics)
latest_accuracy_bytes: bytes = b"{}"

# ── Per-node missed detections (refreshed every 30 s by analytics refresh) ────
# {node_id: {in_range, detected, missed, miss_rate, missed_aircraft: [...]}}
latest_missed_detections: dict[str, dict] = {}

# Pre-serialised radar3 solver verification (refreshed by background task)
latest_radar3_verification_bytes: bytes = b"{}"

# Rolling sample buffer for MLAT (multinode) solver accuracy — one entry per
# matched track per 30-s refresh cycle, for long-term trend monitoring.
MLAT_SAMPLES_MAX = 5000
mlat_samples: deque = deque(maxlen=MLAT_SAMPLES_MAX)

# Pre-serialised rolling MLAT accuracy stats (updated alongside mlat verification)
latest_mlat_accuracy_bytes: bytes = b"{}"

# Pre-serialised MLAT (multinode) solver verification vs ground-truth trails
# Initialised to the full zero-state so dashboard consumers can always access keys
# like n_solves / match_rate_pct before the first background refresh fires.
latest_mlat_verification_bytes: bytes = b'{"n_solves":0,"n_matched":0,"match_rate_pct":0.0,"match_threshold_km":8.0,"position":{"mean_km":0,"median_km":0,"p95_km":0,"max_km":0},"velocity":{"mean_ms":0,"median_ms":0,"p95_ms":0},"altitude":{"mean_m":0,"median_m":0,"p95_m":0},"by_node_count":{},"tracks":[]}'

# Pre-serialised storage stats (refreshed every 5 min by storage_refresh_task)
latest_storage_bytes: bytes = b"{}"

# ── Rate limiter buckets ──────────────────────────────────────────────────────
rate_buckets: dict[str, list] = defaultdict(list)

def _reset_for_tests() -> None:
    """Restore every module-level mutable store to boot state.  Tests only.

    Owned here (not in conftest) so adding a store means updating the module
    that declares it.  conftest's autouse fixture calls this before every
    test; previously it reset 3 of ~50 stores and the rest leaked across
    tests — most dangerously the wall-clock feed caches in frame_processor,
    which handed one test's detection_arcs/ground_truth to the next.
    """
    global aircraft_dirty, latest_aircraft_json, latest_aircraft_json_bytes
    global latest_real_aircraft_json_bytes, latest_analytics_bytes
    global latest_analytics_real_bytes, latest_nodes_bytes, latest_overlaps_bytes
    global latest_accuracy_bytes, latest_radar3_verification_bytes
    global latest_mlat_accuracy_bytes, latest_mlat_verification_bytes
    global latest_storage_bytes, simulation_config
    global frames_dropped, frames_processed, solver_successes, solver_failures
    global n2_unconfirmed, coverage_rebuilds, coverage_rebuild_nodes
    global solver_queue_drops, implausible_velocity_count, arc_velocity_rejects
    global solver_last_latency_s, solver_total_latency_s, solver_total_solved
    global peak_connected_nodes

    for store in (
        connected_nodes, node_pipelines, active_geo_aircraft, multinode_tracks,
        adsb_aircraft, track_histories, track_last_emit, track_gate_hold,
        track_arc_motion, ground_truth_trails, ground_truth_meta,
        node_identities, chain_entries, iq_commitments, anomaly_hexes,
        external_adsb_cache, ws_clients, ws_live_clients, ws_owner_clients,
        task_last_success, task_error_counts, rate_buckets,
        latest_missed_detections,
    ):
        store.clear()
    anomaly_log.clear()
    track_archive_buffer.clear()
    accuracy_samples.clear()
    mlat_samples.clear()
    for q in (frame_queue, solver_queue):
        try:
            while True:
                q.get_nowait()
        except Exception:
            pass

    node_analytics._reset_for_tests()
    node_associator._reset_for_tests()

    aircraft_dirty = False
    latest_aircraft_json = {"now": 0, "aircraft": [], "messages": 0}
    latest_aircraft_json_bytes = b'{"now":0,"aircraft":[],"messages":0}'
    latest_real_aircraft_json_bytes = b'{"now":0,"aircraft":[],"messages":0}'
    latest_analytics_bytes = (
        b'{"nodes":{},"cross_node":{"pair_overlaps":[],"coverage_suggestions":[],"blocked_nodes":[]}}'
    )
    latest_analytics_real_bytes = latest_analytics_bytes
    latest_nodes_bytes = b'{"nodes":{},"connected":0,"total":0,"synthetic":0}'
    latest_overlaps_bytes = b'{"overlaps":[],"registered_nodes":[]}'
    latest_accuracy_bytes = b"{}"
    latest_radar3_verification_bytes = b"{}"
    latest_mlat_accuracy_bytes = b"{}"
    latest_mlat_verification_bytes = b"{}"
    latest_storage_bytes = b"{}"
    simulation_config = dict(_SIMULATION_CONFIG_DEFAULTS)

    with counters_lock:
        frames_dropped = frames_processed = 0
        solver_successes = solver_failures = n2_unconfirmed = 0
        coverage_rebuilds = coverage_rebuild_nodes = solver_queue_drops = 0
        implausible_velocity_count = arc_velocity_rejects = 0
        solver_total_solved = 0
        solver_last_latency_s = solver_total_latency_s = 0.0
        peak_connected_nodes = 0


# ── Simulation physics config (read by fleet orchestrator, written by UI) ─────
simulation_config: dict = {
    # Anomalies off by default. Raise via PUT /api/simulation/config (or the
    # Physics tab) to turn them back on; note this dict is not persisted in the
    # state snapshot, so a backend restart returns it to these defaults.
    "frac_anomalous": 0.0,
    "frac_drone": 0.10,
    "frac_dark": 0.15,
    # aircraft (commercial) fraction = 1 - sum of above
    "max_range_km": 140,
    "min_aircraft": 60,
    "max_aircraft": 100,
    "_updated_at": 0.0,
}
_SIMULATION_CONFIG_DEFAULTS: dict = dict(simulation_config)
