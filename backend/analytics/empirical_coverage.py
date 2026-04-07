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

    def to_polygon(self, min_points: int = MIN_POINTS,
                   beam_azimuth_deg: float | None = None,
                   beam_width_deg: float | None = None) -> list[list[float]] | None:
        """Return a closed polygon [[lat, lon], …] or None if insufficient data.

        When *beam_azimuth_deg* and *beam_width_deg* are provided the polygon
        is constrained to the beam sector (a pie-slice shape starting and ending
        at the RX position).  Bins outside the sector are zeroed so the
        interpolation step never bleeds coverage into directions the antenna
        physically cannot observe.
        """
        if self.n_points < min_points:
            return None

        # --- Determine which bins fall inside the beam sector -----------------
        if beam_azimuth_deg is not None and beam_width_deg is not None:
            half = beam_width_deg / 2.0
            def _in_beam(bin_idx: int) -> bool:
                centre = bin_idx * _DEG_PER_BIN
                diff = (centre - beam_azimuth_deg + 180.0) % 360.0 - 180.0
                return abs(diff) <= half
        else:
            _in_beam = lambda _: True  # noqa: E731 — no constraint

        # Step 1: robust range per bin (P85, or 0 if empty / outside beam)
        ranges: list[float] = []
        for i, b in enumerate(self._bins):
            if not _in_beam(i):
                ranges.append(0.0)
            else:
                ranges.append(_p85(b) if b else 0.0)

        # Step 2: fill empty *in-beam* bins by angular interpolation from neighbours
        for i in range(N_BINS):
            if ranges[i] > 0.0 or not _in_beam(i):
                continue
            left_dist, left_val = None, None
            for j in range(1, N_BINS):
                ni = (i - j) % N_BINS
                if not _in_beam(ni):
                    break  # stop at beam edge
                lv = ranges[ni]
                if lv > 0.0:
                    left_dist, left_val = j, lv
                    break
            right_dist, right_val = None, None
            for j in range(1, N_BINS):
                ni = (i + j) % N_BINS
                if not _in_beam(ni):
                    break  # stop at beam edge
                rv = ranges[ni]
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
                total = left_dist + right_dist
                est = (left_val * right_dist + right_val * left_dist) / total
                gap = max(left_dist, right_dist)

            discount = max(0.70, 1.0 - 0.10 * gap)
            ranges[i] = est * discount

        # Step 3: rolling smooth (window = 3), only among in-beam bins
        smoothed = list(ranges)
        for i in range(N_BINS):
            if not _in_beam(i):
                continue
            prev_i = (i - 1) % N_BINS
            next_i = (i + 1) % N_BINS
            vals = [ranges[i]]
            if _in_beam(prev_i):
                vals.append(ranges[prev_i])
            if _in_beam(next_i):
                vals.append(ranges[next_i])
            smoothed[i] = sum(vals) / len(vals)

        # Step 4: generate sector polygon (pie-slice shape)
        cos_lat = math.cos(math.radians(self.rx_lat))
        polygon: list[list[float]] = []

        # Start at RX (sector tip)
        polygon.append([round(self.rx_lat, 5), round(self.rx_lon, 5)])

        for i in range(N_BINS):
            if not _in_beam(i):
                continue
            r_km = smoothed[i]
            if r_km < 0.1:
                r_km = 0.1
            bearing_rad = math.radians(i * _DEG_PER_BIN)
            lat = self.rx_lat + (r_km * math.cos(bearing_rad)) / 111.320
            lon = self.rx_lon + (r_km * math.sin(bearing_rad)) / (111.320 * cos_lat)
            polygon.append([round(lat, 5), round(lon, 5)])

        # Close back to RX
        polygon.append([round(self.rx_lat, 5), round(self.rx_lon, 5)])

        if len(polygon) < 4:
            return None
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
