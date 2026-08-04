"""Geodesy for the backend: one implementation, re-exported.

The primitives live in ``retina_analytics.constants`` because that is where the
canonical spherical haversine and bearing already were, and because the only
other consumer of them is that same library.  This module re-exports them so
backend code has one import site, and adds the adapters that read a *node config
dict* — the shape backend code actually has in hand, and the shape that was
independently re-derived in four places before this existed.

What was here before: six haversines across three different formulas (two of
them flat-earth, answering 0.175% short), six bearings, five bistatic-delay
computations, and roughly fourteen inline dead-reckoning sites using four
different values for kilometres-per-degree.  A position dead-reckoned in one
module and compared against a gate in another disagreed by up to 0.3%.
"""

from retina_analytics.constants import (  # noqa: F401  (re-exported surface)
    C_KM_US,
    KM_PER_DEG_LAT,
    M_PER_DEG_LAT,
    YAGI_BEAM_WIDTH_DEG,
    bearing_deg,
    bistatic_delay_us,
    bistatic_differential_km,
    bistatic_max_radius_km,
    bistatic_range_limit_km,
    enu_km,
    haversine_km,
    km_per_deg_lon,
    offset_latlon,
    offset_latlon_m,
    point_in_beam,
    resolve_beam_azimuth_deg,
)
from retina_analytics.constants import (
    R_EARTH as R_EARTH_KM,
)

__all__ = [
    "C_KM_US", "KM_PER_DEG_LAT", "M_PER_DEG_LAT", "R_EARTH_KM",
    "YAGI_BEAM_WIDTH_DEG",
    "bearing_deg", "bistatic_delay_us", "bistatic_differential_km",
    "bistatic_max_radius_km", "bistatic_range_limit_km", "enu_km",
    "haversine_km", "km_per_deg_lon", "offset_latlon", "offset_latlon_m",
    "point_in_beam", "resolve_beam_azimuth_deg",
    "node_beam_params", "in_node_beam",
]


def node_beam_params(node_cfg: dict) -> dict:
    """Pull a node's detection-area geometry out of its config dict.

    One place where the defaults live, because the callers disagreed:
    ``solver.py`` read ``beam_width_deg or 41`` while ``manager.py`` read
    ``.get("beam_width_deg", YAGI_BEAM_WIDTH_DEG)``.  Those differ for a config
    carrying ``beam_width_deg: 0`` — the first gives 41°, the second gives a
    node that can see nothing.  The forgiving form wins: a zero beam width is
    always a missing value, never a real antenna.

    ``beam_azimuth_deg`` is None when the node declares no aim *and* has no TX
    to derive broadside from; callers should then skip the bearing test rather
    than invent a direction.
    """
    rx_lat = float(node_cfg.get("rx_lat") or node_cfg.get("lat") or 0)
    rx_lon = float(node_cfg.get("rx_lon") or node_cfg.get("lon") or 0)
    tx_lat = node_cfg.get("tx_lat")
    tx_lon = node_cfg.get("tx_lon")

    if "beam_azimuth_deg" in node_cfg and node_cfg["beam_azimuth_deg"] is not None:
        beam_az = resolve_beam_azimuth_deg(node_cfg, rx_lat, rx_lon, 0.0, 0.0)
    elif tx_lat and tx_lon:
        beam_az = (bearing_deg(rx_lat, rx_lon, float(tx_lat), float(tx_lon))
                   + 90.0) % 360.0
    else:
        beam_az = None

    return {
        "rx_lat": rx_lat,
        "rx_lon": rx_lon,
        "tx_lat": float(tx_lat) if tx_lat else None,
        "tx_lon": float(tx_lon) if tx_lon else None,
        "beam_azimuth_deg": beam_az,
        "beam_width_deg": float(node_cfg.get("beam_width_deg") or YAGI_BEAM_WIDTH_DEG),
        "max_range_km": float(node_cfg.get("max_range_km") or 50.0),
        "max_bistatic_range_km": node_cfg.get("max_bistatic_range_km"),
    }


def in_node_beam(lat: float, lon: float, node_cfg: dict) -> bool:
    """Is (lat, lon) inside this node's detection area?"""
    return point_in_beam(lat, lon, **node_beam_params(node_cfg))
