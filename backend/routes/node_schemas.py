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
    model_serializer,
    model_validator,
)


def _reject_non_number(value: Any) -> Any:
    """Three shapes that are not a JSON `number` but that Pydantic's lax mode
    would otherwise accept for one.

    `bool` subclasses `int`, so `true` would be filed as 1.0. A numeric string
    coerces, so `"14.2"` would be filed as 14.2. Both would leave a frame
    carrying `"snr": [true]` or `"config_version": "7"` silently accepted
    instead of refused.
    """
    if isinstance(value, bool | str):
        raise ValueError("expected a number, not a boolean or a string")
    return value


def _rfc3339_z(value: datetime) -> str:
    """UTC with a `Z`, which is the form every example in the spec uses.

    Pydantic's default renders `+00:00`. Both are valid RFC 3339, but two other
    implementations read this field and one of them asserts on the suffix.
    """
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# `allow_inf_nan=False` closes the third hole `_reject_non_number` cannot: Starlette
# parses bodies with the stdlib `json` module, which accepts the bare `NaN` and
# `Infinity` literals, and a non-finite delay or SNR would otherwise reach the
# solver. Confirmed to survive the merge with a field's own `Field(ge=...)`.
Number = Annotated[float, BeforeValidator(_reject_non_number), Field(allow_inf_nan=False)]
# Ints cannot be non-finite, so only the shared string/bool guard applies here.
Count = Annotated[int, BeforeValidator(_reject_non_number)]
# AwareDatetime rather than datetime: `_rfc3339_z` calls `astimezone(UTC)`, which
# reads a naive value as local time, so a handler passing `datetime.utcnow()` would
# silently emit a `server_time` an hour wrong under BST instead of raising. This is
# the one field a node uses to detect clock skew, so a silent shift is the worst
# failure mode available.
ServerTime = Annotated[AwareDatetime, PlainSerializer(_rfc3339_z, return_type=str)]
NodeId = Annotated[str, Field(pattern=r"^ret[0-9a-f]{8}$")]
NodeRef = Annotated[str, Field(pattern=r"^(nde|sim)[0-9a-z]{12}$")]
BootId = Annotated[str, Field(pattern=r"^[0-9a-z]{8,32}$")]
ConfigVersion = Annotated[int, BeforeValidator(_reject_non_number), Field(ge=1)]

# A body guard rather than a statement about how many detections a CPI produces,
# which is single figures in practice.
MAX_DETECTIONS = 512

AdsbHex = Annotated[str, Field(pattern=r"^[0-9a-f]{6}$")] | None

# Six values as of contract 1.1.0. `stalled` is a healthy node whose radar has
# stopped, which the server cannot tell from a network fault on its own.
NodeState = Literal["starting", "streaming", "stalled", "paused", "error", "stopping"]
ServiceState = Literal["up", "down", "unknown"]


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


class DetectionFrame(_RequestModel):
    """One CPI's worth of detections.

    The frame carries no node identifier: the token resolves to a node and the
    frame is stamped server side, so `extra="forbid"` on the base class is load
    bearing rather than tidiness.
    """

    # Unix epoch seconds, node clock, the end of the capture window. The samples
    # behind a frame span [t - cpi_s, t], and cpi_s lives in the node's
    # configuration rather than on the hot path.
    t: Number = Field(ge=0)
    # Restart-local, so it is only interpretable alongside boot_id.
    seq: Count = Field(ge=0)
    boot_id: BootId
    config_version: ConfigVersion
    delay: list[Number] = Field(max_length=MAX_DETECTIONS)
    doppler: list[Number] = Field(max_length=MAX_DETECTIONS)
    snr: list[Number] = Field(max_length=MAX_DETECTIONS)
    adsb_hex: list[AdsbHex] = Field(max_length=MAX_DETECTIONS)

    @model_validator(mode="after")
    def _arrays_are_parallel(self) -> "DetectionFrame":
        """The four arrays are one table on its side, so a mismatch is a 422."""
        if len({len(self.delay), len(self.doppler), len(self.snr), len(self.adsb_hex)}) > 1:
            raise ValueError("delay, doppler, snr and adsb_hex must be the same length")
        return self


class DetectionAck(BaseModel):
    # v1 accepts a frame whole or not at all, so this always equals the array
    # length. The field exists so a later plausibility gate can accept fewer
    # without a new response shape.
    accepted: Count = Field(ge=0)
    config_stale: bool
    streaming_allowed: bool


class NodeHealth(_RequestModel):
    """Diagnostic only. The server decides whether a node is working from its own
    record of frame arrivals, not from this.

    The four values a node can always attempt to read are required and nullable,
    so a value it could not obtain arrives as an explicit null rather than as an
    absent key. `cpu_pct` is the motivating case: /proc/stat is cumulative, so
    the first beat after a start has no percentage to report.
    """

    cpu_pct: Number | None = Field(ge=0, le=100)
    disk_free_mb: Count | None = Field(ge=0)
    temp_c: Number | None = Field(ge=-50, le=150)
    blah2: ServiceState | None
    # Omitted entirely when ADS-B is disabled in node configuration, so absence
    # means disabled rather than unknown. An explicit null reads the same way.
    adsb: ServiceState | None = None


class NodeVersions(_RequestModel):
    owl_os: Annotated[str, Field(max_length=64)] | None = None
    retina_node: Annotated[str, Field(max_length=64)] | None = None
    blah2_image: Annotated[str, Field(max_length=64)] | None = None


class HeartbeatRequest(_RequestModel):
    state: NodeState
    uptime_s: Count = Field(ge=0)
    boot_id: BootId
    # Required and nullable. Only the server issues a version, so there is a
    # window at every start where the node genuinely holds none, and a node that
    # cannot build a configuration at all is the one most worth hearing from.
    config_version: ConfigVersion | None
    health: NodeHealth | None = None
    versions: NodeVersions | None = None
    # Accumulated since the last beat rather than a single slot, so transient
    # faults between beats are not lost. Anything beyond the bound is dropped
    # node side rather than truncating the request.
    errors: list[Annotated[str, Field(max_length=512)]] = Field(default_factory=list, max_length=32)


class HeartbeatResponse(BaseModel):
    server_time: ServerTime
    config_stale: bool
    streaming_allowed: bool
    # The only place the node learns its public identifier has rotated.
    node_ref: NodeRef


class ConfigResponse(BaseModel):
    config_version: ConfigVersion


class ErrorBody(BaseModel):
    """The spec's `Error`. Registration errors carry no detail by design.

    The contract types `detail` as a string with no null member, so the key is
    dropped rather than serialised as `"detail": null`. Doing it here rather
    than asking every call site for `exclude_none=True` means a handler cannot
    emit a body the frozen schema rejects by forgetting the flag.
    """

    error: str = Field(max_length=64)
    detail: Annotated[str, Field(max_length=512)] | None = None

    @model_serializer
    def _omit_absent_detail(self) -> dict[str, str]:
        body = {"error": self.error}
        if self.detail is not None:
            body["detail"] = self.detail
        return body
