"""Backend-computed node bias, trust feeding, and lying-radar/lying-transponder split.

The claiming stage (identity-first lane) binds detections to known ADS-B
aircraft and records, per claim, the measured AND predicted bistatic delay /
Doppler.  It calls ``record_claim_residual`` inline with the residual
(measured minus predicted) for every claim.  Everything in this module flows
from those residuals — which makes it the first trust input the *backend*
computes for itself.  The historical input, node self-reports POSTed to
routes/analytics.py, let a lying or miscalibrated node control its own trust
score; claim residuals are computed from the node's raw detections against an
ADS-B fix the backend fetched, so the node has no pen to grade itself with.

Three outputs, one residual stream:

1.  **Per-node bias** (``get_node_bias``): an EWMA estimate of each node's
    systematic delay/Doppler offset.  A receiver clock offset shows up as a
    constant delay bias; oscillator drift as a Doppler bias.  Exposed only
    past a maturity bar (see ``_bias_mature``).

2.  **Trust** (``get_node_trust``): residuals are converted into
    ``AdsReportEntry`` samples (provenance ``"claim_residual"``) and pushed
    into the EXISTING ``state.node_analytics.trust_scores`` — the same
    ``TrustScoreState`` the self-report route feeds, so node_retirement,
    state_snapshot and the analytics surfaces keep working unchanged, and the
    score blends both provenances (``summary()`` breaks them down).

3.  **Untrusted transponders** (``get_untrusted_hexes``): hexes demoted as
    lying/erroring ADS-B emitters, with hysteresis.  The claiming stage skips
    these as claim candidates.

Row/column disambiguation — the reason this is more than an error average:
a residual matrix cell is (node, hex).  One NODE misfitting across many
distinct hexes while its peers fit those same hexes is a lying/miscalibrated
RADAR — its own trust score sinks, because every one of its residual samples
lands in its TrustScoreState.  One HEX misfitting across many distinct nodes
(each of which fits elsewhere) is a lying TRANSPONDER — the hex is demoted,
flagged as an anomaly, and, crucially, its residuals are excluded from node
bias/trust inputs both prospectively (skipped while demoted) and
retroactively (already-fed samples are purged on demotion), so a spoofer
cannot talk the fleet's trust down.

Application hook (dark lane; integration happens post-merge, deliberately not
wired here): the dark-association/solver path should (a) subtract
``get_node_bias(node_id)`` from each dark-target measurement's delay/Doppler
before solving, so a node with a stable clock offset still triangulates
cleanly, and (b) weight each node's measurements by ``get_node_trust``
when building solver inputs, so a lying radar's contribution decays instead
of poisoning fits.  services/tasks/solver.py and frame_processor.py are owned
by other slices and are not touched here.

All state is module-level and guarded by ``_lock`` (repo lock discipline:
plain ``threading.Lock``, taken for every read-modify-write, never held
across a call that takes ``state.anomaly_lock``'s owners' locks... anomaly
flagging happens strictly *after* ``_lock`` is released, and nothing that
holds ``state.anomaly_lock`` ever calls back in here, so lock order is
acyclic).  Nothing is added to core/state.py — slice A owns that file.
"""

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

from retina_analytics.trust import AdsReportEntry, TrustScoreState

from core import state

log = logging.getLogger(__name__)

# ── Fit thresholds ────────────────────────────────────────────────────────────
# One definition of "this residual fits": TrustScoreState's own per-sample
# gates.  The trust score counts a sample good below exactly these, so reusing
# them means a cell we call "misfitting" is precisely a cell that is dragging
# the trust score down — the matrix view and the score can never disagree
# about what a misfit is.
_FIT_DELAY_US: float = TrustScoreState.delay_threshold_us
_FIT_DOPPLER_HZ: float = TrustScoreState.doppler_threshold_hz

# ── Matrix cell parameters ────────────────────────────────────────────────────
# A cell only gets a verdict from >=3 live samples — the repo's M-of-N
# maturity bar (track_gates gates coverage on n_detections>=3 for the same
# reason: one mis-matched frame must not characterize anything).  The verdict
# is the majority of the live samples, so a single glitch amid fits stays a
# fit.  10 minutes of liveness keeps verdicts about the *current* state of a
# transponder/receiver — a spoof that ended an hour ago should not still be
# voting — and 8 samples per cell bounds memory at nodes x hexes x 8 tuples.
_CELL_MIN_SAMPLES = 3
_CELL_MAX_SAMPLES = 8
_CELL_TTL_MS = 600_000  # 10 min

