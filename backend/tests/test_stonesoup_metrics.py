"""Tests for the Stone-Soup GOSPA/SIAP bench adapter (scripts/stonesoup_metrics.py).

Skipped wholesale via pytest.importorskip if stonesoup is not installed: it
is a bench-only dependency that ships in the association_bench.py docker
image, never a requirement for the test suite at large (see that module's
docstring for why services/ must never import it).

Every scenario below is built directly in the local ENU-metres frame
MetricRecorder itself uses internally (see _enu_to_latlon, the exact inverse
of MetricRecorder._enu), rather than picking "realistic" lat/lon values and
hoping the geometry works out.  That makes the expected GOSPA/SIAP numbers
(a 2 km bias reads back as ~2 km localisation, a 30 km-away phantom reads as
a false track, etc.) exact by construction instead of approximate, which is
what keeps the tolerances below as tight as they are.
"""

from __future__ import annotations

import math
import types

import pytest

pytest.importorskip("stonesoup")

from scripts.stonesoup_metrics import MetricRecorder, format_report_lines

# Arbitrary metro-scale reference point -- only its role as the ENU origin
# matters here, not any real-world location.
REF_LAT = 40.0
REF_LON = -74.0
_M_PER_DEG = 111320.0  # must match stonesoup_metrics._METERS_PER_DEG


def _enu_to_latlon(e_m: float, n_m: float) -> tuple[float, float]:
    """Inverse of MetricRecorder._enu -- lets test aircraft be authored
    directly in metres and still round-trip through the same lat/lon
    interface the real bench feeds record_truth/record_publish."""
    lat = REF_LAT + n_m / _M_PER_DEG
    lon = REF_LON + e_m / (_M_PER_DEG * math.cos(math.radians(REF_LAT)))
    return lat, lon


def _velocity_enu(speed_km_s: float, heading_deg: float) -> tuple[float, float]:
    """ve, vn in m/s -- the same heading convention (0=N, 90=E) and formula
    MetricRecorder.record_truth uses internally."""
    speed_ms = speed_km_s * 1000.0
    rad = math.radians(heading_deg)
    return speed_ms * math.sin(rad), speed_ms * math.cos(rad)


def _aircraft_at(object_id: str, e0: float, n0: float, heading_deg: float,
                  speed_km_s: float, t_s: float,
                  offset_e: float = 0.0, offset_n: float = 0.0):
    """A straight-line, constant-velocity fake 'aircraft' at time t_s.

    Stands in for retina_simulation's SimulatedAircraft: only the four
    attributes record_truth actually reads (lat, lon, object_id,
    speed_km_s, heading_deg) are present.  offset_e/offset_n let a caller
    build a second, spatially-biased copy of the same flight (see
    test_offset_and_missing) without duplicating the motion model.
    """
    ve, vn = _velocity_enu(speed_km_s, heading_deg)
    lat, lon = _enu_to_latlon(e0 + ve * t_s + offset_e, n0 + vn * t_s + offset_n)
    return types.SimpleNamespace(lat=lat, lon=lon, object_id=object_id,
                                 speed_km_s=speed_km_s, heading_deg=heading_deg)


