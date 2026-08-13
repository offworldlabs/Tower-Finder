"""The one configuration validator, shared by registration and PUT /nodes/config.

Bounds are the wire contract's, at version 1.1.0. Three checks are here and not
there because a JSON schema cannot express them: a receiver and illuminator at
the same point give the solver a degenerate baseline; bool is a subclass of int
in Python, so a plain range check accepts True as a latitude of 1; and NaN
compares false against every bound, so it survives a range check untouched.

A leaf on purpose. It takes a dict and returns a dict, knowing nothing of identity,
HTTP or status codes, so both callers can share it and it stays testable without a
database.
"""

import math
from typing import Any

# About 0.11 m. Below this the receiver and illuminator are the same point as far as
# the solver is concerned, whatever the node believes it measured.
_MIN_BASELINE_DEG = 1e-6


class ConfigInvalid(Exception):
    def __init__(self, field: str, reason: str = "out of range") -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


# field -> (low, high, low_inclusive, high_inclusive). beam_azimuth_deg is absent
# because it is nullable and handled separately.
_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool, bool]] = {
    "rx_lat": (-90, 90, True, True),
    "rx_lon": (-180, 180, True, True),
    "rx_alt_ft": (-1500, 30000, True, True),
    "tx_lat": (-90, 90, True, True),
    "tx_lon": (-180, 180, True, True),
    "tx_alt_ft": (-1500, 30000, True, True),
    "fc_hz": (1_000_000, 6_000_000_000, True, True),
    "fs_hz": (100_000, 20_000_000, True, True),
    "beam_width_deg": (0, 360, False, True),
    "max_range_km": (0, 1000, False, True),
    "cpi_s": (0, 10, False, True),
    # The contract sets no maximum on either tolerance, so the bound is math.inf
    # rather than a ceiling of our own: _number has already rejected infinities, so
    # nothing that reaches here can equal it, and inventing a limit would reject a
    # config the contract permits. A plausibility bound is a separate decision from
    # a conformance one, and this module only makes the latter.
    "delay_tolerance_us": (0, math.inf, False, True),
    "doppler_tolerance_hz": (0, math.inf, False, True),
}

_REQUIRED = set(_NUMERIC_BOUNDS) | {"tx_callsign", "beam_azimuth_deg"}


def _number(field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigInvalid(field, "not a number")
    try:
        number = float(value)
    except OverflowError:
        # JSON puts no ceiling on integer literals, so a node can send an int with no
        # float representation. Rejected rather than left to raise out of the route.
        raise ConfigInvalid(field, "out of range") from None
    if not math.isfinite(number):
        raise ConfigInvalid(field, "not a finite number")
    return number


def validate_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the normalised configuration, or raise ConfigInvalid naming one field."""
    # A JSON body is not necessarily an object, and every check below assumes it is:
    # set(None) and sorted() over mixed types both raise TypeError, which would leave
    # the route with no ConfigInvalid to map and answer a malformed body with a 500.
    # "config" is the key registration nests this under, and the whole body on PUT.
    if not isinstance(payload, dict):
        raise ConfigInvalid("config", "not an object")

    # Sorted so that a payload wrong in several places always names the same field,
    # and a node retrying unchanged always gets the same answer.
    unknown = sorted(set(payload) - _REQUIRED)
    if unknown:
        raise ConfigInvalid(unknown[0], "unknown field")

    missing = sorted(_REQUIRED - set(payload))
    if missing:
        raise ConfigInvalid(missing[0], "missing")

    out: dict[str, Any] = {}
    for field, (low, high, low_inclusive, high_inclusive) in _NUMERIC_BOUNDS.items():
        value = _number(field, payload[field])
        below = value < low if low_inclusive else value <= low
        above = value > high if high_inclusive else value >= high
        if below or above:
            raise ConfigInvalid(field)
        out[field] = value

    callsign = payload["tx_callsign"]
    if not isinstance(callsign, str) or not 1 <= len(callsign) <= 32:
        raise ConfigInvalid("tx_callsign")
    out["tx_callsign"] = callsign

    # null is meaningful: the node is broadside or omnidirectional. Coercing it to 0.0
    # would silently aim every unaimed node in the fleet due north.
    azimuth = payload["beam_azimuth_deg"]
    if azimuth is None:
        out["beam_azimuth_deg"] = None
    else:
        azimuth = _number("beam_azimuth_deg", azimuth)
        if not 0 <= azimuth < 360:
            raise ConfigInvalid("beam_azimuth_deg")
        out["beam_azimuth_deg"] = azimuth

    if (
        abs(out["rx_lat"] - out["tx_lat"]) < _MIN_BASELINE_DEG
        and abs(out["rx_lon"] - out["tx_lon"]) < _MIN_BASELINE_DEG
    ):
        raise ConfigInvalid("tx_lat", "receiver and illuminator are at the same point")

    return out
