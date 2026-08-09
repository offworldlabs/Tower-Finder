"""Stone-Soup GOSPA/SIAP adapter for association_bench.py -- bench-only, optional.

Why this exists
----------------
association_bench.py's ghost/matched counters are a single hand-rolled rule:
a solve is "real" if it lands within MATCH_KM of some aircraft, "ghost"
otherwise.  That is the right question for the ghost-rate investigation it
was built for, but it is one number, picked in advance, and it says nothing
about track continuity, ambiguous assignment, or the trade the solver makes
between missing a target and inventing one.  Stone-Soup's GOSPA and SIAP
metrics are the standard, assignment-based complement: GOSPA decomposes a
single distance into localisation/missed/false costs via an optimal
assignment at each timestamp, and SIAP scores completeness, ambiguity,
spuriousness and track-continuity the way the tracking literature usually
does.  Bringing both in gives a second, independently-implemented read on
the same runs, which is worth having precisely because it is not tuned to
this codebase's own assumptions.

This is entirely optional.  Stone-Soup (pip package ``stonesoup``) ships in
the bench's own docker image but must never become a hard dependency --
services/ never imports it, and this module itself degrades to inert when
the package is absent: every stonesoup import below is wrapped in a single
try/except ImportError, and STONESOUP_AVAILABLE tells callers whether any of
the rest of this module can be used at all.  format_report_lines() is kept
free of stonesoup imports specifically so association_bench.py can format a
dict computed in a stonesoup-available process (or in a test) even in a
process where stonesoup itself is not installed.

The sampling design -- why a recorder exists at all
----------------------------------------------------
Stone-Soup's GOSPAMetric.compute_over_time and SIAPMetrics evaluate at the
union of every timestamp present across all tracks and all ground truths.
That is the wrong timeline for this bench: the world steps every ``dt``
(commonly 1 s) but a node only *publishes* a solve on its association
cadence (commonly every few seconds, and only when a solve actually clears
every gate) -- so the published series is sparse and irregular while the
truth series the simulator can supply is effectively continuous.  Evaluated
naively against the union of timestamps, a perfectly-tracked aircraft would
read as "missed" at every instant between two publishes, since a track
carries no state at the truth-only timestamps in between and GOSPA scores a
truth with no coincident track as a miss at that instant.  The number that
would fall out of that is not a tracking-quality number; it is a measurement
of the publish cadence.

So both series are resampled onto one uniform grid (``sample_dt_s``) before
either ever reaches Stone-Soup.  Truth is recorded once per grid point (the
world's actual state snapshotted at whichever step first reaches that grid
time).  Publishes are recorded as they happen, then at compute time each
track's *last* publish before a grid point is held forward -- position and
velocity unchanged -- for up to ``hold_max_age_s``, after which the track
has no state at that grid point at all (a real miss, not an artefact of
cadence).  This is a deliberate simplification: it is explicitly NOT
dead-reckoning (no extrapolation along heading/speed), it is "what would a
viewer of the map, refreshed every sample_dt_s seconds, still be seeing when
this solve was last updated" -- i.e. it measures what the pipeline visibly
delivers between solves, not some idealised continuous track. hold_max_age_s
should therefore track how staleness is actually treated downstream (the map
UI's own staleness cutoff, or state.py's track-expiry window), not be tuned
to flatter the numbers.

A key's held coverage need not be one contiguous run: --cv-fit deferred can
leave a gap between two publishes longer than hold_max_age_s in the *middle*
of a key's life, not just trailing off at its end (sparse publishes are the
normal case there, not an edge case). _build_tracks segments a key at every
such gap into one Track per contiguous covered run, rather than a single
Track carrying a stateless hole in the middle of its states. That distinction
matters because stonesoup's TrackToTruth associator builds its association
time-ranges from a track's own state timestamps, and a range spanning a
stateless grid point makes SIAPMetrics.accuracy_at_time index a timestamp the
track never had a state for, raising IndexError -- segmenting at the gap
means every range a Track can be assigned always has a state at every
timestamp inside it. GOSPA is unaffected either way: it scores states per
timestamp, never a track-level range. A key that is covered at every grid
point (the only case exercised before this existed) still produces exactly
one Track, byte-identical to before.

Units and frames
-----------------
Internally everything is metres and m/s, in a local ENU (East-North-Up,
minus Up) frame about a single fixed reference point -- the fleet centroid,
computed once per run.  The conversion is the simple equirectangular
approximation (n_m = (lat-ref_lat)*111320, e_m = (lon-ref_lon)*111320*cos(
ref_lat)); it is not geodesically exact, but every node in one bench run
sits within a few tens of km of the reference, and at that scale the error
is centimetres against a MATCH_KM=8 km gate -- not worth a real projection
library for a bench script. Everything surfaced to a human (format_report_
lines, the headline dict's *_m fields as reported) is converted back to km.

GOSPA configuration
--------------------
p=1, c=match_km*1000 (metres).  alpha is NOT a constructor argument on
stonesoup's GOSPAMetric -- its __init__ hard-codes self.alpha = 2 and its own
docstring says this implementation is "for alpha = 2"; passing alpha=2 (or
any value) as a kwarg raises TypeError, since it is not one of the class's
declared Properties. It is mentioned here only because the decomposition
below assumes alpha=2, which is simply what this stonesoup version always
does. p=1 is not an arbitrary choice: at p=1 GOSPA's decomposition is exactly
linear, i.e.

    distance = localisation + missed + false

with no cross term, so "missed" and "false" can be read directly as costs in
the same units as "distance" rather than needing the p-th-root unwinding
p=2 would require. At alpha=2 (stonesoup's fixed value) each missed target
and each false track costs exactly c/2, so dividing the missed (or false)
cost by c/2 recovers the *average count* of missed targets (or false tracks)
per evaluated timestamp -- e.g. a missed cost of 5.2 km at c=8 km means an
average of 5.2/4.0 = 1.3 missed targets per step, which is the
``gospa_missed_avg_targets`` / ``gospa_false_avg_tracks`` fields below.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

# ── Optional stonesoup import ────────────────────────────────────────────
# Every name this module needs from stonesoup is imported here, once, inside
# a single try/except.  If stonesoup is not installed (the common case
# outside the bench's own docker image), STONESOUP_AVAILABLE is False and
# MetricRecorder refuses to construct -- nothing else in this module (or in
# association_bench.py) touches stonesoup directly, so nothing else can
# break from its absence.
STONESOUP_AVAILABLE: bool
STONESOUP_VERSION: str | None
try:
    import stonesoup  # noqa: F401 -- imported only to read __version__ below
    from stonesoup.dataassociator.tracktotrack import TrackToTruth
    from stonesoup.measures import Euclidean
    from stonesoup.metricgenerator.manager import MultiManager
    from stonesoup.metricgenerator.ospametric import GOSPAMetric
    from stonesoup.metricgenerator.tracktotruthmetrics import SIAPMetrics
    from stonesoup.types.array import StateVector
    from stonesoup.types.groundtruth import GroundTruthPath, GroundTruthState
    from stonesoup.types.state import State
    from stonesoup.types.track import Track

    STONESOUP_AVAILABLE = True
    STONESOUP_VERSION = getattr(stonesoup, "__version__", None)
except ImportError:
    STONESOUP_AVAILABLE = False
    STONESOUP_VERSION = None


# Equirectangular local-ENU approximation -- see module docstring.  Same
# constant association_bench.py's own haversine-adjacent code would use;
# kept local here rather than imported from services.geo so this module has
# zero import surface into production code (bench-only, by design).
_METERS_PER_DEG = 111320.0

# The seven SIAP TimeRangeMetric titles stonesoup's SIAPMetrics generator
# produces, in the order they are reported.  Titles, not attribute names --
# stonesoup identifies its metrics by these strings, so this list IS the
# contract with the library (see _find_metric_by_title).
_SIAP_TITLES = (
    "SIAP Completeness",
    "SIAP Ambiguity",
    "SIAP Spuriousness",
    "SIAP Position Accuracy",
    "SIAP Velocity Accuracy",
    "SIAP Rate of Track Number Change",
    "SIAP Longest Track Segment",
)


def _lookup_generator(metrics: dict, generator_name: str):
    """Fetch one generator's metrics sub-object out of generate_metrics()'s output.

    Documented shape (stonesoup 1.9.1): MultiManager.generate_metrics()
    returns {generator_name: {metric_title: metric, ...}, ...}, so
    metrics[generator_name] is the primary path.  There is no CI coverage of
    this module against a real stonesoup install in this dev image (the
    package is bench-only and lives in a separate docker image), so this
    falls back to scanning values for anything dict-shaped rather than
    trusting the key literally -- cheap insurance against a point release
    reshaping the container.
    """
    sub = metrics.get(generator_name)
    if sub is not None:
        return sub
    for v in metrics.values():
        if isinstance(v, dict):
            return v
    return metrics


def _find_metric_by_title(container, title: str):
    """Pull one named metric out of a generator's metrics sub-object.

    Handles the documented shape (a dict keyed by title), a bare list/tuple/
    set of metric objects (each carrying its own ``.title``), or a single
    metric object -- whichever shape this version of stonesoup hands back.
    """
    if container is None:
        return None
    if isinstance(container, dict):
        if title in container:
            return container[title]
        for v in container.values():
            if getattr(v, "title", None) == title:
                return v
        return None
    if isinstance(container, (list, tuple, set)):
        for v in container:
            if getattr(v, "title", None) == title:
                return v
        return None
    # A single bare metric object (not a container at all).
    if getattr(container, "title", title) == title:
        return container
    return None


def _scalar_from_metric(metric) -> float:
    """A SIAP TimeRangeMetric's .value is documented as a plain scalar.

    Defensive against a shape where .value turns out to be a per-timestamp
    list of SingleTimeMetric instead (as GOSPA's is) -- if so, average it
    rather than raising.
    """
    if metric is None:
        return 0.0
    value = getattr(metric, "value", metric)
    if isinstance(value, (list, tuple)):
        vals = [float(getattr(v, "value", v)) for v in value]
        return statistics.mean(vals) if vals else 0.0
    return float(value)


def _gospa_component_means(metric) -> dict[str, float]:
    """Average GOSPA's four cost components over every evaluated timestamp.

    metric.value is documented as a list of SingleTimeMetric, one per
    evaluated timestamp, each of whose own .value is the
    {'distance', 'localisation', 'missed', 'false'} dict for that instant.
    Averaging those over time gives the per-step figures this module
    reports.  Defensive against a single bare dict too (in case a version
    skips the per-timestamp wrapping when there is only one timestamp).
    """
    keys = ("distance", "localisation", "missed", "false")
    sums: dict[str, list[float]] = {k: [] for k in keys}
    if metric is None:
        return dict.fromkeys(keys, 0.0)
    values = getattr(metric, "value", metric)
    if not isinstance(values, (list, tuple)):
        values = [values]
    for v in values:
        d = getattr(v, "value", v)
        if not isinstance(d, dict):
            continue
        for k in keys:
            if k in d:
                sums[k].append(float(d[k]))
    return {k: (statistics.mean(v) if v else 0.0) for k, v in sums.items()}


class MetricRecorder:
    """Accumulates one bench run's truth/publish series and scores them.

    Usage mirrors the two hooks association_bench.py's run() already has:
    call record_truth() once per world step (every aircraft's current
    state) and record_publish() once per accepted solve (its published
    lat/lon/velocity).  Call compute() once at the end of the run to get a
    plain dict of scalars, or None if there was nothing to score.

    See the module docstring for why the truth/publish series are resampled
    onto a uniform grid before ever reaching Stone-Soup, and for the GOSPA
    p=1/alpha=2 configuration's meaning.
    """

    def __init__(self, ref_lat: float, ref_lon: float, duration_s: float,
                 sample_dt_s: float = 5.0, hold_max_age_s: float = 12.0,
                 match_km: float = 8.0,
                 epoch: datetime = datetime(2026, 1, 1)):
        if not STONESOUP_AVAILABLE:
            # Callers are expected to check STONESOUP_AVAILABLE (or catch
            # this) before constructing -- association_bench.py's --ss-
            # metrics auto/on/off resolution does this so the message below
            # should be unreachable in practice, but a direct import of this
            # module without that guard should fail loudly, not silently
            # produce a recorder that can never compute anything.
            raise RuntimeError(
                "stonesoup is not installed; MetricRecorder requires it "
                "(pip package `stonesoup`, present in the bench's docker "
                "image but not a dependency of the shipped services/ code). "
                "Check stonesoup_metrics.STONESOUP_AVAILABLE before "
                "constructing a MetricRecorder.",
            )
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.duration_s = duration_s
        self.sample_dt_s = sample_dt_s
        self.hold_max_age_s = hold_max_age_s
        self.match_km = match_km
        self.epoch = epoch
        self._ref_cos_lat = math.cos(math.radians(ref_lat))

        # object_id -> {grid_t: (e_m, n_m, ve_ms, vn_ms)} -- one entry per
        # grid point the aircraft existed at.
        self._truth: dict[str, dict[float, tuple]] = defaultdict(dict)
        # key -> [(t_s, e_m, n_m, ve_ms, vn_ms), ...], append-ordered (the
        # bench calls record_publish forward in simulated time, so this list
        # stays sorted by construction -- compute() relies on that for
        # bisect).
        self._publishes: dict[str, list[tuple]] = defaultdict(list)
        # Next grid point due to be recorded -- record_truth snaps whatever
        # world state it is given onto this, then advances it.
        self._next_truth_grid_t = 0.0

        # The dense evaluation grid, 0..duration_s at sample_dt_s -- fixed
        # up front from duration_s rather than derived from whatever truth/
        # publish data happens to arrive, so both series are laid over
        # exactly the same timeline regardless of when aircraft come and go
        # or when publishes start/stop.
        n_points = int(duration_s // sample_dt_s) + 1
        self._grid_times = [round(i * sample_dt_s, 6) for i in range(max(n_points, 1))]

    def _enu(self, lat: float, lon: float) -> tuple[float, float]:
        """Equirectangular local-ENU offset from (ref_lat, ref_lon), in metres."""
        n_m = (lat - self.ref_lat) * _METERS_PER_DEG
        e_m = (lon - self.ref_lon) * _METERS_PER_DEG * self._ref_cos_lat
        return e_m, n_m

    def record_truth(self, t_s: float, aircraft_iter) -> None:
        """Snapshot world.aircraft onto the grid -- call once per world step.

        Only actually stores anything when t_s has reached (or passed) the
        next grid point; most calls between grid points are no-ops.  The
        `while` (rather than `if`) covers the case where a world step is
        larger than sample_dt_s -- unusual for this bench's default dt=1s,
        sample_dt_s=5s, but cheap to handle so a config change can't silently
        skip grid points.
        """
        aircraft = list(aircraft_iter)
        while t_s >= self._next_truth_grid_t - 1e-9:
            grid_t = self._next_truth_grid_t
            for ac in aircraft:
                e_m, n_m = self._enu(ac.lat, ac.lon)
                speed_ms = ac.speed_km_s * 1000.0
                heading_rad = math.radians(ac.heading_deg)
                # heading 0deg = North, 90deg = East (world.py's own
                # convention -- see SimulatedAircraft.heading_deg).
                ve_ms = speed_ms * math.sin(heading_rad)
                vn_ms = speed_ms * math.cos(heading_rad)
                self._truth[ac.object_id][grid_t] = (e_m, n_m, ve_ms, vn_ms)
            self._next_truth_grid_t += self.sample_dt_s

    def record_publish(self, t_s: float, key: str, lat: float, lon: float,
                       vel_east_ms: float = 0.0, vel_north_ms: float = 0.0) -> None:
        """Record one accepted solve -- call once per bench_mn publish.

        Unlike record_truth, every call is stored (no grid snapping): the
        grid alignment happens at compute() time, by holding each key's
        latest publish forward across grid points (see class docstring).
        """
        e_m, n_m = self._enu(lat, lon)
        self._publishes[key].append((t_s, e_m, n_m, vel_east_ms, vel_north_ms))

    def _build_truth_paths(self):
        """One GroundTruthPath per object_id, states at every grid point it existed."""
        paths = set()
        for object_id, grid_map in self._truth.items():
            path = GroundTruthPath(id=str(object_id))
            for grid_t in self._grid_times:
                sample = grid_map.get(grid_t)
                if sample is None:
                    continue
                e_m, n_m, ve_ms, vn_ms = sample
                ts = self.epoch + timedelta(seconds=grid_t)
                path.append(GroundTruthState(StateVector([e_m, ve_ms, n_m, vn_ms]), timestamp=ts))
            if len(path) > 0:
                paths.add(path)
        return paths

    def _build_tracks(self):
        """One Track per CONTIGUOUS run of held publishes for a key.

        At each grid point, a key is "covered" iff some publish for it has
        happened at or before that point AND it is no older than
        hold_max_age_s -- i.e. exactly the "what does a viewer still see"
        rule from the module docstring.  No dead-reckoning: position and
        velocity are the held publish's own values, unchanged.

        Coverage need not be contiguous across the whole run: sparse
        publishes (--cv-fit deferred) can leave an internal gap longer than
        hold_max_age_s between two publishes for the same key.  Building one
        Track per key regardless would leave that Track with a hole -- no
        state at the grid points inside the gap -- and stonesoup's
        TrackToTruth associator builds its association time-ranges from a
        track's own state timestamps, so a range spanning the hole makes
        SIAPMetrics.accuracy_at_time index a timestamp the track has no
        state for, raising IndexError.  So a key is instead segmented at
        every gap: a new Track starts on the first covered grid point after
        an uncovered stretch (or at the run's start), and the current Track
        is closed on the next uncovered point.

        The first segment keeps id str(key), so a key that is covered at
        every grid point -- the only case this had before segmentation
        existed -- still produces exactly one Track, byte-identical to
        before.  Only a key with an actual internal gap produces more than
        one, numbered f"{key}#s{n}" (n = 1, 2, ...) from the second segment
        on.
        """
        tracks = set()
        for key, samples in self._publishes.items():
            if not samples:
                continue
            times = [s[0] for s in samples]
            current = None
            next_segment_n = 0
            for grid_t in self._grid_times:
                # Index of the latest publish at or before this grid point.
                idx = bisect.bisect_right(times, grid_t) - 1
                sample = samples[idx] if idx >= 0 else None
                covered = sample is not None and (grid_t - sample[0]) <= self.hold_max_age_s
                if not covered:
                    if current is not None and len(current) > 0:
                        tracks.add(current)
                    current = None
                    continue
                if current is None:
                    track_id = (str(key) if next_segment_n == 0
                               else f"{key}#s{next_segment_n}")
                    current = Track(id=track_id)
                    next_segment_n += 1
                _t_pub, e_m, n_m, ve_ms, vn_ms = sample
                ts = self.epoch + timedelta(seconds=grid_t)
                current.append(State(StateVector([e_m, ve_ms, n_m, vn_ms]), timestamp=ts))
            if current is not None and len(current) > 0:
                tracks.add(current)
        return tracks

    def compute(self) -> dict | None:
        """Score the recorded run. None if there is nothing to score.

        Returns a flat, json-safe dict of floats (see module docstring for
        the GOSPA p=1/alpha=2 configuration this relies on to turn missed/
        false costs into average target/track counts).
        """
        if not self._truth or not self._publishes:
            return None

        truth_paths = self._build_truth_paths()
        tracks = self._build_tracks()
        if not truth_paths or not tracks:
            # Everything recorded fell outside the grid (e.g. a run shorter
            # than one sample_dt_s) -- nothing meaningful to score, and
            # handing stonesoup two empty sets is more likely to raise than
            # to return a sensible zeroed metric.
            return None

        c = self.match_km * 1000.0
        manager = MultiManager(
            generators=[
                # No alpha kwarg: GOSPAMetric has no such Property (any
                # extra kwarg raises TypeError under stonesoup's Base/
                # Property mechanism) -- alpha=2 is hard-coded inside its
                # __init__ and is not configurable.  See module docstring.
                GOSPAMetric(
                    p=1, c=c, measure=Euclidean((0, 2)),
                    generator_name="gospa", tracks_key="tracks",
                    truths_key="groundtruth_paths",
                ),
                SIAPMetrics(
                    position_measure=Euclidean((0, 2)),
                    velocity_measure=Euclidean((1, 3)),
                    generator_name="siap", tracks_key="tracks",
                    truths_key="groundtruth_paths",
                ),
            ],
            associator=TrackToTruth(association_threshold=c, measure=Euclidean((0, 2))),
        )
        manager.add_data({"groundtruth_paths": truth_paths, "tracks": tracks})
        metrics = manager.generate_metrics()

        gospa_generator = _lookup_generator(metrics, "gospa")
        # GOSPAMetric.compute_over_time (stonesoup/metricgenerator/ospametric.py)
        # returns a TimeRangeMetric titled "GOSPA Metrics" (plural) when more
        # than one distinct timestamp is present across all truth/track
        # states, but degrades to a single bare SingleTimeMetric titled
        # "GOSPA Metric" (singular) in the one-timestamp edge case (e.g. a
        # very short run collapsing to one grid point) -- try both rather
        # than assuming the plural, multi-timestamp shape always applies.
        gospa_metric = (_find_metric_by_title(gospa_generator, "GOSPA Metrics")
                        or _find_metric_by_title(gospa_generator, "GOSPA Metric"))
        gospa = _gospa_component_means(gospa_metric)

        siap_generator = _lookup_generator(metrics, "siap")
        siap = {
            title: _scalar_from_metric(_find_metric_by_title(siap_generator, title))
            for title in _SIAP_TITLES
        }

        # alpha=2: each missed target and each false track costs exactly
        # c/2 in the GOSPA decomposition, so dividing the pooled cost by c/2
        # recovers the average count per evaluated timestamp -- see module
        # docstring.
        half_c = (c / 2.0) or 1.0
        return {
            "gospa_distance_m": float(gospa["distance"]),
            "gospa_localisation_m": float(gospa["localisation"]),
            "gospa_missed_m": float(gospa["missed"]),
            "gospa_false_m": float(gospa["false"]),
            "gospa_missed_avg_targets": float(gospa["missed"] / half_c),
            "gospa_false_avg_tracks": float(gospa["false"] / half_c),
            "siap_completeness": float(siap["SIAP Completeness"]),
            "siap_ambiguity": float(siap["SIAP Ambiguity"]),
            "siap_spuriousness": float(siap["SIAP Spuriousness"]),
            "siap_pos_accuracy_m": float(siap["SIAP Position Accuracy"]),
            "siap_vel_accuracy_ms": float(siap["SIAP Velocity Accuracy"]),
            "siap_rate_track_num_change": float(siap["SIAP Rate of Track Number Change"]),
            "siap_longest_track_segment": float(siap["SIAP Longest Track Segment"]),
            "n_timestamps": float(len(self._grid_times)),
            "n_truth_paths": float(len(truth_paths)),
            "n_tracks": float(len(tracks)),
            "sample_dt_s": float(self.sample_dt_s),
            "hold_max_age_s": float(self.hold_max_age_s),
            "match_km": float(self.match_km),
        }


def format_report_lines(metrics: dict, indent: str = "  ") -> list[str]:
    """Format a MetricRecorder.compute() dict into the bench's print style.

    Deliberately has no stonesoup dependency -- it is pure dict formatting,
    so association_bench.py's report() can call it on a dict that was
    computed elsewhere (a different process, a test, a --repeat aggregate)
    even in a process where stonesoup itself is not importable.  Distances
    are reported in km (the bench's existing convention -- see MATCH_KM and
    every *_km field on Result) even though the dict itself carries metres.
    """
    dt = metrics.get("sample_dt_s", 0.0)
    hold = metrics.get("hold_max_age_s", 0.0)
    match_km = metrics.get("match_km", 0.0)
    dist_km = metrics.get("gospa_distance_m", 0.0) / 1000.0
    loc_km = metrics.get("gospa_localisation_m", 0.0) / 1000.0
    missed_km = metrics.get("gospa_missed_m", 0.0) / 1000.0
    false_km = metrics.get("gospa_false_m", 0.0) / 1000.0
    missed_tgt = metrics.get("gospa_missed_avg_targets", 0.0)
    false_trk = metrics.get("gospa_false_avg_tracks", 0.0)

    lines = [
        f"{indent}stonesoup [dt={dt:g}s hold={hold:g}s c={match_km:g}km p=1]: "
        f"GOSPA dist {dist_km:.2f} km/step = loc {loc_km:.2f} + "
        f"missed {missed_km:.2f} (~{missed_tgt:.2f} tgt) + "
        f"false {false_km:.2f} (~{false_trk:.2f} trk)",
        f"{indent}SIAP: completeness {metrics.get('siap_completeness', 0.0):.2f}  "
        f"ambiguity {metrics.get('siap_ambiguity', 0.0):.2f}  "
        f"spuriousness {metrics.get('siap_spuriousness', 0.0):.2f}  "
        f"pos-acc {metrics.get('siap_pos_accuracy_m', 0.0) / 1000.0:.2f} km  "
        f"vel-acc {metrics.get('siap_vel_accuracy_ms', 0.0):.1f} m/s  "
        f"track-swaps {metrics.get('siap_rate_track_num_change', 0.0):.3f}/s  "
        f"longest-seg {metrics.get('siap_longest_track_segment', 0.0):.2f}",
    ]
    return lines