def test_perfect_tracks():
    """Two aircraft, publishes exactly on GT every 2 s with matching
    velocity: GOSPA should read almost no localisation/missed/false cost,
    and SIAP completeness/position accuracy should both look clean.
    """
    recorder = MetricRecorder(REF_LAT, REF_LON, duration_s=60.0,
                              sample_dt_s=5.0, hold_max_age_s=12.0)
    # Deliberately slow (<=100 m/s) so that the worst-case 1 s gap between
    # a publish and a 5 s-spaced grid point (publishes are every 2 s, grid
    # points every 5 s -- they don't always land together) stays well under
    # the 200 m assertion below.
    tracks = {
        "A": (0.0, 0.0, 90.0, 0.1),      # due east, 100 m/s
        "B": (-8000.0, 6000.0, 0.0, 0.08),  # due north, 80 m/s, elsewhere
    }
    t = 0.0
    while t <= 60.0:
        aircraft = [_aircraft_at(oid, e0, n0, hdg, spd, t)
                    for oid, (e0, n0, hdg, spd) in tracks.items()]
        recorder.record_truth(t, aircraft)
        if t % 2.0 == 0.0:
            for ac in aircraft:
                ve, vn = _velocity_enu(ac.speed_km_s, ac.heading_deg)
                recorder.record_publish(t, ac.object_id, ac.lat, ac.lon, ve, vn)
        t += 1.0

    metrics = recorder.compute()
    assert metrics is not None
    assert metrics["gospa_distance_m"] < 200.0
    assert metrics["gospa_missed_avg_targets"] < 0.1
    assert metrics["gospa_false_avg_tracks"] < 0.1
    assert metrics["siap_completeness"] > 0.9
    assert metrics["siap_pos_accuracy_m"] < 200.0


def test_offset_and_missing():
    """Track A carries a fixed 2 km eastward bias; GT B is never published.

    Expect ~2 km localisation while both are alive, ~1 missed-target
    average (B, which is never tracked at all), completeness around the
    "half the population" mark, and SIAP position accuracy around 2 km.
    Tolerances are loose (~30%) -- GOSPA/SIAP both show edge effects at the
    first/last grid points where the hold window hasn't settled yet.
    """
    recorder = MetricRecorder(REF_LAT, REF_LON, duration_s=60.0,
                              sample_dt_s=5.0, hold_max_age_s=12.0)
    offset_e = 2000.0  # 2 km fixed eastward bias on the published track only
    t = 0.0
    while t <= 60.0:
        ac_a = _aircraft_at("A", 0.0, 0.0, 90.0, 0.1, t)
        ac_b = _aircraft_at("B", -6000.0, 5000.0, 180.0, 0.08, t)
        recorder.record_truth(t, [ac_a, ac_b])
        if t % 2.0 == 0.0:
            # Same velocity as GT A -- only position carries the bias, so
            # this is a pure localisation offset, not a target the track is
            # failing to keep up with.
            biased = _aircraft_at("A", 0.0, 0.0, 90.0, 0.1, t, offset_e=offset_e)
            ve, vn = _velocity_enu(0.1, 90.0)
            recorder.record_publish(t, "A", biased.lat, biased.lon, ve, vn)
            # GT B: no record_publish call, ever -- it is simply untracked.
        t += 1.0

    metrics = recorder.compute()
    assert metrics is not None
    # Localisation is dominated by the fixed 2 km bias on A; B contributes
    # no localisation cost at all (it is never assigned a track).
    assert 1400.0 < metrics["gospa_localisation_m"] < 2600.0
    # B is untracked for the whole run -> ~1 missed target on average.
    assert 0.7 < metrics["gospa_missed_avg_targets"] < 1.3
    assert metrics["gospa_false_avg_tracks"] < 0.2
    # Only 1 of 2 aircraft is ever tracked.
    assert 0.3 < metrics["siap_completeness"] < 0.7
    assert 1400.0 < metrics["siap_pos_accuracy_m"] < 2600.0


def test_false_track():
    """A perfect track on GT A, plus a static phantom publish stream ~30 km
    away from A's entire flight path: reads back as ~1 false track on
    average and non-trivial SIAP spuriousness.
    """
    recorder = MetricRecorder(REF_LAT, REF_LON, duration_s=60.0,
                              sample_dt_s=5.0, hold_max_age_s=12.0)
    # A's whole path only reaches e = 0.1 km/s * 60 s = 6 km east, so a
    # static phantom at e=40 km stays >= 34 km away from it throughout.
    ghost_lat, ghost_lon = _enu_to_latlon(40000.0, 0.0)
    t = 0.0
    while t <= 60.0:
        ac_a = _aircraft_at("A", 0.0, 0.0, 90.0, 0.1, t)
        recorder.record_truth(t, [ac_a])
        if t % 2.0 == 0.0:
            ve, vn = _velocity_enu(ac_a.speed_km_s, ac_a.heading_deg)
            recorder.record_publish(t, "A", ac_a.lat, ac_a.lon, ve, vn)
            recorder.record_publish(t, "ghost", ghost_lat, ghost_lon, 0.0, 0.0)
        t += 1.0

    metrics = recorder.compute()
    assert metrics is not None
    assert 0.6 < metrics["gospa_false_avg_tracks"] < 1.4
    assert metrics["siap_spuriousness"] > 0.3


