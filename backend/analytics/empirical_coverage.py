"""Empirical detection-area characterisation built from known-position calibration points.

Instead of assuming a fixed Yagi-like antenna lobe, this module accumulates
ground-truth target positions (from ADS-B or multinode-solver solutions) that a
node has positively detected, then derives a smoothed coverage polygon that
reflects the node's *actual* detection area as observed over time.

Algorithm
---------
1. Each confirmed detection is projected from the RX site into (bearing, range)
   polar coordinates and accumulated in one of N_BINS angular bins (5°/bin).
2. Per bin, the robust range estimate is the 85th-percentile of observed ranges
   (enough samples sit below a farther outlier so we use P85, not max).
3. Bins with no observations are filled by angular-linear interpolation between
   the nearest filled neighbours on each side, with a conservative discount
   (30 %) applied for estimated coverage that we haven't actually seen yet.
4. A circular rolling average (window = 3 bins) smooths the resulting vector.
5. Polygon vertices are computed at each bin centre and returned as [[lat, lon]].

The polygon is only returned once at least MIN_POINTS calibration points have
been recorded; below that the frontend falls back to the theoretical Yagi sector.
"""

import json
import math
import os

N_BINS = 72          # 5 ° per bin  (360 / 5 = 72)
_DEG_PER_BIN = 360.0 / N_BINS
_MAX_PER_BIN = 200   # cap per-bin history to prevent unbounded RAM growth
MIN_POINTS = 20      # minimum calibration points before emitting a polygon


def _bin_for_bearing(bearing_deg: float) -> int:
    return int(bearing_deg / _DEG_PER_BIN) % N_BINS


def _bearing_and_range(rx_lat: float, rx_lon: float,
                       lat: float, lon: float) -> tuple[float, float]:
    """Return (bearing °, range_km) from RX to target."""
    dlat = lat - rx_lat
    cos_lat = math.cos(math.radians(rx_lat))
    dlon = (lon - rx_lon) * cos_lat
    range_km = math.sqrt((dlat * 111.320) ** 2 + (dlon * 111.320) ** 2)
    bearing = math.degrees(math.atan2(dlon, dlat)) % 360.0
    return bearing, range_km


def _p85(values: list[float]) -> float:
    """85th-percentile of a non-empty list (sorts in place)."""
    s = sorted(values)
    idx = min(int(len(s) * 0.85), len(s) - 1)
    return s[idx]


class EmpiricalCoverageState:
    """Accumulates calibration points and derives a smoothed detection polygon."""

    def __init__(self, rx_lat: float, rx_lon: float):
        self.rx_lat = rx_lat
        self.rx_lon = rx_lon
        # Per-bin list of observed ranges (km).  List, not array — no numpy dep.
        self._bins: list[list[float]] = [[] for _ in range(N_BINS)]

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def add_point(self, lat: float, lon: float) -> None:
        """Record one calibration point (known target position)."""
        bearing, range_km = _bearing_and_range(self.rx_lat, self.rx_lon, lat, lon)
        if range_km < 0.5:
            return  # too close — not informative
        b = self._bins[_bin_for_bearing(bearing)]
        b.append(range_km)
        if len(b) > _MAX_PER_BIN:
            del b[0]  # drop oldest

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def n_points(self) -> int:
        return sum(len(b) for b in self._bins)

    @property
    def n_filled_bins(self) -> int:
        return sum(1 for b in self._bins if b)

    # ── Polygon generation ────────────────────────────────────────────────────

    def to_polygon(self, min_points: int = MIN_POINTS) -> list[list[float]] | None:
        """Return a closed polygon [[lat, lon], …] or None if insufficient data."""
        if self.n_points < min_points:
            return None

        # Step 1: robust range per bin (P85, or 0 if empty)
        ranges: list[float] = []
        for b in self._bins:
            ranges.append(_p85(b) if b else 0.0)

        # Step 2: fill empty bins by angular interpolation from neighbours
        for i in range(N_BINS):
            if ranges[i] > 0.0:
                continue
            # Search for nearest filled bin in each direction
            left_dist, left_val = None, None
            for j in range(1, N_BINS):
                lv = ranges[(i - j) % N_BINS]
                if lv > 0.0:
                    left_dist, left_val = j, lv
                    break
            right_dist, right_val = None, None
            for j in range(1, N_BINS):
                rv = ranges[(i + j) % N_BINS]
                if rv > 0.0:
                    right_dist, right_val = j, rv
                    break

            if left_val is None and right_val is None:
                continue
            elif left_val is None:
                est = right_val
                gap = right_dist
            elif right_val is None:
                est = left_val
                gap = left_dist
            else:
                # Weighted interpolation, closer side has more weight
                total = left_dist + right_dist
                est = (left_val * right_dist + right_val * left_dist) / total
                gap = max(left_dist, right_dist)

            # Conservative discount for unobserved gap: 10 % per bin up to 30 %
            discount = max(0.70, 1.0 - 0.10 * gap)
            ranges[i] = est * discount

        # Step 3: circular rolling smooth (window = 3)
        smoothed = [
            (ranges[(i - 1) % N_BINS] + ranges[i] + ranges[(i + 1) % N_BINS]) / 3.0
            for i in range(N_BINS)
        ]

        # Step 4: convert polar → lat/lon
        cos_lat = math.cos(math.radians(self.rx_lat))
        polygon: list[list[float]] = []
        for i, r_km in enumerate(smoothed):
            if r_km < 0.1:
                r_km = 0.1  # prevent degenerate polygon
            bearing_rad = math.radians(i * _DEG_PER_BIN)
            lat = self.rx_lat + (r_km * math.cos(bearing_rad)) / 111.320
            lon = self.rx_lon + (r_km * math.sin(bearing_rad)) / (111.320 * cos_lat)
            polygon.append([round(lat, 5), round(lon, 5)])

        # Close the ring
        polygon.append(polygon[0])
        return polygon

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "rx_lat": self.rx_lat,
            "rx_lon": self.rx_lon,
            "bins": [b[:] for b in self._bins],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmpiricalCoverageState":
        obj = cls(rx_lat=d["rx_lat"], rx_lon=d["rx_lon"])
        for i, b in enumerate(d.get("bins", [])):
            if i < N_BINS:
                obj._bins[i] = list(b)
        return obj

    def save_to_file(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)

    @classmethod
    def load_from_file(cls, path: str) -> "EmpiricalCoverageState":
        with open(path, "r") as f:
            return cls.from_dict(json.load(f))
