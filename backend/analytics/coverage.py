"""Historical ADS-B-validated coverage map accumulation with persistent storage."""

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CoverageMapEntry:
    """A single ADS-B-validated detection position."""
    lat: float
    lon: float
    alt_km: float
    timestamp: float
    snr: float
    delay_error: float


@dataclass
class HistoricalCoverageMap:
    """Accumulates ADS-B-validated detection positions over time to build
    a factual coverage map for each node."""
    node_id: str
    entries: list[CoverageMapEntry] = field(default_factory=list)
    max_entries: int = 10000
    _grid: dict[tuple[int, int], dict] = field(default_factory=dict)
    _grid_resolution_deg: float = 0.01  # ~1.1 km

    def add_detection(self, lat: float, lon: float, alt_km: float,
                      snr: float, delay_error: float):
        entry = CoverageMapEntry(
            lat=lat, lon=lon, alt_km=alt_km,
            timestamp=time.time(), snr=snr, delay_error=delay_error,
        )
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]

        grid_key = (
            round(lat / self._grid_resolution_deg),
            round(lon / self._grid_resolution_deg),
        )
        cell = self._grid.get(grid_key)
        if cell is None:
            self._grid[grid_key] = {
                "lat": lat, "lon": lon,
                "count": 1, "avg_snr": snr,
                "first_seen": time.time(), "last_seen": time.time(),
            }
        else:
            cell["count"] += 1
            cell["avg_snr"] = (cell["avg_snr"] * (cell["count"] - 1) + snr) / cell["count"]
            cell["last_seen"] = time.time()

    @property
    def coverage_area_km2(self) -> float:
        cell_area = (self._grid_resolution_deg * 111.0) ** 2
        return len(self._grid) * cell_area

    @property
    def n_grid_cells(self) -> int:
        return len(self._grid)

    def get_coverage_grid(self) -> list[dict]:
        return [
            {
                "lat": cell["lat"],
                "lon": cell["lon"],
                "count": cell["count"],
                "avg_snr": round(cell["avg_snr"], 2),
                "first_seen": cell["first_seen"],
                "last_seen": cell["last_seen"],
            }
            for cell in self._grid.values()
        ]

    def estimate_beam_width(self) -> Optional[float]:
        if len(self.entries) < 20:
            return None
        lats = [e.lat for e in self.entries]
        lons = [e.lon for e in self.entries]
        lats_sorted = sorted(lats)
        lons_sorted = sorted(lons)
        mid = len(lats_sorted) // 2
        center_lat = lats_sorted[mid]
        center_lon = lons_sorted[mid]
        bearings = []
        for e in self.entries:
            dlat = e.lat - center_lat
            dlon = (e.lon - center_lon) * math.cos(math.radians(center_lat))
            b = math.degrees(math.atan2(dlon, dlat)) % 360
            bearings.append(b)
        if not bearings:
            return None
        bearings.sort()
        gaps = [(bearings[i + 1] - bearings[i]) for i in range(len(bearings) - 1)]
        gaps.append(360 - bearings[-1] + bearings[0])
        max_gap_idx = gaps.index(max(gaps))
        rotated = bearings[max_gap_idx + 1:] + bearings[:max_gap_idx + 1]
        if not rotated:
            return None
        spread = (rotated[-1] - rotated[0]) % 360
        return min(spread, 180.0)

    def summary(self) -> dict:
        beam_est = self.estimate_beam_width()
        return {
            "node_id": self.node_id,
            "total_entries": len(self.entries),
            "grid_cells": self.n_grid_cells,
            "coverage_area_km2": round(self.coverage_area_km2, 1),
            "estimated_beam_width_deg": round(beam_est, 1) if beam_est else None,
        }

    def save_to_file(self, path: str):
        data = {
            "node_id": self.node_id,
            "entries": [
                {"lat": e.lat, "lon": e.lon, "alt_km": e.alt_km,
                 "timestamp": e.timestamp, "snr": e.snr,
                 "delay_error": e.delay_error}
                for e in self.entries
            ],
            "grid": {
                f"{k[0]},{k[1]}": v for k, v in self._grid.items()
            },
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    @classmethod
    def load_from_file(cls, path: str) -> "HistoricalCoverageMap":
        with open(path, "r") as f:
            data = json.load(f)
        cmap = cls(node_id=data["node_id"])
        for e in data.get("entries", []):
            cmap.entries.append(CoverageMapEntry(
                lat=e["lat"], lon=e["lon"], alt_km=e["alt_km"],
                timestamp=e["timestamp"], snr=e["snr"],
                delay_error=e["delay_error"],
            ))
        for k_str, v in data.get("grid", {}).items():
            parts = k_str.split(",")
            cmap._grid[(int(parts[0]), int(parts[1]))] = v
        return cmap