def test_no_data_returns_none():
    """A recorder with truth but no publishes at all must return None from
    compute(), not raise -- this is the "nothing to score" degrade path."""
    recorder = MetricRecorder(REF_LAT, REF_LON, duration_s=30.0,
                              sample_dt_s=5.0, hold_max_age_s=12.0)
    ac = _aircraft_at("A", 0.0, 0.0, 90.0, 0.1, 0.0)
    recorder.record_truth(0.0, [ac])
    assert recorder.compute() is None


def test_report_lines_format():
    """format_report_lines is pure dict formatting (no stonesoup involved)
    so it must work standalone, return several lines, and actually
    interpolate the numbers rather than leaving format placeholders."""
    metrics = {
        "gospa_distance_m": 9410.0,
        "gospa_localisation_m": 2100.0,
        "gospa_missed_m": 5200.0,
        "gospa_false_m": 2110.0,
        "gospa_missed_avg_targets": 1.3,
        "gospa_false_avg_tracks": 0.53,
        "siap_completeness": 0.62,
        "siap_ambiguity": 1.03,
        "siap_spuriousness": 0.11,
        "siap_pos_accuracy_m": 1870.0,
        "siap_vel_accuracy_ms": 38.2,
        "siap_rate_track_num_change": 0.02,
        "siap_longest_track_segment": 0.71,
        "n_timestamps": 37.0,
        "n_truth_paths": 12.0,
        "n_tracks": 9.0,
        "sample_dt_s": 5.0,
        "hold_max_age_s": 12.0,
        "match_km": 8.0,
    }
    lines = format_report_lines(metrics)
    assert len(lines) >= 2
    assert any("GOSPA" in line for line in lines)
    assert any("SIAP" in line for line in lines)
    joined = "\n".join(lines)
    assert "9.41" in joined  # gospa_distance_m/1000
    assert "0.62" in joined  # siap_completeness
    assert "1.87" in joined  # siap_pos_accuracy_m/1000


def test_hold_window():
    """Publishes stop at t=20 s while GT continues to t=60 s: track states
    must vanish beyond hold_max_age_s past that last publish, dragging
    completeness well below a run where publishing never stops.
    """
    hold_s = 12.0

    def _run(stop_publishing_at):
        recorder = MetricRecorder(REF_LAT, REF_LON, duration_s=60.0,
                                  sample_dt_s=5.0, hold_max_age_s=hold_s)
        t = 0.0
        while t <= 60.0:
            ac = _aircraft_at("A", 0.0, 0.0, 90.0, 0.1, t)
            recorder.record_truth(t, [ac])
            if t % 2.0 == 0.0 and (stop_publishing_at is None or t <= stop_publishing_at):
                ve, vn = _velocity_enu(ac.speed_km_s, ac.heading_deg)
                recorder.record_publish(t, "A", ac.lat, ac.lon, ve, vn)
            t += 1.0
        return recorder.compute()

    stopped = _run(stop_publishing_at=20.0)
    continuous = _run(stop_publishing_at=None)
    assert stopped is not None
    assert continuous is not None

    # Continuous publishing should look like the perfect-track case:
    # completeness well up near 1.0.
    assert continuous["siap_completeness"] > 0.85
    # Past grid_t = 20 + hold_s = 32, no grid point has a held track left
    # (grid points 35..60 of the 0..60-by-5 grid), so completeness for the
    # whole run comes in well under the continuously-published case.
    assert stopped["siap_completeness"] < continuous["siap_completeness"] - 0.2
    assert stopped["siap_completeness"] < 0.7
