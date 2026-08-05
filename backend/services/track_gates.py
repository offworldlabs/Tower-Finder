"""The per-track feed entry: gates, dead-reckoning, anomaly flags, arcs.

Extracted from frame_processor.py.  ``track_entry`` was a 370-line closure
inside build_combined_aircraft_json; it is the display-side machinery for one
geolocated track — staleness, the speed/RMS gates with their bounded hold,
dead-reckoning, the position-jump and implausible-velocity anomaly flags, and
the memoised single-node ambiguity arc.
"""

import logging
import math

from config.constants import (
    DISPLAY_STALE_TRACK_S,
    GATE_MAX_HOLD_S,
    IMPLAUSIBLE_SPEED_MS,
)
from core import state
from services.calibration import record_adsb_calibration
from services.feed_helpers import (
    _estimate_velocity_from_motion,
    _record_arc_motion,
    _should_log_implausible,
    append_track_history,
    position_distance_km,
    resolve_ground_truth_hex,
)
from services.geo import (
    C_KM_US,
    bearing_deg,
    enu_km,
    haversine_km,
    offset_latlon,
    offset_latlon_m,
)


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    _implausible_now.clear()
    _single_node_arc_cache.clear()


# ── Multi-node result → tar1090-compatible dict ──────────────────────────────

# Hexes/keys currently flagged implausible.  state.implausible_velocity_count
# is documented as "solves whose speed exceeded the bound", but both increment
# sites run on the 1 Hz feed-render cadence — a single persistently-implausible
# track was adding ~60 counts/minute.  Counting *transitions into* the state
# makes the number mean what the docs (and /api/radar/accuracy) say it means.
_implausible_now: set[str] = set()


def _note_implausible(track_key: str, implausible: bool) -> bool:
    """Record plausibility state; return True on a fresh transition into it."""
    if implausible:
        if track_key not in _implausible_now:
            _implausible_now.add(track_key)
            state.bump_counter("implausible_velocity_count")
            return True
        return False
    _implausible_now.discard(track_key)
    return False



# Aliases: tests import both names from this module, and the arc builder below
# reads more clearly with the short ones.  The bodies were duplicates of
# services.geo's — _enu_to_lla additionally used 111.32 and a cos_lat clamp of
# 0.1, which only differs above 84.3° latitude and would have misplaced a target
# there rather than admit the projection had run out.
_bearing_deg = bearing_deg


def _enu_to_lla(rx_lat: float, rx_lon: float, east_km: float, north_km: float) -> list[float]:
    lat, lon = offset_latlon(rx_lat, rx_lon, east_km, north_km)
    return [float(lat), float(lon)]


