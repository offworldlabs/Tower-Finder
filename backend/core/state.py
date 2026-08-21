"""Centralised mutable state shared across modules.

Every global dict / set / queue that multiple parts of the server touch
lives here so imports are unambiguous and circular-dependency-free.
"""

import asyncio
import logging
import math
import os
import threading
import time
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
    FT_TO_M,
    GROUND_TRUTH_MAX,  # noqa: F401 — re-exported, used via state.GROUND_TRUTH_MAX
    N2_CONFIRM_MIN_EPOCHS,
    N2_CONFIRM_MIN_SPAN_S,
    TRACK_HISTORY_MAX,  # noqa: F401 — re-exported, used via state.TRACK_HISTORY_MAX
)

# ── Coverage / analytics persistence ──────────────────────────────────────────
COVERAGE_STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data")

# ── Connected node state tracking ─────────────────────────────────────────────
connected_nodes: dict[str, dict] = {}
# node_id → {config_hash, config, status, last_heartbeat, peer, is_synthetic, capabilities}

# Empirical FOV (see retina_analytics.empirical_coverage / manager /
# association).  off/shadow/active, ASSOC_CLAIM_MODE precedent — an
# unrecognised value falls back to "off" rather than raising.  Read here
# rather than in config/constants.py for the same reason _ASSOC_CLAIM_MODE
# is: this module's construction of node_analytics/node_associator below is
# the use site, and constants.py's header rule is that env vars are read
# there.  Default "off" is byte-identical to every behaviour that predates
# this flag.
FOV_MODE = os.getenv("FOV_MODE", "off").lower()
if FOV_MODE not in ("off", "shadow", "active"):
    FOV_MODE = "off"

# ADS-B-seeded detection assignment (see retina_analytics.association.
# _adsb_seed_round).  off/shadow/active, same ASSOC_CLAIM_MODE precedent as
# FOV_MODE above — an unrecognised value falls back to "off" rather than
# raising.
ADSB_SEED_MODE = os.getenv("ADSB_SEED_MODE", "off").lower()
if ADSB_SEED_MODE not in ("off", "shadow", "active"):
    ADSB_SEED_MODE = "off"

node_analytics = NodeAnalyticsManager(storage_dir=COVERAGE_STORAGE_DIR, fov_mode=FOV_MODE)


def _coverage_limit_for(node_id: str):
    """Shrink-only empirical prior for one node, from accumulated ADS-B fixes.

    Injected rather than imported so retina-analytics keeps knowing nothing
    about NodeAnalyticsManager, the same shape cv_fit used.
    """
    return node_analytics.coverage_limit_for(node_id)


def _learned_fov_for(node_id: str):
    """The learned-FOV state for one node, or None — see
    NodeAnalyticsManager.learned_fov_for.

    Passed to InterNodeAssociator as fov_provider ONLY when FOV_MODE is
    active (below): off/shadow construct the associator with fov_provider
    unset, so every geo.fov stays None and the overlap grids this module
    builds are byte-identical to before FOV_MODE existed.  The solver's beam
    gate (services/tasks/solver.py) reads node_analytics.learned_fov_for
    directly instead of through this — it runs per-solve, not at
    registration, and shadow mode needs the state even though the associator
    must not.
    """
    return node_analytics.learned_fov_for(node_id)


# Top-down tracklet claiming (see retina_analytics.association._claim_round).
# off/shadow/active, following the SOLVER_CONSENSUS_MODE precedent.  Read
# here rather than in config/constants.py: constants.py's header rule is
# that env vars are read at their use site, and this module's construction
# of node_associator below is that site.
_ASSOC_CLAIM_MODE = os.getenv("ASSOC_CLAIM_MODE", "off").lower()


