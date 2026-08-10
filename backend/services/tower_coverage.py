"""Coverage-area-added scoring for candidate illuminator towers.

Ranks a candidate receiver location by the n>=2 solve-coverage area it would
ADD to the current fleet, not by geometric dilution. Motivation: geometry
already supports ~23 m median position accuracy where n>=2 nodes cover a
point, but only a small fraction of the metro grid has any n>=2 coverage at
all — extent, not precision, is the limiter, so a candidate tower is worth
more the more previously-uncovered ground its beam would put under a second
(or third) node.

Deliberate simplification: the candidate is modeled with the fleet's default
Yagi wedge and legacy monostatic range (no max_bistatic_range declared) —
exactly how a node registering without those keys is scored. Every tower at
the same RX therefore shares one candidate footprint model, and the azimuth
sweep answers "where should the antenna point", not "which tower is better
built". All towers passed to one call get identical scores — that is correct
under this model and still usefully ranks fleets with coverage holes;
per-tower differentiation (fc, EIRP-dependent range) is a documented future
refinement.

Pure module: no reads of services.state — the caller (route) injects the
fleet's node geometries.
"""

import math

from retina_analytics.association import _point_in_beam

from config.constants import YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM
from services.geo import bearing_deg, haversine_km

_KM_PER_DEG_LAT = 111.2
_MIN_COS_LAT = 0.01  # floor for cos(rx_lat) so the lon step stays finite at the poles


def annotate_coverage_added(
    towers: list[dict],
    rx_lat: float,
    rx_lon: float,
    geometries: dict,
    *,
    grid_step_km: float = 3.0,
    beam_width_deg: float = YAGI_BEAM_WIDTH_DEG,
    max_range_km: float = YAGI_MAX_RANGE_KM,
    n_azimuths: int = 12,
) -> None:
    """Stamp coverage-added fields onto every tower dict, in place.

    Grids only the candidate's own reachable disk around (rx_lat, rx_lon) —
    cells outside it cannot change what the candidate adds, since the fleet's
    existing coverage there is irrelevant to this candidate. For each surviving
    cell, existing fleet coverage is counted once via `_point_in_beam` (so
    NodeGeometry's fov / coverage_limit semantics apply for free), then a set
    of candidate boresights is swept to find the azimuth that newly covers the
    most n==1 cells (upgrading them to n>=2). Computed once per call and
    stamped identically onto every tower — never recomputed per tower.

    No-op when `towers` or `geometries` is empty; the tower dicts are then
    left without these keys (the ranking sort treats a missing key as 0).
    """
    if not towers or not geometries:
        return

    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    lat_step_deg = grid_step_km / _KM_PER_DEG_LAT
    lon_step_deg = grid_step_km / (_KM_PER_DEG_LAT * cos_lat)
    # Steps-per-radius cancels the 111.2/cos(lat) scaling, so one integer
    # bound covers both axes of the bounding box.
    n_steps = math.ceil(max_range_km / grid_step_km)

    # Precompute once: (bearing_from_rx, existing_fleet_count) per in-range cell.
    cells: list[tuple[float, int]] = []
    for i in range(-n_steps, n_steps + 1):
        cell_lat = rx_lat + i * lat_step_deg
        for j in range(-n_steps, n_steps + 1):
            cell_lon = rx_lon + j * lon_step_deg
            dist = haversine_km(rx_lat, rx_lon, cell_lat, cell_lon)
            if dist > max_range_km:
                continue
            bearing = bearing_deg(rx_lat, rx_lon, cell_lat, cell_lon)
            existing = sum(
                1 for geo in geometries.values() if _point_in_beam(cell_lat, cell_lon, geo)
            )
            cells.append((bearing, existing))

    cell_area_km2 = grid_step_km ** 2
    half_beam = beam_width_deg / 2.0

    best_azimuth = 0.0
    best_area_n2 = -1.0
    best_area_n3 = 0.0
    for k in range(n_azimuths):
        az = k * 360.0 / n_azimuths
        n2_count = 0
        n3_count = 0
        for bearing, existing in cells:
            angle_diff = abs((bearing - az + 180) % 360 - 180)
            if angle_diff > half_beam:
                continue
            if existing == 1:
                n2_count += 1
            elif existing == 2:
                n3_count += 1
        area_n2 = n2_count * cell_area_km2
        area_n3 = n3_count * cell_area_km2
        # Ascending az means the first cell of any tie already holds the
        # lowest azimuth, so only area_n2 / area_n3 need comparing.
        if area_n2 > best_area_n2 or (area_n2 == best_area_n2 and area_n3 > best_area_n3):
            best_azimuth = az
            best_area_n2 = area_n2
            best_area_n3 = area_n3

    added_km2 = round(max(best_area_n2, 0.0), 0)
    n3_km2 = round(max(best_area_n3, 0.0), 0)
    for tower in towers:
        tower["coverage_area_added_km2"] = added_km2
        tower["coverage_area_n3_km2"] = n3_km2
        tower["coverage_best_azimuth_deg"] = float(best_azimuth)