# Sweep dead cells every N records rather than per record: GC cost stays
# amortized O(1) while the matrix stays bounded by TTL x claim rate.  The
# hard cap is a backstop against hex churn (mass spoofing mints hexes faster
# than the TTL retires them); beyond it the stalest cells are evicted first,
# mirroring HistoricalCoverageMap.max_grid_cells' rationale.
_GC_EVERY = 512
_CELLS_MAX = 20_000

# ── Hex demotion (lying transponder) + hysteresis ─────────────────────────────
# Enter: >=3 distinct nodes hold a misfit verdict on the hex AND those are
# >=2/3 of the nodes with any verdict on it.  Three, because one misfitting
# node is indistinguishable from a miscalibrated radar (that is the row
# signature, handled by trust), and two could be a bad overlap-pair geometry;
# three independent receivers agreeing the fix does not match measurement is
# no longer explicable by any single-node fault.  The 2/3 supermajority
# protects marginal geometry: if nearly half the observing fleet fits the
# hex, the physics is ambiguous and demotion would be guessing.
#
# A misfit vote only counts from a node that currently FITS some other hex —
# that is the row/column split in one line: a node that misfits everywhere
# is indicting itself, not the transponder, and gets no say about hexes.
#
# Exit: hysteresis, deliberately asymmetric.  Re-admission needs >=2 distinct
# nodes with clean fit verdicts and ZERO misfit verdicts (cheaper than entry:
# while demoted the hex is skipped as a claim candidate, so fresh residuals
# are scarce — demanding another 3-node supermajority to exit could hold an
# innocent hex hostage forever), or the evidence ageing out entirely
# (_CELL_TTL_MS with no fresh sample — the demotion was built on a window
# that no longer exists, so holding it would be memory, not measurement;
# a still-active spoofer that gets re-admitted re-accumulates 3x3 misfits
# and is re-demoted, which is the intended probation loop).
_HEX_DEMOTE_MIN_NODES = 3
_HEX_DEMOTE_FRACTION = 2.0 / 3.0
_HEX_CLEAR_MIN_NODES = 2

# ── Bias estimator ────────────────────────────────────────────────────────────
# EWMA with alpha=0.1: at ~1 claim/s/node the estimate re-centres over ~10
# samples, fast enough to track oscillator drift over minutes but slow enough
# that one aircraft pass cannot own it.  The window deque exists for three
# things the recursion can't do: windowed variance for the maturity bar,
# distinct-hex counting, and replaying the EWMA after a demoted hex's samples
# are purged.
_BIAS_ALPHA = 0.1
_BIAS_WINDOW = 128

# Maturity bar (M-of-N convention, cf. track_gates' n_detections>=3):
# a bias is only reported once
#   - n >= 12 live samples (four cells' worth of the 3-sample bar), and
#   - >= 3 distinct hexes (a bias confirmed by one aircraft is
#     indistinguishable from that aircraft's transponder error — the same
#     row/column argument as demotion, applied to the row), and
#   - the standard error of the windowed mean is within a quarter of the fit
#     gate on both axes (1.25 us / 5 Hz).  The bias is destined to be
#     SUBTRACTED from dark-lane measurements; a correction less certain than
#     a quarter-gate can push borderline measurements across the gate in
#     either direction, which is worse than no correction.  Until the spread
#     justifies it, get_node_bias reports (0.0, 0.0) — "apply nothing".
_BIAS_MIN_SAMPLES = 12
_BIAS_MIN_HEXES = 3
_BIAS_SEM_MAX_US = _FIT_DELAY_US / 4.0
_BIAS_SEM_MAX_HZ = _FIT_DOPPLER_HZ / 4.0

# ── Trust prior ───────────────────────────────────────────────────────────────
# Neutral prior for unknown nodes, and the M-of-N bar again before the real
# score replaces it: below 3 samples the score quantizes to {0, 1/2, 1} and a
# single unlucky residual would zero a brand-new node's solver weight.
_TRUST_PRIOR = 0.5
_TRUST_MIN_SAMPLES = 3

_PROVENANCE = "claim_residual"

_lock = threading.Lock()

# node_id → hex → deque[(ts_ms, is_fit)]
_cells: dict[str, dict[str, deque]] = {}
# hex → set of node_ids with a cell (column index for the matrix)
_hex_nodes: dict[str, set[str]] = {}
# hex → {"demoted_at_ms": int, "last_sample_ms": int}
_untrusted: dict[str, dict] = {}
# Demotions since boot (module-level: core/state.py is owned by slice A, so
# this counter cannot live in state's counters block).
_demotion_count = 0
_records_since_gc = 0