def _global_tracks_for_claiming():
    """Unlocked snapshot of currently-published dark tracks, in the
    claiming provider contract InterNodeAssociator documents on
    global_track_provider.

    list(dict.items()) — the same unlocked read pattern _coverage_limit_for
    and every other snapshot provider in this file uses; a concurrent
    solver-thread write racing this read loses at worst one entry to a
    stale round, not a corrupted one.

    Only mn-dark-* keys are claimable: an ADS-B-tagged track already has an
    external identity (a transponder hex) and must never be re-derived by
    top-down projection — mirrors "only dark tracks are claimable" at
    solver.py's multinode_key_decision.  Eligibility
    filtering, the DR-age cap and the CLAIM_MAX_GLOBAL_TRACKS truncation are
    all applied by the LIB, not here — this stays a dumb snapshot so the
    offline bench measures the shipped filtering.
    """
    out = []
    for key, rec in list(multinode_tracks.items()):
        if not key.startswith("mn-dark-"):
            continue
        out.append(
            {
                "key": key,
                "lat": rec.get("lat"),
                "lon": rec.get("lon"),
                "alt_m": rec.get("alt_m", 0.0),
                "vel_east": rec.get("vel_east", 0.0),
                "vel_north": rec.get("vel_north", 0.0),
                "vel_up": rec.get("vel_up", 0.0),
                "timestamp_ms": rec.get("timestamp_ms", 0),
                "n_nodes": rec.get("n_nodes", 0),
                "solve_count": rec.get("solve_count", 0),
            }
        )
    return out


def _adsb_for_seeding() -> dict[str, dict]:
    """Unlocked snapshot of currently-live ADS-B fixes, in the seeding
    provider contract InterNodeAssociator documents on adsb_provider.

    list(dict.items()) — the same unlocked read pattern
    _global_tracks_for_claiming uses; a concurrent frame-worker write racing
    this read loses at worst one entry to a stale round, not a corrupted
    one.  Dumb snapshot, no age filtering here — the LIB applies freshness
    and gating, so the offline bench measures the shipped filtering.
    """
    out = {}
    for hexn, rec in list(adsb_aircraft.items()):
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        gs_ms = (rec.get("gs", 0) or 0) * 0.514444
        trk = math.radians(rec.get("track", 0) or 0)
        out[hexn] = {
            "hex": hexn,
            "lat": lat,
            "lon": lon,
            "alt_m": (rec.get("alt_baro", 0) or 0) * FT_TO_M,
            "vel_east": gs_ms * math.sin(trk),
            "vel_north": gs_ms * math.cos(trk),
            "timestamp_ms": rec.get("last_seen_ms", 0),
            "alt_baro": rec.get("alt_baro", 0),
            "gs": rec.get("gs", 0),
            "track": rec.get("track", 0),
            "flight": rec.get("flight", ""),
        }
    return out


