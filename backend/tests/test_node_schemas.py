"""Model-level tests for the v1 node wire models.

Deliberately fixture-free: these models import nothing from the backend, so the
tests need no client, no database and no conftest entry.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from routes.node_schemas import (
    AcceptanceRecord,
    Agreements,
    ConfigResponse,
    DetectionAck,
    DetectionFrame,
    ErrorBody,
    HeartbeatRequest,
    HeartbeatResponse,
    NodeHealth,
    PublicationChoice,
    RegisterRequest,
    RegisterResponse,
)

AGREEMENTS = {
    "licence": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "remote_management": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "publication": {
        "version": "2026-07-01",
        "accepted_at": "2026-07-31T09:12:00Z",
        "choice": "public",
    },
}

VALID_CONFIG = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570000000,
    "fs_hz": 2000000,
    "beam_width_deg": 60,
    "beam_azimuth_deg": None,
    "max_range_km": 150,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}


def _registration(**overrides):
    return {
        "node_id": "ret1a2b3c4d",
        "board_model": "pi5-v3-arm64",
        "agreements": AGREEMENTS,
        "config": VALID_CONFIG,
    } | overrides


def test_the_documented_registration_body_round_trips():
    request = RegisterRequest(**_registration())
    assert request.node_id == "ret1a2b3c4d"
    assert request.agreements.publication.choice == "public"
    assert request.agreements.licence.accepted_at == datetime(2026, 7, 31, 9, 12, tzinfo=UTC)
    assert request.config == VALID_CONFIG


def test_an_out_of_range_rx_lat_is_left_for_the_validator():
    """The config is untyped so that a 400 cannot precede identity resolution."""
    request = RegisterRequest(**_registration(config=dict(VALID_CONFIG, rx_lat=999)))
    assert request.config["rx_lat"] == 999


def test_an_unrecognised_config_key_is_also_left_for_the_validator():
    request = RegisterRequest(**_registration(config=dict(VALID_CONFIG, nonsense=1)))
    assert request.config["nonsense"] == 1


def test_an_unknown_top_level_key_is_rejected():
    with pytest.raises(ValidationError):
        RegisterRequest(**_registration(node_ref="nde4f2k9xq7m3b8"))


@pytest.mark.parametrize("node_id", ["ret1A2B3C4D", "ret1a2b3c4", "nde4f2k9xq7m3b8", ""])
def test_a_malformed_node_id_is_rejected(node_id):
    with pytest.raises(ValidationError):
        RegisterRequest(**_registration(node_id=node_id))


def test_a_board_model_over_64_characters_is_rejected():
    with pytest.raises(ValidationError):
        RegisterRequest(**_registration(board_model="x" * 65))


def test_an_acceptance_version_over_32_characters_is_rejected():
    with pytest.raises(ValidationError):
        AcceptanceRecord(version="x" * 33, accepted_at="2026-07-31T09:12:00Z")


def test_agreements_missing_publication_are_rejected():
    agreements = {k: v for k, v in AGREEMENTS.items() if k != "publication"}
    with pytest.raises(ValidationError):
        RegisterRequest(**_registration(agreements=agreements))


def test_a_private_choice_is_carried():
    agreements = AGREEMENTS | {"publication": AGREEMENTS["publication"] | {"choice": "private"}}
    request = RegisterRequest(**_registration(agreements=agreements))
    assert request.agreements.publication.choice == "private"


def test_an_omitted_choice_is_rejected_rather_than_defaulted():
    """`required` governs the wire; `default: public` describes the onboarding flow."""
    publication = {k: v for k, v in AGREEMENTS["publication"].items() if k != "choice"}
    with pytest.raises(ValidationError):
        Agreements(**(AGREEMENTS | {"publication": publication}))


def test_a_third_choice_is_rejected():
    with pytest.raises(ValidationError):
        PublicationChoice(version="2026-07-01", accepted_at="2026-07-31T09:12:00Z", choice="both")


def test_an_acceptance_timestamp_without_an_offset_is_rejected():
    with pytest.raises(ValidationError):
        AcceptanceRecord(version="2026-07-01", accepted_at="2026-07-31T09:12:00")


def test_the_registration_response_round_trips_with_a_z_suffixed_time():
    response = RegisterResponse(
        token="k" * 40,
        node_ref="nde4f2k9xq7m3b8",
        config_version=7,
        server_time=datetime(2026, 7, 31, 9, 12, 1, tzinfo=UTC),
    )
    assert response.model_dump(mode="json") == {
        "token": "k" * 40,
        "node_ref": "nde4f2k9xq7m3b8",
        "config_version": 7,
        "server_time": "2026-07-31T09:12:01Z",
    }


def test_a_naive_server_time_is_rejected():
    """`ServerTime` is `AwareDatetime`, so a handler passing a naive value gets a
    validation error rather than a `server_time` silently shifted by
    `astimezone(UTC)` reading it as local time."""
    with pytest.raises(ValidationError):
        RegisterResponse(
            token="k" * 40,
            node_ref="nde4f2k9xq7m3b8",
            config_version=7,
            server_time=datetime(2026, 7, 31, 9, 12, 1),  # naive on purpose
        )


@pytest.mark.parametrize("token", ["k" * 31, "k" * 129])
def test_a_token_outside_the_documented_length_is_rejected(token):
    with pytest.raises(ValidationError):
        RegisterResponse(
            token=token,
            node_ref="nde4f2k9xq7m3b8",
            config_version=7,
            server_time=datetime.now(UTC),
        )


@pytest.mark.parametrize("node_ref", ["nde4f2k9xq7m3b", "abc4f2k9xq7m3b8", "NDE4F2K9XQ7M3B8"])
def test_a_malformed_node_ref_is_rejected(node_ref):
    with pytest.raises(ValidationError):
        RegisterResponse(token="k" * 40, node_ref=node_ref, config_version=7, server_time=datetime.now(UTC))


def test_a_config_version_below_one_is_rejected():
    with pytest.raises(ValidationError):
        RegisterResponse(
            token="k" * 40,
            node_ref="nde4f2k9xq7m3b8",
            config_version=0,
            server_time=datetime.now(UTC),
        )


FRAME = {
    "t": 1753900000.123,
    "seq": 918273,
    "boot_id": "k3n8v2qp71ab",
    "config_version": 7,
    "delay": [12.4, 30.1],
    "doppler": [-118.0, 44.5],
    "snr": [14.2, 9.8],
    "adsb_hex": ["4ca1f2", None],
}


def test_the_documented_frame_round_trips():
    frame = DetectionFrame(**FRAME)
    assert frame.model_dump(mode="json") == FRAME


def test_an_empty_frame_is_valid():
    """All four arrays empty is a real frame and worth sending."""
    frame = DetectionFrame(**(FRAME | {"delay": [], "doppler": [], "snr": [], "adsb_hex": []}))
    assert frame.delay == []


@pytest.mark.parametrize("field", ["delay", "doppler", "snr", "adsb_hex"])
def test_arrays_of_different_lengths_are_rejected(field):
    short = {"adsb_hex": ["4ca1f2"]} if field == "adsb_hex" else {field: [1.0]}
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | short))


def test_a_frame_carrying_a_node_identifier_is_rejected():
    """The token resolves to a node. A frame that could disagree with its own
    credential would need a rule for what a mismatch means, and every available
    rule is wrong."""
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"node_ref": "nde4f2k9xq7m3b8"}))


@pytest.mark.parametrize("field", ["delay", "doppler", "snr"])
def test_a_boolean_is_not_a_measurement(field):
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {field: [True, 1.0]}))


def test_a_numeric_string_is_not_a_measurement():
    """Pydantic's lax mode coerces a numeric string for a float field, so `"14.2"`
    would otherwise be filed as 14.2 rather than refused."""
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"t": "1753900000.123"}))


def test_a_numeric_string_is_not_a_count():
    """The same coercion applies to an integer field."""
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"seq": "918273"}))


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_value_is_rejected_on_t(value):
    """Starlette parses bodies with the stdlib `json` module, which accepts the
    bare `NaN` and `Infinity` literals, so this is reachable from the wire."""
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"t": value}))


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_value_is_rejected_inside_snr(value):
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"snr": [value, 9.8]}))


def test_the_array_bound_is_512():
    filled = {"delay": [1.0] * 512, "doppler": [1.0] * 512, "snr": [1.0] * 512, "adsb_hex": [None] * 512}
    assert len(DetectionFrame(**(FRAME | filled)).snr) == 512
    over = {key: value + value[:1] for key, value in filled.items()}
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | over))


def test_an_unassociated_detection_is_null_rather_than_absent():
    frame = DetectionFrame(**(FRAME | {"adsb_hex": [None, None]}))
    assert frame.adsb_hex == [None, None]


@pytest.mark.parametrize("value", ["4CA1F2", "4ca1f", "4ca1f22", "zzzzzz"])
def test_a_malformed_icao_address_is_rejected(value):
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"adsb_hex": [value, None]}))


@pytest.mark.parametrize("override", [{"t": -1}, {"seq": -1}, {"config_version": 0}])
def test_a_frame_below_a_documented_bound_is_rejected(override):
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | override))


@pytest.mark.parametrize("boot_id", ["k3n8v2q", "K3N8V2QP71AB", "k" * 33])
def test_a_malformed_boot_id_is_rejected(boot_id):
    with pytest.raises(ValidationError):
        DetectionFrame(**(FRAME | {"boot_id": boot_id}))


@pytest.mark.parametrize("field", ["t", "seq", "boot_id", "config_version", "delay", "doppler", "snr", "adsb_hex"])
def test_every_frame_field_is_required(field):
    with pytest.raises(ValidationError):
        DetectionFrame(**{k: v for k, v in FRAME.items() if k != field})


def test_the_acknowledgement_round_trips():
    ack = DetectionAck(accepted=2, config_stale=False, streaming_allowed=True)
    assert ack.model_dump(mode="json") == {
        "accepted": 2,
        "config_stale": False,
        "streaming_allowed": True,
    }


HEARTBEAT = {
    "state": "streaming",
    "uptime_s": 84213,
    "boot_id": "k3n8v2qp71ab",
    "config_version": 7,
    "health": {"cpu_pct": 31, "disk_free_mb": 9100, "temp_c": 58, "blah2": "up", "adsb": "up"},
    "versions": {"owl_os": "1.4.0", "retina_node": "2.2.1", "blah2_image": "sha-9f21c4e"},
    "errors": [],
}


def test_the_documented_heartbeat_round_trips():
    beat = HeartbeatRequest(**HEARTBEAT)
    assert beat.model_dump(mode="json") == HEARTBEAT


def test_the_minimal_heartbeat_is_state_uptime_boot_id_and_version():
    beat = HeartbeatRequest(state="starting", uptime_s=3, boot_id="k3n8v2qp71ab", config_version=None)
    assert beat.health is None
    assert beat.versions is None
    assert beat.errors == []


def test_a_node_with_no_version_yet_sends_null():
    """Nullable rather than optional, so the heartbeat really is unconditional."""
    assert HeartbeatRequest(**(HEARTBEAT | {"config_version": None})).config_version is None


@pytest.mark.parametrize("field", ["state", "uptime_s", "boot_id", "config_version"])
def test_the_four_required_heartbeat_fields_are_required(field):
    with pytest.raises(ValidationError):
        HeartbeatRequest(**{k: v for k, v in HEARTBEAT.items() if k != field})


@pytest.mark.parametrize("state", ["starting", "streaming", "stalled", "paused", "error", "stopping"])
def test_every_documented_state_is_accepted(state):
    assert HeartbeatRequest(**(HEARTBEAT | {"state": state})).state == state


def test_an_undocumented_state_is_rejected():
    with pytest.raises(ValidationError):
        HeartbeatRequest(**(HEARTBEAT | {"state": "wedged"}))


def test_a_negative_uptime_is_rejected():
    with pytest.raises(ValidationError):
        HeartbeatRequest(**(HEARTBEAT | {"uptime_s": -1}))


def test_a_versions_field_over_64_characters_is_rejected():
    with pytest.raises(ValidationError):
        HeartbeatRequest(**(HEARTBEAT | {"versions": {"owl_os": "x" * 65}}))


def test_health_says_known_to_be_unknown_with_nulls():
    health = NodeHealth(cpu_pct=None, disk_free_mb=None, temp_c=None, blah2=None)
    assert health.cpu_pct is None
    assert health.adsb is None


@pytest.mark.parametrize("field", ["cpu_pct", "disk_free_mb", "temp_c", "blah2"])
def test_an_absent_health_field_is_rejected(field):
    """The four are required and nullable, so absence never carries meaning."""
    full = {"cpu_pct": 31, "disk_free_mb": 9100, "temp_c": 58, "blah2": "up"}
    with pytest.raises(ValidationError):
        NodeHealth(**{k: v for k, v in full.items() if k != field})


@pytest.mark.parametrize(
    "override",
    [{"cpu_pct": 101}, {"cpu_pct": -1}, {"temp_c": 151}, {"temp_c": -51}, {"disk_free_mb": -1}, {"blah2": "sideways"}],
)
def test_health_outside_a_documented_bound_is_rejected(override):
    with pytest.raises(ValidationError):
        NodeHealth(**({"cpu_pct": 31, "disk_free_mb": 9100, "temp_c": 58, "blah2": "up"} | override))


def test_absent_adsb_means_disabled_rather_than_unknown():
    beat = HeartbeatRequest(
        **(HEARTBEAT | {"health": {"cpu_pct": None, "disk_free_mb": None, "temp_c": None, "blah2": "up"}})
    )
    assert beat.health.adsb is None


def test_the_error_list_is_bounded_at_32():
    assert len(HeartbeatRequest(**(HEARTBEAT | {"errors": ["x"] * 32})).errors) == 32
    with pytest.raises(ValidationError):
        HeartbeatRequest(**(HEARTBEAT | {"errors": ["x"] * 33}))


def test_an_over_long_error_string_is_rejected():
    with pytest.raises(ValidationError):
        HeartbeatRequest(**(HEARTBEAT | {"errors": ["x" * 513]}))


def test_an_unknown_heartbeat_key_is_rejected():
    with pytest.raises(ValidationError):
        HeartbeatRequest(**(HEARTBEAT | {"node_ref": "nde4f2k9xq7m3b8"}))


def test_the_heartbeat_response_round_trips_with_a_z_suffixed_time():
    response = HeartbeatResponse(
        server_time=datetime(2026, 7, 31, 9, 12, 1, tzinfo=UTC),
        config_stale=True,
        streaming_allowed=False,
        node_ref="nde4f2k9xq7m3b8",
    )
    assert response.model_dump(mode="json") == {
        "server_time": "2026-07-31T09:12:01Z",
        "config_stale": True,
        "streaming_allowed": False,
        "node_ref": "nde4f2k9xq7m3b8",
    }


def test_a_synthetic_node_ref_is_accepted():
    response = HeartbeatResponse(
        server_time=datetime.now(UTC), config_stale=False, streaming_allowed=True, node_ref="sim4f2k9xq7m3b8"
    )
    assert response.node_ref.startswith("sim")


def test_the_config_response_carries_the_active_version():
    assert ConfigResponse(config_version=7).model_dump(mode="json") == {"config_version": 7}


def test_an_error_body_omits_detail_when_there_is_none():
    """Registration errors carry no detail by design."""
    assert ErrorBody(error="forbidden").model_dump(mode="json", exclude_none=True) == {"error": "forbidden"}


def test_an_error_body_may_name_the_condition():
    body = ErrorBody(error="invalid_config", detail="rx_lat")
    assert body.model_dump(mode="json") == {"error": "invalid_config", "detail": "rx_lat"}


def test_an_error_over_64_characters_is_rejected():
    with pytest.raises(ValidationError):
        ErrorBody(error="x" * 65)


def test_a_detail_over_512_characters_is_rejected():
    with pytest.raises(ValidationError):
        ErrorBody(error="invalid_config", detail="x" * 513)