def _build_single_node_arc(
    track_or_delay,
    node_cfg: dict,
    *,
    target_lat: float | None = None,
    target_lon: float | None = None,
) -> list[list[float]] | None:
    """Build the bistatic-ambiguity arc for one detection.

    When `target_lat`/`target_lon` are provided (i.e. we know roughly where
    the aircraft actually is, via ADS-B or a prior solve), the arc is
    trimmed to a short segment around the known position. This makes the
    frontend trail of successive arcs readable as a chain of distinct
    blips tracing the target's path through the beam, instead of one fat
    smudge of overlapping full-beam loci.

    Without a known position (true unknowns like raw radar-only detections)
    the full beam-spanning locus is returned so the operator can see the
    full ambiguity.
    """
    if isinstance(track_or_delay, (int, float)):
        delay_us = track_or_delay
    else:
        delay_us = getattr(track_or_delay, "latest_delay_us", None)
    if delay_us is None or delay_us <= 0:
        return None

    rx_lat = node_cfg.get("rx_lat")
    rx_lon = node_cfg.get("rx_lon")
    tx_lat = node_cfg.get("tx_lat")
    tx_lon = node_cfg.get("tx_lon")
    if None in (rx_lat, rx_lon, tx_lat, tx_lon):
        return None

    beam_width_deg = float(node_cfg.get("beam_width_deg", 41.0) or 41.0)
    max_range_km = float(node_cfg.get("max_range_km", 50.0) or 50.0)
    beam_azimuth_deg = node_cfg.get("beam_azimuth_deg")
    if beam_azimuth_deg is None:
        beam_azimuth_deg = (_bearing_deg(rx_lat, rx_lon, tx_lat, tx_lon) + 90.0) % 360.0

    tx_east_km, tx_north_km = enu_km(rx_lat, rx_lon, tx_lat, tx_lon)
    baseline_km = math.hypot(tx_east_km, tx_north_km)
    differential_range_km = delay_us * C_KM_US

    def _differential_at(range_km: float, bearing_deg: float) -> float:
        bearing_rad = math.radians(bearing_deg)
        east_km = math.sin(bearing_rad) * range_km
        north_km = math.cos(bearing_rad) * range_km
        tx_dist_km = math.hypot(east_km - tx_east_km, north_km - tx_north_km)
        return tx_dist_km + range_km - baseline_km

    # Ceiling for the per-bearing binary search below.  Note this is a
    # *monostatic* RX-range bound, so a node's bistatic limit cannot be
    # substituted for it directly — doing so would silently truncate every arc.
    #
    # Derive it instead.  Writing D(r) for the differential range along a
    # bearing, the triangle inequality gives D(r) >= 2r - 2*baseline, so the
    # range satisfying a measured differential D is at most D/2 + baseline.
    # That is a tight ceiling that provably never clips the locus.
    max_bistatic_km = node_cfg.get("max_bistatic_range_km")
    if max_bistatic_km is not None:
        max_bistatic_km = float(max_bistatic_km)
        if differential_range_km > max_bistatic_km:
            # The measured delay puts the target beyond what this node can
            # physically detect — there is no arc to draw.
            return None
        search_max_km = differential_range_km / 2.0 + baseline_km
    else:
        search_max_km = max_range_km

    # When we have a known target position, centre the sweep on the bearing
    # from RX to that position and aim for an arc whose ground length is
    # roughly the same regardless of range — short enough to look like a
    # radar blip near the aircraft, long enough to remain visible at
    # continental zoom. Pure angular trimming (e.g. fixed 6°) collapses
    # below 1 pixel at low zooms; targeting an arc length in km solves it.
    if target_lat is not None and target_lon is not None:
        centre_bearing = _bearing_deg(rx_lat, rx_lon, target_lat, target_lon)
        target_range_km = max(
            haversine_km(rx_lat, rx_lon, target_lat, target_lon), 5.0)
        # ~25 km arc length keeps the blip visible at zoom ≥4 and short
        # enough to read as a chain at zoom ≥7 — verified on staging.
        BLIP_ARC_LENGTH_KM = 25.0
        sweep_width_deg = min(beam_width_deg, math.degrees(BLIP_ARC_LENGTH_KM / target_range_km))
        steps = 18
    else:
        centre_bearing = beam_azimuth_deg
        sweep_width_deg = beam_width_deg
        steps = 36

    half_sweep = sweep_width_deg / 2.0
    points: list[list[float]] = []
    for step in range(steps + 1):
        bearing_deg = centre_bearing - half_sweep + sweep_width_deg * (step / steps)
        # Stay within the antenna beam — drop bearings that fall outside.
        delta_from_axis = abs(((bearing_deg - beam_azimuth_deg + 180.0) % 360.0) - 180.0)
        if delta_from_axis > beam_width_deg / 2.0:
            continue
        lo = 0.0
        hi = search_max_km
        if _differential_at(hi, bearing_deg) < differential_range_km:
            continue
        for _ in range(32):
            mid = (lo + hi) / 2.0
            if _differential_at(mid, bearing_deg) < differential_range_km:
                lo = mid
            else:
                hi = mid
        bearing_rad = math.radians(bearing_deg)
        points.append(_enu_to_lla(
            rx_lat,
            rx_lon,
            hi * math.sin(bearing_rad),
            hi * math.cos(bearing_rad),
        ))

    if len(points) < 2:
        return None
    return points



# Per-track single-node arc memo.  _build_single_node_arc is a pure function
# of (delay_us, target position, node geometry); the flush loop rebuilds it
# every cycle even when the detection is unchanged, which is the compute the
# ~4s map pulse traces back to.  Key by (ac_hex, node_id) -- the identity the
# rest of the pipeline and the frontend already use -- with a fingerprint of
# the non-static inputs, so a miss happens exactly when a new detection changes
# them (invalidate on arrival, no timer).  node_cfg is treated as static per
# node_id.  The fingerprint is None-safe: latest_delay_us is None for ADS-B-only
# tracks and rounding None would raise.
_single_node_arc_cache: dict[tuple[str, str | None], tuple[tuple, list | None]] = {}


