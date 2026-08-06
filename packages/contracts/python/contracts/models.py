"""Transport-agnostic Pydantic v2 models shared by Life Context services."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)


def _normalize_utc(value: datetime) -> datetime:
    """Require an aware timestamp and serialize all timestamps as UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


JsonObject: TypeAlias = dict[str, JsonValue]

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000),
]
UtcDateTime = Annotated[datetime, AfterValidator(_normalize_utc)]


class ContractModel(BaseModel):
    """Common behavior for every wire-level contract model."""

    model_config = ConfigDict(
        extra="forbid",
        # Do not normalize strings nested inside JsonValue payloads.  Source
        # adapters (especially raw clipboard capture) must preserve bytes,
        # whitespace, and line endings exactly; the Identifier/Text aliases
        # below still strip and validate their own contract fields.
        str_strip_whitespace=False,
        validate_assignment=True,
    )

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-compatible representation of this model."""

        return self.model_dump(mode="json")

    def to_json(self, **kwargs: object) -> str:
        """Serialize this model as JSON using Pydantic's canonical encoder."""

        return self.model_dump_json(**kwargs)

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "ContractModel":
        """Deserialize JSON into the concrete contract class."""

        return cls.model_validate_json(value)


class ArtifactKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    ARCHIVE = "archive"
    STRUCTURED = "structured"
    OTHER = "other"


class SourceKind(str, Enum):
    FILE = "file"
    EMAIL = "email"
    CALENDAR = "calendar"
    MESSAGE = "message"
    NOTE = "note"
    WEB = "web"
    APPLICATION = "application"
    SENSOR = "sensor"
    MANUAL = "manual"
    OTHER = "other"


class SourceEventType(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    OBSERVED = "observed"


class ObservationKind(str, Enum):
    FACT = "fact"
    MEASUREMENT = "measurement"
    CLASSIFICATION = "classification"
    RELATION = "relation"
    SUMMARY = "summary"


class ObservationStatus(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    RETRACTED = "retracted"


class QuerySortOrder(str, Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ArtifactRef(ContractModel):
    """A stable reference to captured content without embedding the content."""

    artifact_id: Identifier = Field(
        validation_alias=AliasChoices("artifact_id", "id"),
    )
    kind: ArtifactKind
    uri: Identifier | None = None
    name: Text | None = None
    media_type: Identifier | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized


class SourceEvent(ContractModel):
    """One source observation with source-neutral JSON payload and artifacts."""

    event_id: Identifier = Field(validation_alias=AliasChoices("event_id", "id"))
    source_id: Identifier
    source_kind: SourceKind = Field(
        validation_alias=AliasChoices("source_kind", "source_type"),
    )
    event_type: SourceEventType = Field(
        validation_alias=AliasChoices("event_type", "type"),
    )
    occurred_at: UtcDateTime
    captured_at: UtcDateTime | None = None
    payload: JsonValue = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)


class CaptureEnvelope(ContractModel):
    """A batch of source events captured at one point in time."""

    envelope_id: Identifier = Field(
        validation_alias=AliasChoices("envelope_id", "capture_id", "id"),
    )
    schema_version: Literal["1"] = "1"
    captured_at: UtcDateTime
    source_kind: SourceKind = Field(
        validation_alias=AliasChoices("source_kind", "source_type"),
    )
    events: list[SourceEvent] = Field(min_length=1)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    producer: Identifier | None = None
    metadata: JsonObject = Field(default_factory=dict)


class ObservationV1(ContractModel):
    """A normalized, provenance-linked observation emitted by processing."""

    schema_version: Literal["1"] = "1"
    observation_id: Identifier = Field(
        validation_alias=AliasChoices("observation_id", "id"),
    )
    subject: Identifier
    predicate: Identifier
    value: JsonValue
    kind: ObservationKind = Field(
        default=ObservationKind.FACT,
        validation_alias=AliasChoices("kind", "observation_kind", "observation_type"),
    )
    status: ObservationStatus = ObservationStatus.CANDIDATE
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    observed_at: UtcDateTime
    source_event_id: Identifier = Field(
        validation_alias=AliasChoices("source_event_id", "event_id"),
    )
    evidence: list[ArtifactRef] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)


class QueryRequest(ContractModel):
    """A structured query over observations, independent of query transport."""

    query: Text | None = Field(
        default=None,
        validation_alias=AliasChoices("query", "text"),
    )
    subject: Identifier | None = None
    predicate: Identifier | None = None
    source_ids: list[Identifier] = Field(default_factory=list)
    kinds: list[ObservationKind] = Field(
        default_factory=list,
        validation_alias=AliasChoices("kinds", "observation_kinds"),
    )
    statuses: list[ObservationStatus] = Field(default_factory=list)
    observed_after: UtcDateTime | None = None
    observed_before: UtcDateTime | None = None
    sort_order: QuerySortOrder = QuerySortOrder.DESCENDING
    limit: int = Field(default=100, ge=1, le=1_000)
    cursor: Identifier | None = None
    include_evidence: bool = False

    @model_validator(mode="after")
    def validate_time_window(self) -> "QueryRequest":
        if (
            self.observed_after is not None
            and self.observed_before is not None
            and self.observed_after >= self.observed_before
        ):
            raise ValueError("observed_after must be earlier than observed_before")
        return self


class QueryResponse(ContractModel):
    """A page of observations and its pagination metadata."""

    request_id: Identifier | None = None
    observations: list[ObservationV1] = Field(
        default_factory=list,
        validation_alias=AliasChoices("observations", "items", "results"),
    )
    total_count: int = Field(default=0, ge=0)
    next_cursor: Identifier | None = None

    @model_validator(mode="after")
    def validate_total_count(self) -> "QueryResponse":
        if self.total_count < len(self.observations):
            raise ValueError("total_count cannot be smaller than the returned observations")
        return self


class BacklogStatus(ContractModel):
    """Counts and timing for work waiting to be processed."""

    pending_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    oldest_pending_at: UtcDateTime | None = None
    updated_at: UtcDateTime


class HealthSnapshot(ContractModel):
    """A point-in-time service health report with optional component detail."""

    service: Identifier
    status: HealthState
    checked_at: UtcDateTime
    version: Text | None = None
    components: dict[str, HealthState] = Field(default_factory=dict)
    backlog: BacklogStatus | None = None
    message: Text | None = None
    details: JsonObject = Field(default_factory=dict)


# Friendly names for callers that use the shorter vocabulary.  They are aliases
# rather than duplicate enums, so the JSON schema has one canonical definition.
SourceType = SourceKind
EventType = SourceEventType
ObservationType = ObservationKind
HealthStatus = HealthState


__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "BacklogStatus",
    "CaptureEnvelope",
    "ContractModel",
    "EventType",
    "HealthSnapshot",
    "HealthState",
    "HealthStatus",
    "JsonObject",
    "JsonValue",
    "ObservationKind",
    "ObservationStatus",
    "ObservationType",
    "ObservationV1",
    "QueryRequest",
    "QueryResponse",
    "QuerySortOrder",
    "SourceEvent",
    "SourceEventType",
    "SourceKind",
    "SourceType",
]