class _NodeBiasState:
    __slots__ = ("window", "ewma_delay", "ewma_doppler")

    def __init__(self) -> None:
        # (ts_ms, hex, d_res_us, f_res_hz)
        self.window: deque = deque(maxlen=_BIAS_WINDOW)
        self.ewma_delay: float = 0.0
        self.ewma_doppler: float = 0.0

    def push(self, ts_ms: int, hex_: str, d_res_us: float, f_res_hz: float) -> None:
        if not self.window:
            self.ewma_delay = d_res_us
            self.ewma_doppler = f_res_hz
        else:
            self.ewma_delay += _BIAS_ALPHA * (d_res_us - self.ewma_delay)
            self.ewma_doppler += _BIAS_ALPHA * (f_res_hz - self.ewma_doppler)
        self.window.append((ts_ms, hex_, d_res_us, f_res_hz))

    def purge_hex(self, hex_: str) -> None:
        """Drop a demoted hex's samples and replay the EWMA over what remains.

        The recursion cannot un-mix a sample, so the estimate is rebuilt from
        the retained window.  History older than the window is lost to the
        replay — acceptable: the window (128 samples) is already the horizon
        the variance and maturity bar are computed over.
        """
        kept = [s for s in self.window if s[1] != hex_]
        self.window.clear()
        self.ewma_delay = 0.0
        self.ewma_doppler = 0.0
        for ts_ms, h, d, f in kept:
            self.push(ts_ms, h, d, f)


_node_bias: dict[str, _NodeBiasState] = {}


def _now_ms() -> int:
    """Wall clock in ms.  A function (not inline calls) so tests can advance it."""
    return int(time.time() * 1000)


# ── Interface contract (called by the claiming stage via guarded import) ─────


def record_claim_residual(node_id: str, hex: str, d_res_us: float, f_res_hz: float, ts_ms: int) -> None:  # noqa: A002 - `hex` is the contract's parameter name
    """Record one claim residual (measured minus predicted delay/Doppler).

    Called inline by the claiming stage for every claim.  Cheap by design:
    O(nodes observing this hex) worst case, amortized-O(1) GC.
    """
    if not node_id or not hex:
        return
    if not (math.isfinite(d_res_us) and math.isfinite(f_res_hz)):
        # A NaN residual would poison the EWMA permanently (NaN absorbs every
        # later sample); drop it here rather than trusting every caller.
        log.debug("node_bias: dropping non-finite residual from %s for %s", node_id, hex)
        return
    hex_ = hex.strip().lower()
    now = _now_ms()
    is_fit = abs(d_res_us) < _FIT_DELAY_US and abs(f_res_hz) < _FIT_DOPPLER_HZ

    demoted = False
    cleared = False
    feed_trust = False
    with _lock:
        _record_cell_locked(node_id, hex_, ts_ms, is_fit)
        _maybe_gc_locked(now)
        if hex_ in _untrusted:
            # Untrusted hex: keep observing (the matrix is how it earns its
            # way back), but feed neither bias nor trust — a demoted hex must
            # not punish the nodes that reported it.
            _untrusted[hex_]["last_sample_ms"] = now
            cleared = _hex_exit_check_locked(hex_, now)
        else:
            _node_bias.setdefault(node_id, _NodeBiasState()).push(ts_ms, hex_, d_res_us, f_res_hz)
            demoted = _hex_demote_check_locked(hex_, now)
            if demoted:
                _demote_locked(hex_, now)
            else:
                feed_trust = True

    # Everything below touches stores with their own (or no) locking; done
    # outside _lock so the module lock never nests inside another.
    if demoted:
        _purge_trust_samples(hex_)
        _flag_anomaly(hex_, now)
    elif cleared:
        _unflag_anomaly(hex_)
    elif feed_trust:
        _feed_trust(node_id, hex_, d_res_us, f_res_hz, ts_ms)


def get_node_bias(node_id: str) -> tuple[float, float]:
    """Current systematic (delay_us, doppler_hz) bias estimate for a node.

    (0.0, 0.0) — "apply no correction" — when the node is unknown or the
    estimate has not cleared the maturity bar (see the constants block).
    """
    with _lock:
        st = _node_bias.get(node_id)
        if st is None or not _bias_mature_locked(st):
            return (0.0, 0.0)
        return (st.ewma_delay, st.ewma_doppler)


