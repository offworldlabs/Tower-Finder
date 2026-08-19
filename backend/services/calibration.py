"""One rule for recording empirical-coverage calibration points.

Two call sites accumulated this independently — the frame path in
``frame_processor`` and the solve path in ``tasks/solver`` — and they disagreed
on the only parameter that matters.  The solver gated on
``CAL_MAX_ADSB_AGE_S`` (10 s); the frame path used ``_fresh_adsb``'s 60 s
window, because that helper exists for *display* freshness and was reached for
without the difference being noticed.

At 250 m/s a 60 s fix is 15 km from where the aircraft actually was, against a
5-degree, 72-bin polar grid whose whole purpose is to record where a node can
see.  The solver's own comment already spelled out why 10 s is the limit — "at
250 m/s a 10 s fix is 2.5 km stale, which is already coarse ... beyond that the
point stops describing where the target was when the node detected it" — so the
frame path was violating the stated rule by a factor of six.

Tightening means bins fill more slowly toward
``_MIN_BIN_POINTS_TO_CONSTRAIN``.  Since the prior is shrink-only and abstains
below that floor, the transient effect is *less* constraint on association,
which is the safe direction to be wrong in.

A third rule lives here for the same reason the other two do: two call sites
must not be free to disagree on it.  ``age_s`` and ``CAL_DETECTION_FRESH_S``
(applied by the caller before this function is even reached) both measure
freshness against ``now``, the moment the emit loop happens to run — neither
one bounds the fix against the detection event it is meant to describe.  The
result, diagnosed on staging 2026-08-10: for up to ``CAL_DETECTION_FRESH_S``
after a track's last real detection, the emit loop kept recording the LIVE
ADS-B fix while the aircraft flew on past the node's actual field of view —
at the wedge edge for medium-range nodes, and at bearings 90 degrees or more
off for targets that crossed within a kilometre or two of the RX, where a few
seconds of travel is a huge swing in bearing.  32 of 32 directional nodes on
staging had learned-FOV polygons with lobes outside their theoretical beam
because of exactly this.  ``CAL_FIX_DETECTION_SKEW_S`` closes it by binding
the fix directly to the detection, not to the wall clock: a calibration point
is only ever recorded from a fix taken within that window of the detection it
is attributed to.
"""

import logging
from collections.abc import Iterable

from config.constants import CAL_FIX_DETECTION_SKEW_S, CAL_MAX_ADSB_AGE_S
from core import state
from services.geo import valid_latlon

log = logging.getLogger(__name__)


def record_adsb_calibration(
    node_ids: Iterable[str],
    lat: float | None,
    lon: float | None,
    age_s: float,
    fix_ts: float,
    detection_ts: float,
) -> int:
    """Record one ADS-B fix as a calibration point for each node that saw it.

    The position must come from ADS-B, never from a solve: the coverage polygon
    is used to judge solves, so building it from them would let a phantom widen
    the region that produced it.  Measured blind, 55-85% of n=2 tracks are
    ghosts a median 20+ km from any aircraft.

    fix_ts is the ADS-B fix's own wall-clock timestamp (last_seen_ms / 1000);
    detection_ts is the node's last real detection (track.last_detection_wall_ts).
    Both are required, not optional — see the module docstring's third rule —
    and the point is stamped with detection_ts, not the wall-clock moment this
    function runs, so the bin's positive-timestamp history describes when the
    node actually saw the target, not when this call happened to fire.

    Returns the number of points recorded, so a caller can report rather than
    assume.
    """
    if not valid_latlon(lat, lon):
        return 0
    if age_s > CAL_MAX_ADSB_AGE_S:
        return 0
    if abs(fix_ts - detection_ts) > CAL_FIX_DETECTION_SKEW_S:
        return 0
    recorded = 0
    for nid in node_ids:
        if not nid:
            continue
        state.node_analytics.record_calibration_point(nid, lat, lon, ts=detection_ts)
        recorded += 1
    return recorded
