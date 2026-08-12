import math

import pytest

from services.node_config import ConfigInvalid, validate_config

VALID = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900,
    "tx_callsign": "CRYSTAL_PALACE",
    "fc_hz": 5.7e8,
    "fs_hz": 2.0e6,
    "beam_width_deg": 60,
    "beam_azimuth_deg": None,
    "max_range_km": 150,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

NUMERIC_FIELDS = [
    "rx_lat",
    "rx_lon",
    "rx_alt_ft",
    "tx_lat",
    "tx_lon",
    "tx_alt_ft",
    "fc_hz",
    "fs_hz",
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
    "cpi_s",
    "delay_tolerance_us",
    "doppler_tolerance_hz",
]


def test_a_valid_config_passes_through_unchanged():
    assert validate_config(dict(VALID)) == VALID


def test_null_beam_azimuth_is_preserved_not_defaulted():
    assert validate_config(dict(VALID))["beam_azimuth_deg"] is None


def test_zero_beam_azimuth_is_not_null():
    payload = dict(VALID, beam_azimuth_deg=0.0)
    assert validate_config(payload)["beam_azimuth_deg"] == 0.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("rx_lat", 91),
        ("rx_lat", -91),
        ("rx_lon", 181),
        ("rx_lon", -181),
        ("rx_alt_ft", -1501),
        ("rx_alt_ft", 30001),
        ("tx_lat", 91),
        ("tx_lat", -91),
        ("tx_lon", 181),
        ("tx_lon", -181),
        ("tx_alt_ft", -1501),
        ("tx_alt_ft", 30001),
        ("fc_hz", 999_999),
        ("fc_hz", 6_000_000_001),
        ("fs_hz", 99_999),
        ("fs_hz", 20_000_001),
        ("beam_width_deg", 0),
        ("beam_width_deg", 361),
        ("beam_azimuth_deg", 360),
        ("beam_azimuth_deg", -1),
        ("max_range_km", 0),
        ("max_range_km", 1001),
        ("cpi_s", 0),
        ("cpi_s", -1),
        ("cpi_s", 10.001),
        ("delay_tolerance_us", 0),
        ("delay_tolerance_us", -1),
        ("doppler_tolerance_hz", 0),
        ("doppler_tolerance_hz", -1),
        ("tx_callsign", ""),
        ("tx_callsign", "x" * 33),
    ],
)
def test_out_of_range_values_are_rejected_naming_the_field(field, value):
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, **{field: value}))
    assert excinfo.value.field == field


def test_a_missing_field_is_rejected():
    payload = dict(VALID)
    del payload["tx_callsign"]
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(payload)
    assert excinfo.value.field == "tx_callsign"


def test_an_unknown_field_is_rejected():
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, gain_db=12))
    assert excinfo.value.field == "gain_db"


def test_rx_and_tx_at_the_same_point_is_rejected():
    payload = dict(VALID, tx_lat=VALID["rx_lat"], tx_lon=VALID["rx_lon"])
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(payload)
    assert excinfo.value.field == "tx_lat"


def test_a_boolean_is_not_accepted_as_a_number():
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, rx_lat=True))
    assert excinfo.value.field == "rx_lat"


# --- Further cases, beyond the original set ----------------------------------------


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize("value", [True, False])
def test_no_numeric_field_accepts_a_boolean(field, value):
    """bool is a subclass of int, so False would otherwise pass as a valid 0 azimuth."""
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, **{field: value}))
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
def test_nan_is_rejected(field):
    """Every comparison against NaN is false, so a bounds check alone lets it through."""
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, **{field: math.nan}))
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
@pytest.mark.parametrize("value", [math.inf, -math.inf])
def test_infinity_is_rejected(field, value):
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, **{field: value}))
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", NUMERIC_FIELDS)
def test_an_integer_too_large_for_a_float_is_rejected(field):
    """JSON caps no integer literal, and float() on one of these raises OverflowError."""
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, **{field: 10**400}))
    assert excinfo.value.field == field


@pytest.mark.parametrize("value", ["51.42", None, [], {}])
def test_a_non_numeric_type_is_rejected(value):
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, rx_lat=value))
    assert excinfo.value.field == "rx_lat"


@pytest.mark.parametrize("value", [123, None, ["CRYSTAL_PALACE"]])
def test_a_non_string_callsign_is_rejected(value):
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, tx_callsign=value))
    assert excinfo.value.field == "tx_callsign"