def get_node_trust(node_id: str) -> float:
    """Trust 0..1 for a node, from the existing TrustScoreState.

    Neutral prior (0.5) for unknown nodes and below the M-of-N sample bar —
    the score blends self-reports and backend-fed claim residuals, so a node
    the backend has judged across many hexes converges to its earned score.
    """
    ts = state.node_analytics.trust_scores.get(node_id)
    if ts is None or len(ts.samples) < _TRUST_MIN_SAMPLES:
        return _TRUST_PRIOR
    return ts.score


def get_untrusted_hexes() -> set[str]:
    """Hexes currently demoted as lying transponders.

    The claiming stage skips these as claim candidates.  Ageing-out is
    evaluated lazily here as well as on record, so a spoofer that went silent
    (and therefore produces no residuals to trigger the exit check) still
    leaves the set once its evidence expires.
    """
    now = _now_ms()
    aged: list[str] = []
    with _lock:
        for hex_, meta in list(_untrusted.items()):
            if now - meta["last_sample_ms"] > _CELL_TTL_MS:
                del _untrusted[hex_]
                aged.append(hex_)
        result = set(_untrusted)
    for hex_ in aged:
        _unflag_anomaly(hex_)
    return result


# ── Surfacing helpers (routes/analytics.py) ──────────────────────────────────


def node_summary(node_id: str) -> dict | None:
    """Bias block for the per-node analytics payload; None when unknown."""
    with _lock:
        st = _node_bias.get(node_id)
        if st is None:
            return None
        n = len(st.window)
        hexes = {s[1] for s in st.window}
        d_std, f_std = _window_std_locked(st)
        mature = _bias_mature_locked(st)
        return {
            "bias_delay_us": round(st.ewma_delay, 3),
            "bias_doppler_hz": round(st.ewma_doppler, 2),
            "mature": mature,
            "n_samples": n,
            "n_hexes": len(hexes),
            "delay_std_us": round(d_std, 3),
            "doppler_std_hz": round(f_std, 2),
        }


def untrusted_summary() -> dict:
    """Untrusted-transponder block for the anomalies payload."""
    with _lock:
        return {
            "untrusted_hexes": sorted(_untrusted),
            "demotions": _demotion_count,
        }


# ── Snapshot persistence (services/state_snapshot.py) ─────────────────────────


def snapshot_state() -> dict:
    """Serializable slice of this module's state.

    Only the demotion set and counter persist: a spoofer must stay demoted
    across a 60 s-cadence restart, while the residual matrix and bias windows
    are 10-minute-horizon evidence that would mostly be stale by the time a
    rebuilt container replays them — they re-accumulate from live claims.
    """
    with _lock:
        return {
            "untrusted_hexes": {h: dict(meta) for h, meta in _untrusted.items()},
            "demotions": _demotion_count,
        }


def restore_state(snap: dict | None) -> None:
    """Restore from snapshot_state()'s output.  None/missing key → no-op,
    which is what keeps pre-node-bias snapshots loading unchanged."""
    global _demotion_count
    if not isinstance(snap, dict):
        return
    hexes = snap.get("untrusted_hexes")
    relit: list[str] = []
    with _lock:
        if isinstance(hexes, dict):
            for hex_, meta in hexes.items():
                if not isinstance(meta, dict):
                    continue
                _untrusted[str(hex_)] = {
                    "demoted_at_ms": int(meta.get("demoted_at_ms", 0)),
                    "last_sample_ms": int(meta.get("last_sample_ms", 0)),
                }
                relit.append(str(hex_))
        _demotion_count = int(snap.get("demotions", _demotion_count))
    # anomaly_hexes is not itself persisted (it is rebuilt live), so re-light
    # the flag for hexes that are coming back demoted.  Stale entries clear
    # through the normal lazy ageing path on the next get_untrusted_hexes().
    for hex_ in relit:
        with state.anomaly_lock:
            state.anomaly_hexes.add(hex_)


# ── Internals (call with _lock held unless noted) ─────────────────────────────


def _record_cell_locked(node_id: str, hex_: str, ts_ms: int, is_fit: bool) -> None:
    cell = _cells.setdefault(node_id, {}).setdefault(hex_, deque(maxlen=_CELL_MAX_SAMPLES))
    cell.append((ts_ms, is_fit))
    _hex_nodes.setdefault(hex_, set()).add(node_id)