node_associator = InterNodeAssociator(
    grid_step_km=ASSOC_GRID_STEP_KM,
    coverage_provider=_coverage_limit_for,
    # Active only (see _learned_fov_for) — shadow still computes/counts the
    # FOV verdict at the solver gate, but must not touch the overlap grid,
    # so off/shadow's association geometry is unchanged by this flag.
    fov_provider=(_learned_fov_for if FOV_MODE == "active" else None),
    claim_mode=_ASSOC_CLAIM_MODE,
    global_track_provider=_global_tracks_for_claiming,
    # Passed explicitly because ASSOC_MIN_INTERVAL_S was dead config: defined
    # here but never reaching the associator, which used its own hardcoded
    # copy, so tuning it did nothing.
    assoc_interval_s=ASSOC_MIN_INTERVAL_S,
    # No inline fit: the epochs travel with the candidate and the solver
    # worker runs the fit on its own threads.  An 86 ms LM solve on the
    # frame path is frame latency.
    # cv_chi2_max is deliberately NOT passed: with cv_fit=None the associator
    # never scores anything, so the parameter is inert here — passing
    # N2_CONFIRM_CHI2_MAX made it a live-looking dead wire.  The threshold
    # that actually gates n=2 publication is the solver worker's
    # _N2_CONFIRM_CHI2_MAX, bound from the same constant.
    cv_fit=None,
    cv_min_epochs=N2_CONFIRM_MIN_EPOCHS,
    cv_min_span_s=N2_CONFIRM_MIN_SPAN_S,
    # Both were dead config in the same way assoc_interval_s had been: defined
    # here, never passed, with the library falling back to its own value.  The
    # neighbour cap in particular only ever applied on the detection path, so
    # the live one was uncapped.
    max_neighbors=ASSOC_MAX_NEIGHBORS,
    max_pairs_per_round=ASSOC_MAX_PAIRS_PER_ROUND,
    adsb_seed_mode=ADSB_SEED_MODE,
    adsb_provider=_adsb_for_seeding,
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

# Per-solve MLAT history for the debug panel: every solver outcome (published
# or gate-rejected), ~30 min retention.  Queried by /api/test/mlat-history so
# an individual map marker's solves can be looked up and checked by its
# mn<sha256[:10]> hex.  maxlen is a hard memory cap (~300 B/record);
# age-pruning happens on write in the solver's recording helper.
MLAT_HISTORY_MAX = 8000
mlat_solve_history: deque = deque(maxlen=MLAT_HISTORY_MAX)

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
# Frames where the backend filled frame["adsb"] itself (ADSB_SEED_MODE=active,
# node reported no list of its own) — see frame_processor.process_one_frame's
# predictive-tagging block.
adsb_seed_frames_autotagged: int = 0
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
# Nodes whose coverage digest has moved but whose grids are still queued behind
# the per-cycle rebuild budget.  A gauge, not a counter: it is the depth right
# now, and a depth that never returns to zero means the budget is below the
# fleet's trigger rate — constraints are then converging slower than the
# coverage they follow, which no rebuild counter can show.
coverage_rebuild_backlog: int = 0
solver_queue_drops: int = 0
# Queue items discarded unsolved because they aged past _SOLVER_MAX_QUEUE_AGE_S
# waiting for a worker.  Was only a DEBUG log, which staging does not emit —
# the drain-rate collapse behind the August latency incident was invisible in
# every counter (solver_queue_drops stayed 0: the queue never overflowed, it
# just drained too slowly).
solver_stale_drops: int = 0

# Candidates dequeued and skipped because every single-node track they carry
# was already solved within _SOLVER_RESOLVE_INTERVAL_S at no fewer nodes (see
# solver.py's _claim_resolve_slot).  Association is per-node and rate-limited
# per node, so one aircraft arrives as one candidate per node that can see it;
# this counts the copies that were never worth solving.  High against
# solver_successes is normal and is the mechanism working — it is
# solver_stale_drops that means work was lost.
solver_resolve_skips: int = 0

# Multinode entries removed because a later solve shared a source single-node
# track under a different track key (the 6 km match in multinode_key_decision
# missed).  One aircraft is one set of source tracks, so the earlier entry is
# replaced immediately rather than coexisting with the new one until its own
# 60 s expiry.
mn_superseded: int = 0

# Solves published after node-trimming recovered them from the rms_delay
# gate at n>=4 (see solver.py's _trim_and_resolve).  Counted once per
# publish, not per trim round — this is "how many map markers exist because
# of trimming", not "how many rounds trimming ran".
solver_trimmed: int = 0

# Consensus hypothesis stage (solver.py's _consensus_select /
# retina_geolocator.consensus.select_consensus), gated by
# SOLVER_CONSENSUS_MODE (off/shadow/active; off by default).  selected and
# filtered both count "active" selections that cleared _CONSENSUS_MIN_NODES
# — filtered is the subset of those where at least one input node was
# actually dropped, so selected - filtered is "corroborated but nothing to
# drop".  fallback is every non-acting outcome regardless of mode (an
# exception, an abstain, or too few corroborated nodes) — the unfiltered
# input is used either way.  shadow is "active"'s dry-run twin: consensus
# ran and its selection was recorded, but the LM still saw the unfiltered
# input.
solver_consensus_selected: int = 0
solver_consensus_filtered: int = 0
solver_consensus_fallback: int = 0
solver_consensus_shadow: int = 0

# Top-down claiming's solver-side honoring (services.tasks.solver's
# multinode_key_decision), gated on the anchor_key field an anchored solver
# input carries — no mode read here, ASSOC_CLAIM_MODE lives entirely in the
# associator; without the field this whole block stays at zero, which is
# what "off" (no field ever set) and "shadow" (claiming computed but no
# anchored input emitted) both look like.  anchored_published is every
# publish that carried an anchor_key regardless of outcome; hits is the
# subset that actually kept the anchor's own key, fallbacks the subset that
# fell through to proximity/mint anyway (e.g. the >6 km displacement check
# in multinode_key_decision tripped) — hits + fallbacks == anchored_published.
solver_anchor_hits: int = 0
solver_anchor_fallbacks: int = 0
solver_anchored_published: int = 0

# Publishes whose velocity carried the vel_untrusted flag (vz saturated, or
# raw-solve velocity at n<=3) — the denominator is solver_successes.
solver_vel_untrusted_published: int = 0

# FOV_MODE beam-gate instrumentation (services/tasks/solver.py's
# fov_gate_verdict).  shadow-mode-only comparison of today's per-node beam
# verdict against what the learned FOV would have decided: agree covers both
# (pass,pass) and (reject,reject); would_pass is "today rejects, FOV would
# pass" — the radar3 recovery number; would_reject is the opposite direction
# (today passes, FOV would reject — never acted on in shadow, but worth
# knowing about before any active flip).  neg_events is the disappearance
# detector's (analytics_refresh.py) accepted record_negative_event count,
# shadow AND active (it runs in both — see _refresh_missed_detections).  All
# four stay at zero under FOV_MODE=off.
fov_shadow_agree: int = 0
fov_shadow_would_pass: int = 0
fov_shadow_would_reject: int = 0
fov_neg_events: int = 0

# Unhandled exceptions swallowed by the solver worker loop so the thread
# survives.  Nonzero means a solve item crashed past every gate's own
# handling — the 2026-08-08 outage (trail-deque race) killed both workers
# because nothing caught it; now the item is dropped, this counts it, and
# the traceback lands in the log.
solver_worker_errors: int = 0

# Per-reason solver rejection counters.  solver_failures is the aggregate; the
# per-solve reason was only ever logged at DEBUG, which staging does not emit —
# 301 failures in one 66-minute window were unattributable.  One counter per
# reject gate makes the breakdown observable at /api/test/dashboard.
solver_fail_exception: int = 0
solver_fail_unconverged: int = 0
solver_fail_rms_delay: int = 0
solver_fail_rms_doppler: int = 0
solver_fail_beam: int = 0
solver_fail_displacement: int = 0

# Position-jump detections (teleporting emits).  Observability only: jumps are
# solver mis-association noise, not target behaviour, so they no longer mark
# tracks anomalous — but a rising rate here still means association regressed.
position_jump_events: int = 0

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

# Pre-serialised per-node solver verification, {node_id: json bytes}
# (refreshed by background task, one entry per real node)
latest_node_verification_bytes: dict[str, bytes] = {}

# Rolling sample buffer for MLAT (multinode) solver accuracy — one entry per
# matched track per 30-s refresh cycle, for long-term trend monitoring.
MLAT_SAMPLES_MAX = 5000
mlat_samples: deque = deque(maxlen=MLAT_SAMPLES_MAX)

# Pre-serialised rolling MLAT accuracy stats (updated alongside mlat verification)
latest_mlat_accuracy_bytes: bytes = b"{}"

# Pre-serialised MLAT (multinode) solver verification vs ground-truth trails
# Initialised to the full zero-state so dashboard consumers can always access keys
# like n_solves / match_rate_pct before the first background refresh fires.
latest_mlat_verification_bytes: bytes = b'{"n_solves":0,"n_matched":0,"match_rate_pct":0.0,"match_threshold_km":12.0,"position":{"mean_km":0,"median_km":0,"p95_km":0,"max_km":0},"velocity":{"mean_ms":0,"median_ms":0,"p95_ms":0},"altitude":{"mean_m":0,"median_m":0,"p95_m":0},"by_node_count":{},"tracks":[]}'

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
    global latest_accuracy_bytes
    global latest_mlat_accuracy_bytes, latest_mlat_verification_bytes
    global latest_storage_bytes, simulation_config
    global frames_dropped, frames_processed, solver_successes, solver_failures
    global adsb_seed_frames_autotagged
    global n2_unconfirmed, coverage_rebuilds, coverage_rebuild_nodes
    global coverage_rebuild_backlog
    global solver_queue_drops, solver_stale_drops, solver_resolve_skips
    global mn_superseded, solver_trimmed
    global solver_consensus_selected, solver_consensus_filtered
    global solver_consensus_fallback, solver_consensus_shadow
    global solver_anchor_hits, solver_anchor_fallbacks, solver_anchored_published
    global solver_vel_untrusted_published
    global fov_shadow_agree, fov_shadow_would_pass, fov_shadow_would_reject
    global fov_neg_events
    global solver_worker_errors
    global solver_fail_exception, solver_fail_unconverged, solver_fail_rms_delay
    global solver_fail_rms_doppler, solver_fail_beam, solver_fail_displacement
    global position_jump_events
    global solver_last_latency_s, solver_total_latency_s, solver_total_solved
    global peak_connected_nodes

    for store in (
        connected_nodes,
        node_pipelines,
        active_geo_aircraft,
        multinode_tracks,
        adsb_aircraft,
        track_histories,
        track_last_emit,
        track_gate_hold,
        track_arc_motion,
        ground_truth_trails,
        ground_truth_meta,
        node_identities,
        chain_entries,
        iq_commitments,
        anomaly_hexes,
        external_adsb_cache,
        ws_clients,
        ws_live_clients,
        ws_owner_clients,
        task_last_success,
        task_error_counts,
        rate_buckets,
        latest_missed_detections,
    ):
        store.clear()
    anomaly_log.clear()
    track_archive_buffer.clear()
    mlat_solve_history.clear()
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
    latest_node_verification_bytes.clear()
    latest_mlat_accuracy_bytes = b"{}"
    latest_mlat_verification_bytes = b"{}"
    latest_storage_bytes = b"{}"
    simulation_config = dict(_SIMULATION_CONFIG_DEFAULTS)

    with counters_lock:
        frames_dropped = frames_processed = 0
        solver_successes = solver_failures = n2_unconfirmed = 0
        adsb_seed_frames_autotagged = 0
        coverage_rebuilds = coverage_rebuild_nodes = solver_queue_drops = 0
        coverage_rebuild_backlog = 0
        solver_stale_drops = 0
        solver_resolve_skips = 0
        mn_superseded = 0
        solver_trimmed = 0
        solver_consensus_selected = solver_consensus_filtered = 0
        solver_consensus_fallback = solver_consensus_shadow = 0
        solver_anchor_hits = solver_anchor_fallbacks = solver_anchored_published = 0
        solver_vel_untrusted_published = 0
        fov_shadow_agree = fov_shadow_would_pass = fov_shadow_would_reject = 0
        fov_neg_events = 0
        solver_worker_errors = 0
        solver_fail_exception = solver_fail_unconverged = solver_fail_rms_delay = 0
        solver_fail_rms_doppler = solver_fail_beam = solver_fail_displacement = 0
        position_jump_events = 0
        solver_total_solved = 0
        solver_last_latency_s = solver_total_latency_s = 0.0
        peak_connected_nodes = 0


# ── Simulation physics config (read by fleet orchestrator, written by UI) ─────

# Hardcoded last-resort fractions.  SIM_FRAC_* env overrides these at boot
# (see _env_frac below), and a restored snapshot overrides that in turn — see
# services/state_snapshot.py for the precedence rule between the last two.
_SIM_FRAC_FALLBACKS: dict = {
    # Anomalies off by default. Raise via PUT /api/simulation/config (or the
    # Physics tab) to turn them back on.
    "frac_anomalous": 0.0,
    # Drones off by default (user call, 2026-08: fixed-wing scene only);
    # raise via the Physics tab / PUT when a drone scenario is wanted.
    "frac_drone": 0.0,
    "frac_dark": 0.15,
}


def _env_frac(name: str, default: float) -> float:
    """Read one 0.0–1.0 spawn fraction from the environment.

    Deployment intent belongs in compose, not in this file: an operator who
    wants dark traffic on staging sets SIM_FRAC_DARK there and every rebuild
    boots into it instead of into the fallbacks above.  A bad value is logged
    and ignored rather than raised — a typo'd env var must not stop the
    backend booting into a working scene.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        logging.warning("%s=%r is not a number — falling back to %.2f", name, raw, default)
        return default
    if not (0.0 <= v <= 1.0):
        logging.warning("%s=%r outside 0.0–1.0 — falling back to %.2f", name, raw, default)
        return default
    return v


def _seed_sim_fracs_from_env() -> dict:
    """Build the boot fraction set from SIM_FRAC_*, rejecting an over-100% mix.

    The PUT route enforces sum ≤ 1.0 because commercial traffic is the
    remainder; env has to enforce the same or the world spawns against a
    negative commercial share.  An over-budget env set is refused whole
    rather than partially applied — a half-honoured scene is harder to
    diagnose than one that is loudly ignored.
    """
    seeded = {key: _env_frac(f"SIM_{key.upper()}", fallback) for key, fallback in _SIM_FRAC_FALLBACKS.items()}
    total = sum(seeded.values())
    if total > 1.0:
        logging.error(
            "SIM_FRAC_* sum to %.2f (> 1.0) — ignoring all three, using fallbacks %s",
            total,
            _SIM_FRAC_FALLBACKS,
        )
        return dict(_SIM_FRAC_FALLBACKS)
    return seeded


simulation_config: dict = {
    **_seed_sim_fracs_from_env(),
    # aircraft (commercial) fraction = 1 - sum of above
    #
    # Deliberately NO defaults for max_range_km / min_aircraft / max_aircraft:
    # the fleet orchestrator applies those keys only when present, falling back
    # to its own deployment env (FLEET_MIN_AIRCRAFT etc.).  Defaults here are a
    # footgun — the whole dict ships on the first poll after boot, so a stale
    # default scale (this dict once said 60-100 aircraft) silently overrode
    # the deployed 20-40.
    #
    # Boot-stamped, not 0.0: the orchestrator applies the fractions only when
    # _updated_at strictly exceeds its last-seen value (which starts at 0.0),
    # so a 0.0 stamp meant these defaults were NEVER pushed — the simulation
    # world silently ran its own constructor defaults (drones 0.10!) until
    # someone touched the Physics tab, and reverted to them on every rebuild.
    "_updated_at": time.time(),
}
_SIMULATION_CONFIG_DEFAULTS: dict = dict(simulation_config)

# The SIM_FRAC_* set this process booted with, recorded so a restored snapshot
# can distinguish "an operator tuned this at runtime" (the snapshot wins, which
# is the whole point of persisting it) from "the deploy changed the intended
# scene" (env wins).  See services/state_snapshot.py:restore_snapshot.
_SIMULATION_ENV_BASELINE: dict = {k: simulation_config[k] for k in _SIM_FRAC_FALLBACKS}
