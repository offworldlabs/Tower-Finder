"""Typed definitions for core data structures.

These TypedDicts describe the shapes of dicts stored in state.py.  They are
documentation-first — not enforced at runtime — so introducing them is
backwards-compatible with existing code that constructs plain dicts.
"""

from __future__ import annotations

import time
from typing import TypedDict


class NodeState(TypedDict, total=False):
    """Shape of entries in ``state.connected_nodes``."""
    config_hash: str
    config: dict
    status: str              # "active" | "disconnected"
    last_heartbeat: str      # ISO-8601
    peer: str
    is_synthetic: bool
    capabilities: dict


class AircraftPosition(TypedDict, total=False):
    """Shape of entries in ``state.adsb_aircraft``."""
    hex: str
    callsign: str
    lat: float
    lon: float
    alt_baro: int
    gs: float                # ground speed (knots)
    track: float             # heading (degrees)
    baro_rate: int
    squawk: str
    category: str
    seen: float              # epoch of last update
    rssi: float
    node_id: str


class GeoAircraft(TypedDict, total=False):
    """Shape of entries in the aircraft.json ``aircraft`` list."""
    hex: str
    flight: str
    lat: float
    lon: float
    alt_baro: int
    alt_geom: int
    gs: float
    track: float
    baro_rate: int
    squawk: str
    category: str
    seen: float
    rssi: float
    type: str                # "adsb_icao" | "radar" | ...
    node_id: str
    multi_node: bool
    anomaly: bool
    anomaly_types: list[str]
    trail: list[list[float]]
    arc: list[dict]


class TaskHealth(TypedDict, total=False):
    """Shape of entries exposed in ``/api/test/dashboard`` → ``task_health``."""
    last_success: dict[str, float]   # task_name → epoch
    error_counts: dict[str, int]     # task_name → cumulative