def _cell_verdict_locked(node_id: str, hex_: str, now: int) -> str | None:
    """Verdict "fit" / "misfit", or None when too few live samples."""
    cell = _cells.get(node_id, {}).get(hex_)
    if not cell:
        return None
    live = [fit for ts, fit in cell if now - ts <= _CELL_TTL_MS]
    if len(live) < _CELL_MIN_SAMPLES:
        return None
    misfits = sum(1 for fit in live if not fit)
    return "misfit" if misfits * 2 >= len(live) else "fit"


def _node_fits_elsewhere_locked(node_id: str, exclude_hex: str, now: int) -> bool:
    for hex_ in _cells.get(node_id, {}):
        if hex_ != exclude_hex and _cell_verdict_locked(node_id, hex_, now) == "fit":
            return True
    return False


def _hex_demote_check_locked(hex_: str, now: int) -> bool:
    """Column rule — see the constants block for the thresholds' rationale."""
    misfit_votes = 0
    fit_votes = 0
    for node_id in _hex_nodes.get(hex_, ()):
        verdict = _cell_verdict_locked(node_id, hex_, now)
        if verdict == "fit":
            fit_votes += 1
        elif verdict == "misfit" and _node_fits_elsewhere_locked(node_id, hex_, now):
            # Only a node that demonstrably works (fits some other hex) may
            # indict a transponder; a node misfitting everywhere is the row
            # signature and votes with its own trust score instead.
            misfit_votes += 1
    total = misfit_votes + fit_votes
    return misfit_votes >= _HEX_DEMOTE_MIN_NODES and misfit_votes >= _HEX_DEMOTE_FRACTION * total


def _hex_exit_check_locked(hex_: str, now: int) -> bool:
    """Hysteresis exit: sustained clean fits.  Returns True (and removes the
    hex) when re-admitted.  Ageing-out is handled in get_untrusted_hexes."""
    fit_votes = 0
    for node_id in _hex_nodes.get(hex_, ()):
        verdict = _cell_verdict_locked(node_id, hex_, now)
        if verdict == "misfit":
            return False
        if verdict == "fit":
            fit_votes += 1
    if fit_votes >= _HEX_CLEAR_MIN_NODES:
        _untrusted.pop(hex_, None)
        log.info("node_bias: hex %s re-admitted after sustained clean fits (%d nodes)", hex_, fit_votes)
        return True
    return False


def _demote_locked(hex_: str, now: int) -> None:
    global _demotion_count
    _untrusted[hex_] = {"demoted_at_ms": now, "last_sample_ms": now}
    _demotion_count += 1
    # The hex's residuals must stop informing node bias immediately — they
    # were the transponder lying, not the receivers drifting.
    for st in _node_bias.values():
        st.purge_hex(hex_)
    log.warning("node_bias: hex %s demoted as untrusted transponder (demotion #%d)", hex_, _demotion_count)


def _bias_mature_locked(st: _NodeBiasState) -> bool:
    n = len(st.window)
    if n < _BIAS_MIN_SAMPLES:
        return False
    if len({s[1] for s in st.window}) < _BIAS_MIN_HEXES:
        return False
    d_std, f_std = _window_std_locked(st)
    sqrt_n = math.sqrt(n)
    return d_std / sqrt_n <= _BIAS_SEM_MAX_US and f_std / sqrt_n <= _BIAS_SEM_MAX_HZ


def _window_std_locked(st: _NodeBiasState) -> tuple[float, float]:
    n = len(st.window)
    if n == 0:
        return (0.0, 0.0)
    d_mean = sum(s[2] for s in st.window) / n
    f_mean = sum(s[3] for s in st.window) / n
    d_var = sum((s[2] - d_mean) ** 2 for s in st.window) / n
    f_var = sum((s[3] - f_mean) ** 2 for s in st.window) / n
    return (math.sqrt(d_var), math.sqrt(f_var))