def _cached_single_node_arc(ac_hex, track, node_cfg, touched_keys):
    """Memoised _build_single_node_arc for the per-geolocated-track arc.

    Returns exactly what
        _build_single_node_arc(track, node_cfg,
                               target_lat=track.lat, target_lon=track.lon)
    returns, but recomputes only when the detection's inputs change.  The arc is
    a pure function of those inputs, so this is transparent to callers and the
    wire format.  ``touched_keys`` accumulates the keys visited this build so the
    caller can evict everything else, bounding the cache to the live fleet.
    """
    node_id = node_cfg.get("node_id")
    key = (ac_hex, node_id)
    touched_keys.add(key)

    delay_us = getattr(track, "latest_delay_us", None)
    fingerprint = (
        None if delay_us is None else round(delay_us, 3),
        round(track.lat, 6),
        round(track.lon, 6),
    )
    cached = _single_node_arc_cache.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    arc = _build_single_node_arc(
        track,
        node_cfg,
        target_lat=track.lat,
        target_lon=track.lon,
    )
    _single_node_arc_cache[key] = (fingerprint, arc)
    return arc


def _record_accuracy_sample(ac_hex: str, error_km: float, position_source: str, ts: float):
    """Append a solver-vs-ADS-B accuracy sample for the rolling accuracy API."""
    state.accuracy_samples.append({
        "hex": ac_hex,
        "error_km": round(error_km, 4),
        "position_source": position_source,
        "ts": round(ts, 1),
    })



def fresh_adsb(ac_hex: str, now: float):
    """Return ADS-B entry for ac_hex, or None if unavailable/stale.

    Checks state.adsb_aircraft first (live feed, < 60 s).  Falls back to
    state.external_adsb_cache (OpenSky snapshot) which the background
    poller already refreshes periodically — no additional staleness guard
    needed here.
    """
    entry = state.adsb_aircraft.get(ac_hex)
    if entry and now - entry.get("last_seen_ms", 0) / 1000 <= 60:
        return entry
    # Fallback: external ADS-B truth sourced from OpenSky Network.
    ext = state.external_adsb_cache.get(ac_hex)
    if ext and ext.get("lat") and ext.get("lon"):
        alt_m = ext.get("alt_m") or 0
        vel = ext.get("velocity") or 0
        hdg = ext.get("heading") or 0
        try:
            alt_ft_val = float(alt_m) / 0.3048
            gs_val = float(vel) * 1.94384
            hdg_val = float(hdg)
        except (TypeError, ValueError):
            alt_ft_val = 0.0
            gs_val = 0.0
            hdg_val = 0.0
        return {
            "hex": ac_hex,
            "lat": ext["lat"],
            "lon": ext["lon"],
            "alt_baro": round(alt_ft_val),
            "gs": round(gs_val, 1),
            "track": round(hdg_val, 1),
            "last_seen_ms": int(now * 1000),
        }
    return None


