"""Tests for tower ranking utilities — source detection, band classification, frequency parsing."""

from routes.towers import _detect_source
from services.tower_ranking import (
    DEFAULT_LIMIT,
    FREQUENCY_MATCH_TOLERANCE_MHZ,
    SENSITIVITY_DBM,
    bearing_to_cardinal,
    classify_band,
    classify_distance,
    fspl,
    haversine,
    initial_bearing,
    parse_geom,
    parse_user_frequencies,
    process_and_rank,
    watts_to_dbm,
)

# ── Auto source detection ────────────────────────────────────────────────────


class TestDetectSource:
    def test_sydney_au(self):
        assert _detect_source(-33.8688, 151.2093) == "au"

    def test_washington_dc_us(self):
        assert _detect_source(38.8977, -77.0365) == "us"

    def test_toronto_ca(self):
        assert _detect_source(43.6532, -79.3832) == "ca"

    def test_anchorage_us(self):
        assert _detect_source(61.2181, -149.9003) == "us"

    def test_honolulu_us(self):
        assert _detect_source(21.3069, -157.8583) == "us"

    def test_unknown_fallback_us(self):
        assert _detect_source(0, 0) == "us"


# ── Broadcast band classification ────────────────────────────────────────────


class TestClassifyBand:
    def test_fm_low_edge(self):
        assert classify_band(87.8) == "FM"

    def test_fm_high_edge(self):
        assert classify_band(108.0) == "FM"

    def test_fm_mid(self):
        assert classify_band(95.5) == "FM"

    def test_below_fm(self):
        assert classify_band(87.7) is None

    def test_vhf_low_edge(self):
        assert classify_band(174) == "VHF"

    def test_vhf_high_edge(self):
        assert classify_band(216) == "VHF"

    def test_vhf_mid(self):
        assert classify_band(195) == "VHF"

    def test_gap_returns_none(self):
        assert classify_band(140) is None

    def test_uhf_low_edge(self):
        assert classify_band(470) == "UHF"

    def test_uhf_high_edge(self):
        assert classify_band(608) == "UHF"

    def test_uhf_mid(self):
        assert classify_band(550) == "UHF"

    def test_above_uhf(self):
        assert classify_band(609) is None


# ── User frequency parsing ───────────────────────────────────────────────────


class TestParseUserFrequencies:
    def test_empty_string(self):
        assert parse_user_frequencies("") == []

    def test_single_freq(self):
        assert parse_user_frequencies("95.5") == [95.5]

    def test_multiple_freqs(self):
        assert parse_user_frequencies("95.5, 177.5, 500") == [95.5, 177.5, 500]

    def test_trailing_comma(self):
        assert parse_user_frequencies("95.5,") == [95.5]

    def test_invalid_values_skipped(self):
        assert parse_user_frequencies("abc, 95.5, xyz") == [95.5]

    def test_max_10_enforced(self):
        assert len(parse_user_frequencies(",".join(str(i) for i in range(1, 20)))) == 10

    def test_zero_skipped(self):
        assert parse_user_frequencies("0, 95.5") == [95.5]

    def test_negative_skipped(self):
        assert parse_user_frequencies("-5, 95.5") == [95.5]


# ── Haversine ────────────────────────────────────────────────────────────────


class TestHaversine:
    def test_same_point_zero(self):
        assert haversine(0, 0, 0, 0) == 0.0

    def test_known_distance(self):
        # Sydney → Melbourne ≈ 714 km
        d = haversine(-33.87, 151.21, -37.81, 144.96)
        assert 700 < d < 730


# ── Bearing ──────────────────────────────────────────────────────────────────


class TestBearing:
    def test_due_north(self):
        b = initial_bearing(0, 0, 1, 0)
        assert abs(b) < 1.0 or abs(b - 360) < 1.0

    def test_due_east(self):
        b = initial_bearing(0, 0, 0, 1)
        assert abs(b - 90) < 1.0

    def test_cardinal_north(self):
        assert bearing_to_cardinal(0) == "N"

    def test_cardinal_south(self):
        assert bearing_to_cardinal(180) == "S"

    def test_cardinal_wrap(self):
        assert bearing_to_cardinal(359) == "N"