def _maybe_gc_locked(now: int) -> None:
    global _records_since_gc
    _records_since_gc += 1
    if _records_since_gc < _GC_EVERY:
        return
    _records_since_gc = 0
    for node_id in list(_cells):
        node_cells = _cells[node_id]
        for hex_ in list(node_cells):
            cell = node_cells[hex_]
            if not cell or now - cell[-1][0] > _CELL_TTL_MS:
                del node_cells[hex_]
                nodes = _hex_nodes.get(hex_)
                if nodes is not None:
                    nodes.discard(node_id)
                    if not nodes:
                        del _hex_nodes[hex_]
        if not node_cells:
            del _cells[node_id]
    # Backstop cap: evict stalest cells first (see _CELLS_MAX rationale).
    total = sum(len(nc) for nc in _cells.values())
    if total > _CELLS_MAX:
        flat = [(cell[-1][0], node_id, hex_) for node_id, nc in _cells.items() for hex_, cell in nc.items() if cell]
        flat.sort()
        for _, node_id, hex_ in flat[: total - _CELLS_MAX]:
            _cells[node_id].pop(hex_, None)
            nodes = _hex_nodes.get(hex_)
            if nodes is not None:
                nodes.discard(node_id)
                if not nodes:
                    del _hex_nodes[hex_]


# ── Internals that take other stores' locks (call WITHOUT _lock held) ─────────


def _feed_trust(node_id: str, hex_: str, d_res_us: float, f_res_hz: float, ts_ms: int) -> None:
    """Push one residual into the existing trust score as an AdsReportEntry.

    predicted=0 / measured=residual keeps TrustScoreState's error arithmetic
    (|predicted - measured|) computing exactly |residual|, so score and RMS
    semantics match the self-report path sample-for-sample.

    Deliberately NOT via NodeAnalyticsManager.record_adsb_correlation, which
    the self-report route uses: that method also feeds the node's
    HistoricalCoverageMap with the sample's position.  A claim residual
    carries no position (the claims registry holds the fix; this contract
    passes residuals only), so routing through it would stamp null-island
    (0,0) coverage cells and, at one entry per claim, churn real coverage
    entries out of the 10k-capped list.  The trust-side logic below is the
    method's trust half verbatim: create-if-absent, then add_sample (the
    500-sample cap lives in add_sample).
    """
    scores = state.node_analytics.trust_scores
    ts = scores.get(node_id)
    if ts is None:
        ts = scores[node_id] = TrustScoreState(node_id=node_id)
    ts.add_sample(
        AdsReportEntry(
            timestamp_ms=ts_ms,
            predicted_delay=0.0,
            predicted_doppler=0.0,
            measured_delay=d_res_us,
            measured_doppler=f_res_hz,
            adsb_hex=hex_,
            adsb_lat=0.0,
            adsb_lon=0.0,
            provenance=_PROVENANCE,
        )
    )


def _purge_trust_samples(hex_: str) -> None:
    """Retract every backend-fed sample for a newly demoted hex.

    Samples were fed inline as they arrived — demotion is only recognizable
    in hindsight, once enough nodes have voted — so by the time a spoofer is
    identified its misfits have already dented every reporting node's score.
    Filtering on provenance keeps the retraction surgical: what a node
    self-reported about this hex is its own claim and stays.
    """
    for ts in state.node_analytics.trust_scores.values():
        before = len(ts.samples)
        ts.samples = [s for s in ts.samples if not (s.provenance == _PROVENANCE and s.adsb_hex == hex_)]
        if len(ts.samples) != before:
            log.info(
                "node_bias: retracted %d claim-residual samples for demoted hex %s from node %s",
                before - len(ts.samples),
                hex_,
                ts.node_id,
            )


def _flag_anomaly(hex_: str, now: int) -> None:
    """Flag a demoted hex the way the system flags anomalous aircraft
    (same store, same lock, same capped log as sim_ingest/track_gates)."""
    with state.anomaly_lock:
        state.anomaly_hexes.add(hex_)
        state.anomaly_log.append(
            {
                "hex": hex_,
                "ts": round(now / 1000.0, 1),
                "reason": "untrusted_transponder",
                "flagged_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(state.anomaly_log) > state.ANOMALY_LOG_MAX:
            state.anomaly_log = state.anomaly_log[-state.ANOMALY_LOG_MAX :]


def _unflag_anomaly(hex_: str) -> None:
    with state.anomaly_lock:
        # Same guard as track_gates' discard path: sim_ingest owns hexes the
        # simulator marked anomalous in ground truth — a transponder
        # re-admission must not wipe a behavioural flag someone else holds.
        if not state.ground_truth_meta.get(hex_, {}).get("is_anomalous"):
            state.anomaly_hexes.discard(hex_)


def _reset_for_tests() -> None:
    """Restore module state to boot.  Tests only — see core.state's twin."""
    global _demotion_count, _records_since_gc
    with _lock:
        _cells.clear()
        _hex_nodes.clear()
        _untrusted.clear()
        _node_bias.clear()
        _demotion_count = 0
        _records_since_gc = 0
