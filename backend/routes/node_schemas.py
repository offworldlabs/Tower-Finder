"""Request and response models for the v1 node API.

The wire contract is `nodes_api_v1.yml` at the workspace root and it is frozen:
the node client and the conformance harness are independent implementations of
the same file, so a bound that looks wrong is raised there rather than adjusted
here. Every bound below is transcribed from it.

Request models forbid unknown keys, because the spec sets
`additionalProperties: false` on all of them. Response models do not, since this
end emits them.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
)


def _reject_bool(value: Any) -> Any:
    """`bool` subclasses `int`, so Pydantic accepts `true` for a numeric field.

    JSON `true` is not a `number`, and a frame carrying `"snr": [true]` would
    otherwise be filed as 1.0 rather than refused.
    """
    if isinstance(value, bool):
        raise ValueError("expected a number, not a boolean")
    return value


def _rfc3339_z(value: datetime) -> str:
    """UTC with a `Z`, which is the form every example in the spec uses.

    Pydantic's default renders `+00:00`. Both are valid RFC 3339, but two other
    implementations read this field and one of them asserts on the suffix.
    """
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


Number = Annotated[float, BeforeValidator(_reject_bool)]
Count = Annotated[int, BeforeValidator(_reject_bool)]
ServerTime = Annotated[datetime, PlainSerializer(_rfc3339_z, return_type=str)]
NodeId = Annotated[str, Field(pattern=r"^ret[0-9a-f]{8}$")]
NodeRef = Annotated[str, Field(pattern=r"^(nde|sim)[0-9a-z]{12}$")]
BootId = Annotated[str, Field(pattern=r"^[0-9a-z]{8,32}$")]
ConfigVersion = Annotated[int, BeforeValidator(_reject_bool), Field(ge=1)]


class _RequestModel(BaseModel):
    """Base for every request model: an unexpected key is a 422, not a silent drop."""

    model_config = ConfigDict(extra="forbid")


class AcceptanceRecord(_RequestModel):
    """One versioned thing the owner accepted, and when."""

    version: str = Field(max_length=32)
    # Aware rather than plain: `format: date-time` carries an offset, and a naive
    # value would quietly mean whatever timezone the server happens to run in.
    accepted_at: AwareDatetime


class PublicationChoice(_RequestModel):
    """Whether the owner chose to publish this node's detections.

    `choice` is required and carries no model default. The spec's `default: public`
    describes what the onboarding flow preselects; a default here would accept a
    body the spec rejects.
    """

    version: str = Field(max_length=32)
    accepted_at: AwareDatetime
    choice: Literal["public", "private"]


class Agreements(_RequestModel):
    """Three separately versioned records, because they are withdrawn separately."""

    licence: AcceptanceRecord
    remote_management: AcceptanceRecord
    publication: PublicationChoice


class RegisterRequest(_RequestModel):
    node_id: NodeId
    board_model: str = Field(max_length=64)
    agreements: Agreements
    # Deliberately untyped. A Pydantic model here would 422 on a bad value before
    # the handler runs, putting a config-shaped rejection in front of identity
    # resolution and making the response an oracle for which identities exist.
    # Validation is services/node_config.validate_config, called from inside the
    # handler once the identity has resolved.
    config: dict[str, Any]


class RegisterResponse(BaseModel):
    token: str = Field(min_length=32, max_length=128)
    node_ref: NodeRef
    config_version: ConfigVersion
    server_time: ServerTime
