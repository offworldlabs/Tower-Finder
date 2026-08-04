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
"""

import logging
from collections.abc import Iterable

from config.constants import CAL_MAX_ADSB_AGE_S
from core import state

log = logging.getLogger(__name__)


def record_adsb_calibration(
    node_ids: Iterable[str],
    lat: float | None,
    lon: float | None,
    age_s: float,
) -> int:
    """Record one ADS-B fix as a calibration point for each node that saw it.

    The position must come from ADS-B, never from a solve: the coverage polygon
    is used to judge solves, so building it from them would let a phantom widen
    the region that produced it.  Measured blind, 55-85% of n=2 tracks are
    ghosts a median 20+ km from any aircraft.

    Returns the number of points recorded, so a caller can report rather than
    assume.
    """
    if lat is None or lon is None or (not lat and not lon):
        return 0
    if age_s > CAL_MAX_ADSB_AGE_S:
        return 0
    recorded = 0
    for nid in node_ids:
        if not nid:
            continue
        state.node_analytics.record_calibration_point(nid, lat, lon)
        recorded += 1
    return recorded
