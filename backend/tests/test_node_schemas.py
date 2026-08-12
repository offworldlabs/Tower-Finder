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
    DetectionAck,
    DetectionFrame,
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