# ── FSPL ─────────────────────────────────────────────────────────────────────


class TestFSPL:
    def test_zero_distance_returns_zero(self):
        assert fspl(0, 100) == 0.0

    def test_zero_freq_returns_zero(self):
        assert fspl(10, 0) == 0.0

    def test_positive_loss(self):
        assert fspl(10, 100) > 0


# ── Watts to dBm ─────────────────────────────────────────────────────────────


class TestWattsToDbm:
    def test_one_watt(self):
        assert abs(watts_to_dbm(1.0) - 30.0) < 0.01

    def test_zero_returns_neg_inf(self):
        assert watts_to_dbm(0) == float("-inf")

    def test_negative_returns_neg_inf(self):
        assert watts_to_dbm(-1) == float("-inf")


# ── Parse geometry ───────────────────────────────────────────────────────────


class TestParseGeom:
    def test_point_wkt(self):
        result = parse_geom({"string": "POINT(151.2 -33.87)"})
        assert result is not None
        lat, lon = result
        assert abs(lat - (-33.87)) < 0.01
        assert abs(lon - 151.2) < 0.01

    def test_none_input(self):
        assert parse_geom(None) is None

    def test_empty_string(self):
        assert parse_geom({"string": ""}) is None

    def test_plain_string(self):
        result = parse_geom("POINT(0 0)")
        assert result is not None


# ── Distance classification ──────────────────────────────────────────────────


class TestClassifyDistance:
    def test_very_far(self):
        assert classify_distance(99999) == "Far"

    def test_returns_string(self):
        assert isinstance(classify_distance(5.0), str)


# ── process_and_rank ─────────────────────────────────────────────────────────

# Atlanta, GA — used as our fixed "user" position throughout these tests
_USER_LAT = 33.749
_USER_LON = -84.388


def _device(freq_mhz, lat, lon, callsign="KXXX", eirp_dbm=60.0, antenna_height=100):
    """Build a minimal device dict accepted by process_and_rank."""
    return {
        "frequency": freq_mhz,
        "callsign": callsign,
        "antennaHeight": antenna_height,
        "location": {
            # parse_geom accepts a plain WKT string; lon before lat per WKT convention
            "geom": f"POINT({lon} {lat})",
            "name": "Test Tower",
            "state": "GA",
        },
        "eirp_dbm": eirp_dbm,
    }


def _system(devices, licence_type="", licence_subtype=""):
    """Wrap devices in a raw-system dict."""
    return {
        "licence": {"type": licence_type, "subtype": licence_subtype},
        "devices": devices,
    }


# A valid FM tower ~20 km north of Atlanta — well within the default 80 km radius
_FM_DEVICE = _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="WXYZ")
_FM_SYSTEM = _system([_FM_DEVICE], licence_type="Broadcast", licence_subtype="FM")