def track_entry(ac_hex, track, node_cfg, now: float, touched_arc_keys: set):
    # Display staleness.  STALE_TRACK_S (120 s) governs when the *tracker*
    # forgets a track; it was also, implicitly, how long a track with no
    # fresh detections kept being painted on the map.  Because arc tracks
    # are not dead-reckoned, that meant a stationary icon for two minutes
    # while the real aircraft flew ~17 km on — measured on staging as every
    # arc track frozen for up to 141 s.  Stop rendering well before the
    # tracker forgets it, so the track survives for re-acquisition but is
    # not presented as a current fix.
    # Keyed on wall_clock_ts (when data last arrived), not pos_fix_ts (when
    # the position last moved).  passive_radar.py:529 advances pos_fix_ts
    # only when lat/lon actually changes, so a genuinely hovering drone —
    # still being detected every frame — would look stale and get dropped.
    # wall_clock_ts is also what "seen" means in the tar1090 schema.
    _track_ts = getattr(track, "wall_clock_ts", 0.0) or 0.0
    _track_age = (now - _track_ts) if _track_ts else 0.0
    if _track_age > DISPLAY_STALE_TRACK_S:
        return None

    # The solver output is always the primary position.  ADS-B data
    # enriches callsign / altitude / velocity but never overrides the
    # radar-derived position — that's the whole point of the system.
    adsb = fresh_adsb(ac_hex, now)

    # Primary position: always from the LM solver
    solver_lat = round(track.lat, 6)
    solver_lon = round(track.lon, 6)
    lat = solver_lat
    lon = solver_lon

    # When ADS-B seeded the solver, label accordingly
    has_adsb = adsb and adsb.get("lat") and adsb.get("lon")
    position_source = "solver_adsb_seed" if has_adsb else "solver_single_node"

    # Altitude / speed / heading: use ADS-B when available (more precise
    # than the bistatic solver for these quantities), solver otherwise.
    # ADS-B alt_baro can be the string "ground"; coerce to float safely.
    def _num(v, fallback=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(fallback)

    alt_ft = _num(adsb.get("alt_baro", track.alt_ft) if adsb else track.alt_ft)
    gs = round(_num(adsb.get("gs", track.speed_knots) if adsb else track.speed_knots), 1)
    heading = round(_num(adsb.get("track", track.track_angle) if adsb else track.track_angle), 1)

    # Build ambiguity arc for every detection — including ADS-B-anchored
    # ones. The frontend uses these as a radar-style afterglow trail: each
    # WS update produces a new arc tagged with the current timestamp, old
    # arcs fade independently. Without arcs for ADS-B aircraft, the
    # synthetic fleet (where almost every target has ADS-B) gives the
    # impression that nodes are idle, which they're not.
    # Always centre on the bistatic-solved position (track.lat/lon), not
    # on the raw ADS-B GPS fix.  When the ADS-B fix is stale or sits
    # outside the beam (e.g. an aircraft near the TX tower), centering on
    # adsb.lat/lon sweeps the wrong beam sector, yields few or no valid
    # arc points, and the position falls back to an unstable raw solver
    # output that jumps tens of km between frames.  track.lat/lon is
    # always the bistatic-constrained estimate — correct sector, stable arc.
    ambiguity_arc = _cached_single_node_arc(
        ac_hex, track, node_cfg, touched_arc_keys,
    )
    # raw_midpoint_lat/lon: the bistatic arc midpoint *before* any gates
    # or smoothing.  Captured for the arc-motion log so velocity
    # estimation is fed only true bistatic positions, not feedback from
    # the smoothing pipeline or gate-reverted last_emit values.
    raw_midpoint_lat = None
    raw_midpoint_lon = None
    if ambiguity_arc and position_source in ("solver_single_node", "solver_adsb_seed"):
        midpoint = ambiguity_arc[len(ambiguity_arc) // 2]
        lat = round(midpoint[0], 6)
        lon = round(midpoint[1], 6)
        raw_midpoint_lat = lat
        raw_midpoint_lon = lon
        position_source = "single_node_ellipse_arc"

        # Gate-hold bookkeeping shared by the speed and RMS gates below.
        # (The 13-line speed-gate rationale was duplicated here verbatim;
        # it now lives once, beside the gate itself.)
        #
        # Both gates below are bounded by GATE_MAX_HOLD_S.  Reverting sets
        # track_last_emit to the reverted value with a fresh timestamp (see
        # the refresh below), so an unbounded gate compares every future
        # midpoint against a frozen reference and never releases — an
        # absorbing state.  Measured on staging before this bound: every
        # arc track pinned in place for up to 141 s while its aircraft flew
        # on, then a 19.6 km teleport when the gate finally cleared.  Once
        # the hold exceeds the bound we accept the measurement: a noisy
        # position is more honest than a confidently wrong stationary one.
        _hold = state.track_gate_hold.get(ac_hex)
        _hold_since = _hold[0] if _hold else None
        _hold_expired = (
            _hold_since is not None and (now - _hold_since) > GATE_MAX_HOLD_S
        )
        _gate_fired = False

        # Speed gate: if the new arc midpoint moved faster than 800 m/s
        # (~1560 kt) from the last EMITTED position, the tracker has likely
        # mis-associated this frame's measurement to a different target.
        # We compare against ``track_last_emit`` (refreshed every emit)
        # rather than ``track_histories`` (deduped at ~5 m), because dedup
        # can age the history timestamp 20-60 s for slow tracks and let a
        # 20 km mis-association come out under 800 m/s.
        # We revert the icon's lat/lon to the previous emit so a single
        # mis-associated frame doesn't yank the marker.  The arc itself is
        # still emitted: it represents this frame's bistatic measurement,
        # which the user reads as "radar saw something along this curve".
        # Nulling it left the testmap looking like every node was idle
        # whenever a single noisy frame came through.
        _last_emit = state.track_last_emit.get(ac_hex)
        if _last_emit:
            _prev_lat, _prev_lon, _prev_ts = _last_emit
            _dt = now - _prev_ts
            if 0 < _dt < 60:
                _speed_ms = haversine_km(lat, lon, _prev_lat, _prev_lon) * 1000.0 / _dt
                if _speed_ms > 800:
                    _gate_fired = True
                    if not _hold_expired:
                        lat = _prev_lat
                        lon = _prev_lon

        # RMS gate: persistently high rms_delay means the Kalman filter
        # cannot fit the current measurement — the track is in a
        # mis-association state and the icon's midpoint is unreliable.
        # Revert the icon to the last emitted position; keep the arc as
        # the honest "radar saw a detection along this curve" signal.
        # Threshold 7 µs: median rms is ~2 µs for clean tracks; bad
        # actors observed at 8–11 µs over dozens of consecutive frames.
        # Inside the outer `if ambiguity_arc and position_source in (...)`
        # block, so ambiguity_arc is guaranteed non-null here — only rms
        # needs checking.
        _rms = getattr(track, "rms_delay", 0.0) or 0.0
        if _rms > 7.0 and _last_emit:
            _gate_fired = True
            if not _hold_expired:
                lat = _last_emit[0]
                lon = _last_emit[1]

        # Maintain the hold window, and dead-reckon while held.  A held
        # position is otherwise *stationary* while the aircraft flies, which
        # is what turns a short suppression into an error that grows at the
        # target's own ground speed.  The blanket exclusion of arc tracks
        # from dead-reckoning (see the DR block below) is about the normal
        # path, where a fresh measurement lands every frame and competing
        # per-node updates made extrapolation oscillate.  While held we are
        # rejecting the measurement by definition, so the only alternative
        # to coasting is freezing.
        # The hold stores its own anchor (ts, lat, lon) rather than
        # extrapolating from track_last_emit: last_emit is rewritten with
        # the dead-reckoned value each frame, so coasting off it would
        # compound — advancing by the full elapsed hold every frame instead
        # of once.  Anchoring makes the offset a pure function of elapsed
        # hold time.
        if _gate_fired and not _hold_expired:
            if _hold is None:
                _hold = (now, lat, lon)
                state.track_gate_hold[ac_hex] = _hold
            _anchor_ts, _anchor_lat, _anchor_lon = _hold
            _vel_e = getattr(track, "vel_east", 0.0) or 0.0
            _vel_n = getattr(track, "vel_north", 0.0) or 0.0
            _hold_dt = min(now - _anchor_ts, GATE_MAX_HOLD_S)
            lat, lon = _anchor_lat, _anchor_lon
            if _hold_dt > 0.5 and (_vel_e != 0.0 or _vel_n != 0.0):
                _dr_lat, _dr_lon = offset_latlon_m(
                    _anchor_lat, _anchor_lon,
                    east_m=_vel_e * _hold_dt, north_m=_vel_n * _hold_dt,
                )
                lat, lon = round(_dr_lat, 6), round(_dr_lon, 6)
        else:
            state.track_gate_hold.pop(ac_hex, None)
    elif not ambiguity_arc:
        # Arc is None.  Only suppress the track if there was a valid delay
        # measurement (delay > 0) but the arc still failed — which means
        # the solver position is outside the antenna beam and is unreliable.
        # If delay is 0/None the track has no bistatic measurement data and
        # should pass through rather than disappearing due to a missing arc.
        _arc_delay = getattr(track, "latest_delay_us", None)
        if _arc_delay and _arc_delay > 0:
            return None

    # Dead-reckon non-arc tracks (multinode, solver_adsb_seed) from the
    # last fix using stored velocity so positions update smoothly
    # between frame arrivals.  Arc tracks (single_node_ellipse_arc)
    # stay pinned to the bistatic-detected position — attempts to
    # extrapolate the arc midpoint between detections produced visible
    # oscillation because multiple node-pipelines can update the same
    # hex with conflicting track.lat values.  The frontend's own 60 fps
    # icon dead-reckoning still slides the visible aircraft forward
    # between emits using the gs / track fields.
    if position_source != "single_node_ellipse_arc":
        _vel_e = getattr(track, 'vel_east', 0.0) or 0.0
        _vel_n = getattr(track, 'vel_north', 0.0) or 0.0
        _pft = getattr(track, 'pos_fix_ts', 0.0) or getattr(track, 'wall_clock_ts', 0.0) or 0.0
        _dr_elapsed = min(now - _pft, 60.0)
        if _dr_elapsed > 0.5 and (_vel_e != 0.0 or _vel_n != 0.0):
            _dr_lat, _dr_lon = offset_latlon_m(
                lat, lon,
                east_m=_vel_e * _dr_elapsed, north_m=_vel_n * _dr_elapsed,
            )
            lat, lon = round(_dr_lat, 6), round(_dr_lon, 6)

    # Record arc-motion samples from the raw bistatic midpoint (kept
    # for the gs/heading estimator that drives the frontend's icon
    # dead-reckoning on tracks without ADS-B).
    if raw_midpoint_lat is not None:
        _record_arc_motion(ac_hex, raw_midpoint_lat, raw_midpoint_lon, now)

    # ── Position-jump anomaly ──────────────────────────────────────────
    # Flag a track that teleports — the solver mis-associated a far-away
    # measurement and the position leapt across the map.  The tracker's
    # own anomaly checks (supersonic, position_mismatch, identity_swap)
    # all require ADS-B truth, so non-ADS-B synthetic single-node tracks
    # had no jump detection at all; the speed gate above only reverts
    # arc-track positions (silently, no flag) and is skipped entirely
    # when dt ≥ 60 s, and multinode tracks have no gate.  This check is
    # universal: it compares the candidate position (the raw solver /
    # arc-midpoint position, *before* the arc-track revert hides it)
    # against the last emit and flags an implausible jump.
    #
    # state.track_last_emit still holds the PREVIOUS emit here — it is
    # refreshed on the line below.  We compare the FINAL emitted position
    # (post-revert), so an arc jump that the speed gate already caught
    # and reverted is NOT flagged — there's no visible teleport in that
    # case.  Only positions that actually leap on screen get flagged.
    _position_jump = False
    _prev_emit = state.track_last_emit.get(ac_hex)
    if _prev_emit:
        _pe_lat, _pe_lon, _pe_ts = _prev_emit
        _jdt = now - _pe_ts
        _jdist_m = haversine_km(lat, lon, _pe_lat, _pe_lon) * 1000.0
        # Trigger on either: a sustained-interval speed above Mach 3
        # (~1029 m/s — well past any real aircraft, so no false positives
        # on legitimate military supersonic traffic), or an absolute
        # single-emit leap over 30 km regardless of dt (covers the
        # reappear-after-gap case where dt is large and the implied speed
        # looks deceptively low).
        if (0 < _jdt < 120 and _jdist_m / _jdt > 1029.0) or _jdist_m > 30_000.0:
            _position_jump = True

    append_track_history(ac_hex, lat, lon, alt_ft, now)
    # Refresh the speed-gate reference on every emit (dedup-free) so the
    # next frame's gate sees the true emit-to-emit interval.
    state.track_last_emit[ac_hex] = [lat, lon, now]

    # gs/heading fallback for single-node arc tracks without ADS-B: the
    # LM solver routinely emits track.speed_knots in the 0–5 kt range
    # because doppler-only velocity is poorly constrained, and the
    # aircraft sits frozen on the map between sparse bistatic frames.
    # Derive a plausible gs/track from the arc-midpoint displacement
    # logged across recent emits — the only honest velocity signal
    # we have for those tracks.
    if gs < 10 and not has_adsb:
        _est = _estimate_velocity_from_motion(ac_hex, lat, lon, now)
        if _est is not None:
            gs, heading = _est

    # Record ADS-B-verified positions as calibration points for empirical coverage.
    if has_adsb:
        nid = node_cfg.get("node_id")
        adsb_lat = adsb.get("lat", 0)
        adsb_lon = adsb.get("lon", 0)
        # The *reported* position, not the solver's estimate.  This is a
        # characterisation of where the node can see, so it has to be built
        # from positions that are known independently of the thing being
        # characterised — the solve is what the coverage polygon is
        # eventually used to judge.  The previous code was gated on
        # has_adsb but stored solver_lat/solver_lon, so it inherited every
        # solver error while looking like ground truth.
        #
        # Age matters as much as provenance, and this path used to have no
        # age gate at all: _fresh_adsb admits a 60 s fix, which is 15 km of
        # travel.  services.calibration holds the one rule now.
        if nid:
            record_adsb_calibration(
                [nid], adsb_lat, adsb_lon,
                age_s=now - (adsb.get("last_seen_ms", 0) or 0) / 1000.0,
            )
        if nid:
            # Track furthest verified detections per node (for detection range)
            area = state.node_analytics.detection_areas.get(nid)
            if area:
                area.record_verified_detection(adsb_lat, adsb_lon, ac_hex)
        # Track accuracy: haversine(solver, adsb) per aircraft per update
        if adsb_lat and adsb_lon:
            err_km = position_distance_km(solver_lat, solver_lon, adsb_lat, adsb_lon)
            _record_accuracy_sample(ac_hex, err_km, position_source, now)

    rms_delay = round(getattr(track, "rms_delay", 0.0) or 0.0, 3)
    rms_doppler = round(getattr(track, "rms_doppler", 0.0) or 0.0, 2)

    # Anomaly propagation: GeolocatedTrack → state.anomaly_hexes.
    # Fold in the position-jump flag computed above so a teleporting
    # track is marked anomalous even when the tracker itself saw nothing
    # wrong (no ADS-B to contradict, doppler within Mach 1).
    _anom_types = set(getattr(track, "anomaly_types", set()))
    if _position_jump:
        _anom_types.add("position_jump")
    # Same velocity-plausibility marker as the multinode path.  Arc tracks
    # reach it by a different route — the LM solver's own speed_knots, or
    # velocity inferred from arc-midpoint displacement — but the statement
    # is identical: this speed is not one an aircraft produces, so the
    # estimate is untrustworthy.
    _implausible_v = (gs / 1.94384) > IMPLAUSIBLE_SPEED_MS if gs else False
    _note_implausible(ac_hex, _implausible_v)
    if _implausible_v:
        _anom_types.add("implausible_velocity")
        if _should_log_implausible(ac_hex, now):
            logging.warning(
                "Implausible velocity: %.0f kt on %s hex=%s node=%s "
                "rms_delay=%.1f µs adsb=%s lat=%.3f lon=%.3f",
                gs, position_source, ac_hex, node_cfg.get("node_id"),
                rms_delay or 0, bool(has_adsb), lat, lon,
            )
    _is_anom = (
        bool(getattr(track, "is_anomalous", False))
        or _position_jump
        or _implausible_v
    )
    _max_vel = getattr(track, "max_velocity_ms", 0.0)
    _flag_hex = ac_hex
    with state.anomaly_lock:
        if _is_anom:
            state.anomaly_hexes.add(_flag_hex)
        else:
            state.anomaly_hexes.discard(_flag_hex)

    return {
        "hex": ac_hex,
        "ground_truth_hex": resolve_ground_truth_hex(ac_hex, lat, lon),
        "type": "tisb_other",
        "flight": (track.adsb_hex or f"PR{abs(hash(track.track_id)) % 10000:04d}").strip(),
        "alt_baro": round(alt_ft),
        "alt_geom": round(alt_ft),
        "gs": gs,
        "track": heading,
        "lat": lat,
        "lon": lon,
        # Real age of the underlying fix, not 0.  Hardcoding 0 made a
        # frozen track claim to be fresh, so the frontend's
        # STALE_AIRCRAFT_MS and the health checks could never see it —
        # which is how a 120 s stationary icon went unnoticed.
        "seen": round(_track_age, 1),
        "messages": track.n_detections,
        "rssi": -10.0,
        "category": "A3",
        "multinode": False,
        "position_source": position_source,
        "ambiguity_arc": ambiguity_arc,
        "solver_lat": solver_lat,
        "solver_lon": solver_lon,
        "rms_delay": rms_delay,
        "rms_doppler": rms_doppler,
        "delay_us": round(getattr(track, "latest_delay_us", 0.0) or 0.0, 3),
        "doppler_hz": round(getattr(track, "latest_doppler_hz", 0.0) or 0.0, 2),
        "node_id": node_cfg.get("node_id"),
        "target_class": getattr(track, "target_class", None),
        "recent_positions": list(state.track_histories.get(ac_hex, [])),
        "is_anomalous": _is_anom,
        "anomaly_types": sorted(_anom_types) if _anom_types else [],
        "max_velocity_ms": round(_max_vel, 1),
    }
