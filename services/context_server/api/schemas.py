"""Validated FastAPI boundary schemas for the public context-server API.

The runtime predates the transport-neutral models in ``packages/contracts``
and still emits a few richer compatibility payloads.  The public API keeps
those payloads available through explicit, strict legacy models while putting
the shared contract models on the same request/response schemas that FastAPI
publishes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

try:
    from contracts import (
        ArtifactRef,
        CaptureEnvelope,
        HealthSnapshot,
        JsonObject,
        JsonValue,
        ObservationV1,
        QueryRequest,
        QueryResponse,
    )
except ModuleNotFoundError:  # Running from a source checkout without an install.
    from packages.contracts.python.contracts import (
        ArtifactRef,
        CaptureEnvelope,
        HealthSnapshot,
        JsonObject,
        JsonValue,
        ObservationV1,
        QueryRequest,
        QueryResponse,
    )


class ApiModel(BaseModel):
    """Strict configuration shared by compatibility-only API models."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
    )


class QueryRequestCompatibility(QueryRequest):
    """Legacy query controls retained alongside the shared query contract."""

    mode: Literal["hybrid", "fts", "semantic"] = "hybrid"
    filters: dict[str, JsonValue] = Field(default_factory=dict)
    start: datetime | str | None = None
    end: datetime | str | None = None
    fts_weight: float = Field(default=0.5, ge=0.0)
    semantic_weight: float = Field(default=0.5, ge=0.0)
    evidence_limit: int | None = Field(default=None, ge=1)
    explain: bool = False

    @model_validator(mode="after")
    def validate_weights(self) -> QueryRequestCompatibility:
        if self.fts_weight == 0 and self.semantic_weight == 0:
            raise ValueError("at least one query weight must be positive")
        return self


class IngestBatchCompatibility(ApiModel):
    """Validated shape for the original collector ``records`` transport."""

    records: list[dict[str, JsonValue]] = Field(min_length=1)


class ObservationCompatibility(ApiModel):
    """Validated legacy observation input accepted by the local clients."""

    id: str | None = None
    event_id: str | None = None
    source_event_id: str | None = None
    source_id: str | None = None
    text: str | None = None
    content: str | None = None
    payload: JsonValue = None
    value: JsonValue = None
    kind: str | None = None
    observation_kind: str | None = None
    observed_at: datetime | str | None = None
    occurred_at: datetime | str | None = None
    metadata: JsonObject = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_observation_content(self) -> ObservationCompatibility:
        if not any(
            value is not None
            for value in (
                self.id,
                self.event_id,
                self.source_event_id,
                self.source_id,
                self.text,
                self.content,
                self.payload,
                self.value,
            )
        ):
            raise ValueError("observation must contain an identifying or content field")
        return self


class LegacyQueryAnswer(ApiModel):
    text: str | None = None
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = "ok"
    metadata: JsonObject = Field(default_factory=dict)


class LegacyQueryEvidence(ApiModel):
    id: str
    excerpt: str
    text: str | None = None
    source: str | None = None
    score: float = 0.0
    timestamp: JsonValue = None
    metadata: JsonObject = Field(default_factory=dict)
    citation: str | None = None
    retrieval_sources: list[str] = Field(default_factory=list)


class LegacySearchResult(ApiModel):
    id: str
    text: str = ""
    content: str | None = None
    score: float = 0.0
    source: str | None = None
    timestamp: JsonValue = None
    metadata: JsonObject = Field(default_factory=dict)
    fts_score: float | None = None
    semantic_score: float | None = None
    combined_score: float | None = None
    retrieval_sources: list[str] = Field(default_factory=list)
    rank: int | None = None


class LegacyQueryResponse(ApiModel):
    """The richer answer/evidence response used by the existing runtime."""

    query: str | None = None
    answer: LegacyQueryAnswer = Field(default_factory=LegacyQueryAnswer)
    answer_text: str | None = None
    evidence: list[LegacyQueryEvidence] = Field(default_factory=list)
    results: list[LegacySearchResult] = Field(default_factory=list)
    meta: JsonObject = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class LegacyTimelineResponse(ApiModel):
    """The source-event timeline shape retained for dashboard compatibility."""

    items: list[dict[str, JsonValue]] = Field(default_factory=list)
    total_count: int = Field(default=0, ge=0)
    generated_at: str | None = None


class LegacyHealthResponse(ApiModel):
    """The existing health response with its stable ``ok`` status value."""

    status: Literal["ok", "not_ready"]
    service: str
    version: str | None = None
    ready: bool = False


PublicQueryRequest: TypeAlias = QueryRequest | QueryRequestCompatibility
PublicIngestRequest: TypeAlias = CaptureEnvelope | IngestBatchCompatibility
PublicObservationRequest: TypeAlias = ObservationV1 | ObservationCompatibility
PublicQueryResponse: TypeAlias = QueryResponse | LegacyQueryResponse
PublicTimelineResponse: TypeAlias = QueryResponse | LegacyTimelineResponse
# ``/health`` currently emits the stable legacy ``ok/not_ready`` payload used
# by the desktop client and probes.  Keep ``HealthSnapshot`` in this union so
# the shared contract remains discoverable in OpenAPI and can be adopted by a
# future version without changing the existing wire shape in place.
PublicHealthResponse: TypeAlias = HealthSnapshot | LegacyHealthResponse


__all__ = [
    "IngestBatchCompatibility",
    "LegacyHealthResponse",
    "LegacyQueryAnswer",
    "LegacyQueryEvidence",
    "LegacyQueryResponse",
    "LegacySearchResult",
    "LegacyTimelineResponse",
    "ObservationCompatibility",
    "PublicHealthResponse",
    "PublicIngestRequest",
    "PublicObservationRequest",
    "PublicQueryRequest",
    "PublicQueryResponse",
    "PublicTimelineResponse",
    "QueryRequestCompatibility",
]