class TestProcessAndRank:
    # ── Basic smoke tests ────────────────────────────────────────────────────

    def test_empty_input_returns_empty(self):
        result = process_and_rank([], _USER_LAT, _USER_LON)
        assert result == []

    def test_empty_devices_returns_empty(self):
        result = process_and_rank([_system([])], _USER_LAT, _USER_LON)
        assert result == []

    # ── Single valid FM tower ────────────────────────────────────────────────

    def test_single_fm_tower_returned(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        assert len(result) == 1

    def test_single_fm_tower_fields(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        t = result[0]
        assert t["callsign"] == "WXYZ"
        assert t["frequency_mhz"] == 95.5
        assert t["band"] == "FM"
        assert t["rank"] == 1
        assert isinstance(t["distance_km"], float)
        assert isinstance(t["bearing_deg"], float)
        assert isinstance(t["bearing_cardinal"], str)
        assert isinstance(t["received_power_dbm"], float)
        assert isinstance(t["eirp_dbm"], float)
        assert isinstance(t["distance_class"], str)
        assert t["licence_type"] == "Broadcast"
        assert t["licence_subtype"] == "FM"
        assert t["frequency_matched"] is False

    def test_single_fm_tower_distance_reasonable(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        # Tower is ~20 km north — should be between 15 and 25 km
        assert 15.0 < result[0]["distance_km"] < 25.0

    def test_single_fm_tower_bearing_roughly_north(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        # Tower is directly north; bearing should be close to 0/360
        brg = result[0]["bearing_deg"]
        assert brg < 10 or brg > 350

    # ── Radius filtering ─────────────────────────────────────────────────────

    def test_tower_beyond_radius_excluded(self):
        # Tower is ~20 km away; use a 10 km radius — should be excluded
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, radius_km=10)
        assert result == []

    def test_tower_within_explicit_radius_included(self):
        # Tower is ~20 km away; use a 50 km radius — should be included
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, radius_km=50)
        assert len(result) == 1

    def test_zero_radius_uses_default(self):
        # radius_km=0 should fall back to DEFAULT_RADIUS_KM (80 km).
        # Near tower is ~20 km away (within 80 km); far tower is ~140 km away (clearly beyond).
        far_device = _device(freq_mhz=95.5, lat=35.0, lon=-84.388, callsign="KFAR")
        near_device = _FM_DEVICE  # ~20 km north, callsign WXYZ
        system = _system([near_device, far_device])
        result = process_and_rank([system], _USER_LAT, _USER_LON, radius_km=0)
        callsigns = {t["callsign"] for t in result}
        assert "WXYZ" in callsigns, "Near tower (~20 km) should be included within default 80 km radius"
        assert "KFAR" not in callsigns, "Far tower (~140 km) should be excluded by default 80 km radius"

    # ── Band filtering ───────────────────────────────────────────────────────

    def test_non_broadcast_frequency_excluded(self):
        # 300 MHz falls in no recognised broadcast band
        bad_device = _device(freq_mhz=300.0, lat=33.93, lon=-84.388, callsign="KBAD")
        result = process_and_rank([_system([bad_device])], _USER_LAT, _USER_LON)
        assert result == []

    def test_none_frequency_excluded(self):
        bad_device = {
            "frequency": None,
            "callsign": "KNONE",
            "location": {"geom": "POINT(-84.388 33.93)"},
        }
        result = process_and_rank([_system([bad_device])], _USER_LAT, _USER_LON)
        assert result == []

    # ── Geometry filtering ───────────────────────────────────────────────────

    def test_device_with_no_geom_excluded(self):
        no_geom_device = {
            "frequency": 95.5,
            "callsign": "KNOGEOM",
            "location": {"geom": None},
        }
        result = process_and_rank([_system([no_geom_device])], _USER_LAT, _USER_LON)
        assert result == []

    def test_device_with_missing_location_excluded(self):
        no_loc_device = {
            "frequency": 95.5,
            "callsign": "KNOLOC",
        }
        result = process_and_rank([_system([no_loc_device])], _USER_LAT, _USER_LON)
        assert result == []

    # ── Deduplication ────────────────────────────────────────────────────────

    def test_deduplication_keeps_stronger_signal(self):
        # Two devices with the same callsign+frequency but different distances
        # The closer one (stronger signal) should win
        closer = _device(95.5, 33.85, -84.388, callsign="KDUP", eirp_dbm=60.0)   # ~11 km
        farther = _device(95.5, 33.99, -84.388, callsign="KDUP", eirp_dbm=60.0)  # ~27 km
        result = process_and_rank([_system([closer, farther])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        # Closer tower should be kept (higher received_power_dbm)
        assert result[0]["distance_km"] < 20.0

    def test_deduplication_different_callsigns_both_kept(self):
        dev1 = _device(95.5, 33.85, -84.388, callsign="KAAA", eirp_dbm=60.0)
        dev2 = _device(95.5, 33.86, -84.388, callsign="KBBB", eirp_dbm=60.0)
        result = process_and_rank([_system([dev1, dev2])], _USER_LAT, _USER_LON)
        assert len(result) == 2

    def test_deduplication_different_frequencies_both_kept(self):
        dev1 = _device(95.5, 33.85, -84.388, callsign="KSAME", eirp_dbm=60.0)
        dev2 = _device(101.1, 33.85, -84.388, callsign="KSAME", eirp_dbm=60.0)
        result = process_and_rank([_system([dev1, dev2])], _USER_LAT, _USER_LON)
        assert len(result) == 2

    # ── Limit parameter ──────────────────────────────────────────────────────

    def test_limit_restricts_output_count(self):
        devices = [
            _device(95.5, 33.85, -84.388, callsign="K001"),
            _device(97.1, 33.85, -84.388, callsign="K002"),
            _device(99.3, 33.85, -84.388, callsign="K003"),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=2)
        assert len(result) == 2

    def test_limit_zero_uses_default(self):
        # Create DEFAULT_LIMIT + 1 devices so limit=0 (→ DEFAULT_LIMIT) actually caps output.
        # Devices use slightly different latitudes to avoid deduplication.
        devices = [
            _device(95.5 + i * 0.1, _USER_LAT + i * 0.001, _USER_LON, callsign=f"K{i:03d}")
            for i in range(DEFAULT_LIMIT + 1)
        ]
        result_default = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=0)
        assert len(result_default) == DEFAULT_LIMIT, (
            f"limit=0 should fall back to DEFAULT_LIMIT ({DEFAULT_LIMIT})"
        )
        result_one = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=1)
        assert len(result_one) == 1

    # ── frequency_matched flag ───────────────────────────────────────────────

    def test_frequency_matched_exact(self):
        result = process_and_rank(
            [_FM_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[95.5]
        )
        assert result[0]["frequency_matched"] is True

    def test_frequency_matched_within_tolerance(self):
        # 95.5 - 4.9 = 90.6 MHz, still within ±5 MHz
        result = process_and_rank(
            [_FM_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[90.6]
        )
        assert result[0]["frequency_matched"] is True

        # Boundary check: exactly ±FREQUENCY_MATCH_TOLERANCE_MHZ is inclusive (uses <=)
        # Tower at 95.5 MHz, user_freq = 95.5 - 5.0 = 90.5 → diff == 5.0 → matched
        result_boundary = process_and_rank(
            [_FM_SYSTEM], _USER_LAT, _USER_LON,
            user_frequencies=[95.5 - FREQUENCY_MATCH_TOLERANCE_MHZ],
        )
        assert result_boundary[0]["frequency_matched"] is True, (
            "Exact boundary (diff == FREQUENCY_MATCH_TOLERANCE_MHZ) should be matched (<=)"
        )

    def test_frequency_not_matched_outside_tolerance(self):
        # 95.5 - 6 = 89.5, outside ±5 MHz
        result = process_and_rank(
            [_FM_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[89.5]
        )
        assert result[0]["frequency_matched"] is False

    def test_frequency_matched_false_when_no_user_frequencies(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        assert result[0]["frequency_matched"] is False

    # ── Frequency-match sorting ──────────────────────────────────────────────

    def test_frequency_matched_towers_sort_first(self):
        # Two towers at the same distance with the same EIRP: one matches user frequency,
        # one does not. The only sorting difference is frequency_matched, so the matched
        # tower must sort first regardless of other attributes.
        matched_device = _device(95.5, 33.85, -84.388, callsign="KMATCH", eirp_dbm=60.0)
        unmatched_device = _device(97.1, 33.85, -84.388, callsign="KOTHER", eirp_dbm=60.0)
        result = process_and_rank(
            [_system([matched_device, unmatched_device])],
            _USER_LAT,
            _USER_LON,
            user_frequencies=[95.5],
        )
        assert result[0]["callsign"] == "KMATCH"
        assert result[0]["frequency_matched"] is True

    # ── Default EIRP fallback ────────────────────────────────────────────────

    def test_fm_default_eirp_used_when_missing(self):
        device_no_eirp = {
            "frequency": 95.5,
            "callsign": "KNOEIRP",
            "location": {"geom": "POINT(-84.388 33.85)"},
            # no eirp_dbm, no eirp, no transmitPower
        }
        result = process_and_rank([_system([device_no_eirp])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        # FM default is 50.0 dBm
        assert result[0]["eirp_dbm"] == 50.0

    def test_vhf_default_eirp_used_when_missing(self):
        device_no_eirp = {
            "frequency": 180.0,  # VHF band
            "callsign": "KVHF",
            "location": {"geom": "POINT(-84.388 33.85)"},
        }
        result = process_and_rank([_system([device_no_eirp])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        # non-FM default is 60.0 dBm
        assert result[0]["eirp_dbm"] == 60.0

    # ── Rank assignment ──────────────────────────────────────────────────────

    def test_rank_is_one_based(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        assert result[0]["rank"] == 1

    def test_rank_sequence_correct(self):
        devices = [
            _device(95.5, 33.85, -84.388, callsign="K001"),
            _device(97.1, 33.86, -84.388, callsign="K002"),
            _device(99.3, 33.87, -84.388, callsign="K003"),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON)
        ranks = [t["rank"] for t in result]
        assert ranks == list(range(1, len(result) + 1))

    def test_limit_one_returns_only_best_tower(self):
        # With limit=1, only the single highest-ranked tower is returned.
        devices = [
            _device(95.5, 33.85, -84.388, callsign="K001"),
            _device(97.1, 33.86, -84.388, callsign="K002"),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=1)
        assert len(result) == 1
        assert result[0]["rank"] == 1

    # ── Sensitivity filter ───────────────────────────────────────────────────

    def test_tower_below_sensitivity_excluded(self):
        # Mock received_power to return SENSITIVITY_DBM - 1 so the sensitivity filter
        # is exercised regardless of the actual path-loss calculation.
        from unittest.mock import patch

        import services.tower_ranking as _tr

        near_device = _device(freq_mhz=95.5, lat=33.85, lon=_USER_LON, callsign="KWEAK")
        below_sensitivity = SENSITIVITY_DBM - 1.0

        with patch.object(_tr, "received_power", return_value=below_sensitivity):
            result = process_and_rank([_system([near_device])], _USER_LAT, _USER_LON)

        assert result == [], (
            f"Tower whose received power ({below_sensitivity} dBm) is below "
            f"SENSITIVITY_DBM ({SENSITIVITY_DBM} dBm) should be excluded"
        )

    # ── Output field completeness ────────────────────────────────────────────

    def test_output_contains_all_expected_fields(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        t = result[0]
        expected_fields = {
            "callsign", "name", "state", "frequency_mhz", "band",
            "latitude", "longitude", "antenna_height_m", "distance_km",
            "bearing_deg", "bearing_cardinal", "received_power_dbm",
            "distance_class", "eirp_dbm", "licence_type", "licence_subtype",
            "frequency_matched", "rank",
        }
        assert expected_fields.issubset(t.keys())

    # ── Multiple systems ─────────────────────────────────────────────────────

    def test_devices_from_multiple_systems_aggregated(self):
        system1 = _system([_device(95.5, 33.85, -84.388, callsign="K001")])
        system2 = _system([_device(97.1, 33.86, -84.388, callsign="K002")])
        result = process_and_rank([system1, system2], _USER_LAT, _USER_LON)
        callsigns = {t["callsign"] for t in result}
        assert "K001" in callsigns
        assert "K002" in callsigns