@pytest.mark.parametrize(
    "field,value",
    [
        ("rx_lat", 90),
        ("rx_lat", -90),
        ("rx_lon", 180),
        ("rx_lon", -180),
        ("rx_alt_ft", -1500),
        ("rx_alt_ft", 30000),
        ("tx_lat", 90),
        ("tx_lat", -90),
        ("tx_lon", 180),
        ("tx_lon", -180),
        ("tx_alt_ft", -1500),
        ("tx_alt_ft", 30000),
        ("fc_hz", 1_000_000),
        ("fc_hz", 6_000_000_000),
        ("fs_hz", 100_000),
        ("fs_hz", 20_000_000),
        ("beam_width_deg", 360),
        ("beam_azimuth_deg", 0),
        ("beam_azimuth_deg", 359.999),
        ("max_range_km", 1000),
        ("cpi_s", 10),
    ],
)
def test_inclusive_bounds_are_accepted(field, value):
    """The contract's minimums and maximums are legal values, not the first illegal one."""
    assert validate_config(dict(VALID, **{field: value}))[field] == value


@pytest.mark.parametrize(
    "field",
    [
        "beam_width_deg",
        "max_range_km",
        "cpi_s",
        "delay_tolerance_us",
        "doppler_tolerance_hz",
    ],
)
def test_exclusive_minimums_reject_a_negative_as_well_as_zero(field):
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, **{field: -1}))
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", ["delay_tolerance_us", "doppler_tolerance_hz"])
@pytest.mark.parametrize("value", [1e3, 1e6, 1e300])
def test_the_tolerances_have_no_upper_bound(field, value):
    """The contract sets no maximum on either, so a ceiling of our own would reject
    a conformant node. Pinned because inventing one is the tempting mistake."""
    assert validate_config(dict(VALID, **{field: value}))[field] == value


def test_every_missing_field_is_reported_by_name():
    for field in VALID:
        payload = {k: v for k, v in VALID.items() if k != field}
        with pytest.raises(ConfigInvalid) as excinfo:
            validate_config(payload)
        assert excinfo.value.field == field


def test_an_empty_payload_is_rejected_rather_than_defaulted():
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config({})
    assert excinfo.value.reason == "missing"


def test_a_baseline_shorter_than_the_degenerate_threshold_is_rejected():
    """Distinct coordinates are still one point once they are under a millimetre apart."""
    payload = dict(VALID, tx_lat=VALID["rx_lat"] + 1e-9, tx_lon=VALID["rx_lon"] - 1e-9)
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(payload)
    assert excinfo.value.field == "tx_lat"


@pytest.mark.parametrize(
    "override",
    [
        {"tx_lat": VALID["rx_lat"]},  # same latitude, different longitude
        {"tx_lon": VALID["rx_lon"]},  # same longitude, different latitude
    ],
)
def test_sharing_one_coordinate_is_not_a_degenerate_baseline(override):
    assert validate_config(dict(VALID, **override))


def test_altitude_alone_does_not_separate_a_coincident_pair():
    """The solver's baseline is horizontal, so a tall mast at the receiver is still degenerate."""
    payload = dict(VALID, tx_lat=VALID["rx_lat"], tx_lon=VALID["rx_lon"], tx_alt_ft=5000)
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(payload)
    assert excinfo.value.field == "tx_lat"


def test_the_payload_is_not_mutated():
    payload = dict(VALID)
    validate_config(payload)
    assert payload == VALID


def test_the_returned_dict_is_not_the_payload():
    payload = dict(VALID)
    assert validate_config(payload) is not payload


def test_config_invalid_carries_a_field_and_a_reason():
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(dict(VALID, rx_lat=91))
    assert excinfo.value.field == "rx_lat"
    assert isinstance(excinfo.value.reason, str)
    assert excinfo.value.reason


def test_an_unknown_field_is_reported_before_a_missing_one():
    """A node sending a renamed field should be told the name it invented, not the one it dropped."""
    payload = {k: v for k, v in VALID.items() if k != "beam_azimuth_deg"}
    payload["beam_azimuth"] = 90
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(payload)
    assert excinfo.value.field == "beam_azimuth"


def test_integers_are_normalised_to_floats():
    """The store and the solver both want floats, so normalisation happens once, here."""
    out = validate_config(dict(VALID, rx_alt_ft=120, beam_azimuth_deg=90))
    assert isinstance(out["rx_alt_ft"], float)
    assert isinstance(out["beam_azimuth_deg"], float)
    assert isinstance(out["tx_callsign"], str)


def test_the_output_carries_exactly_the_contract_fields():
    assert set(validate_config(dict(VALID))) == set(VALID)


@pytest.mark.parametrize(
    "payload",
    [
        None,  # set(None) is a TypeError
        5,  # so is set(5)
        "rx_lat",  # iterable, but of characters
        [],
        [{}],  # unhashable, so set() raises rather than building
        [1, "a"],  # hashable but not mutually orderable, so sorted() raises
    ],
)
def test_a_payload_that_is_not_an_object_is_rejected_not_raised_through(payload):
    """A JSON body can be any type, and the route has only ConfigInvalid to map to 400."""
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config(payload)
    assert excinfo.value.field == "config"


def test_the_field_named_is_always_a_string():
    """The route interpolates .field into the error detail, so an int would be a lie."""
    with pytest.raises(ConfigInvalid) as excinfo:
        validate_config([1, 2])
    assert isinstance(excinfo.value.field, str)
