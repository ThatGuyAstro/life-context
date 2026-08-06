"""Dependency-free request and response models used by query orchestration."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from enum import Enum
from math import isfinite
from typing import Any


_PROVENANCE_METADATA_KEYS = (
    "capture_id",
    "source_id",
    "kind",
    "payload",
    "subject",
    "predicate",
    "status",
)
_EVIDENCE_METADATA_KEYS = (
    "artifact_digest",
    "artifact_url",
    "artifact_uri",
    "uri",
)


def _jsonable(value: Any) -> Any:
    """Convert common contract values to JSON-compatible primitives."""

    if isinstance(value, Enum):
        return _jsonable(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return str(value)


def _float_or(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _stable_id(text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
    return f"hit-{digest[:16]}"


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A normalized result returned by either retrieval backend."""

    id: str
    text: str = ""
    score: float = 0.0
    source: str | None = None
    timestamp: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    fts_score: float | None = None
    semantic_score: float | None = None
    combined_score: float | None = None
    retrieval_sources: tuple[str, ...] = ()
    rank: int | None = None
    raw: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_value(cls, value: Any, *, default_source: str | None = None) -> SearchHit:
        """Normalize a shared-contract object, mapping, or simple test stub."""

        if isinstance(value, cls):
            if value.source or not default_source:
                return value
            return cls(
                id=value.id,
                text=value.text,
                score=value.score,
                source=default_source,
                timestamp=value.timestamp,
                metadata=value.metadata,
                fts_score=value.fts_score,
                semantic_score=value.semantic_score,
                combined_score=value.combined_score,
                retrieval_sources=value.retrieval_sources,
                rank=value.rank,
                raw=value.raw,
            )

        if isinstance(value, Mapping):
            get = value.get
            identifier = get("id", get("document_id", get("key", get("uuid"))))
            text = get(
                "text",
                get("text_content", get("content", get("snippet", get("body", get("value", ""))))),
            )
            score = get("score", get("relevance", get("similarity", 0.0)))
            source = get("source", get("source_id", get("origin", default_source)))
            timestamp = get("timestamp", get("created_at", get("observed_at", get("occurred_at"))))
            metadata = get("metadata", {})
            if not isinstance(metadata, Mapping):
                metadata = {"value": metadata}
            metadata = dict(metadata)
            for key in _PROVENANCE_METADATA_KEYS:
                if key in value and key not in metadata:
                    metadata[key] = value[key]
            for key in _EVIDENCE_METADATA_KEYS:
                if key in value and value[key] is not None and key not in metadata:
                    metadata[key] = value[key]
            if identifier is None:
                identifier = _stable_id(f"{source}|{text}|{timestamp}")
            return cls(
                id=str(identifier),
                text="" if text is None else str(text),
                score=_float_or(score),
                source=None if source is None else str(source),
                timestamp=timestamp,
                metadata=metadata,
                fts_score=_float_or(get("fts_score"), None)
                if get("fts_score") is not None
                else None,
                semantic_score=_float_or(get("semantic_score"), None)
                if get("semantic_score") is not None
                else None,
                combined_score=_float_or(get("combined_score"), None)
                if get("combined_score") is not None
                else None,
                retrieval_sources=tuple(str(item) for item in (get("retrieval_sources") or ())),
                rank=int(get("rank")) if get("rank") is not None else None,
                raw=value,
            )

        identifier = getattr(value, "id", None)
        text = getattr(
            value,
            "text",
            getattr(value, "text_content", getattr(value, "content", getattr(value, "value", ""))),
        )
        source = getattr(value, "source", getattr(value, "source_id", default_source))
        timestamp = getattr(
            value,
            "timestamp",
            getattr(
                value,
                "created_at",
                getattr(value, "observed_at", getattr(value, "occurred_at", None)),
            ),
        )
        if identifier is None:
            identifier = _stable_id(f"{source}|{text}|{timestamp}")
        metadata = getattr(value, "metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {"value": metadata}
        metadata = dict(metadata)
        for key in _PROVENANCE_METADATA_KEYS:
            candidate = getattr(value, key, None)
            if candidate is not None and key not in metadata:
                metadata[key] = candidate
        for key in _EVIDENCE_METADATA_KEYS:
            candidate = getattr(value, key, None)
            if candidate is not None and key not in metadata:
                metadata[key] = candidate
        return cls(
            id=str(identifier),
            text="" if text is None else str(text),
            score=_float_or(getattr(value, "score", 0.0)),
            source=None if source is None else str(source),
            timestamp=timestamp,
            metadata=metadata,
            fts_score=getattr(value, "fts_score", None),
            semantic_score=getattr(value, "semantic_score", None),
            combined_score=getattr(value, "combined_score", None),
            retrieval_sources=tuple(
                str(item) for item in (getattr(value, "retrieval_sources", ()) or ())
            ),
            rank=getattr(value, "rank", None),
            raw=value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "content": self.text,
            "score": self.score,
            "source": self.source,
            "timestamp": _jsonable(self.timestamp),
            "metadata": _jsonable(dict(self.metadata)),
            "fts_score": self.fts_score,
            "semantic_score": self.semantic_score,
            "combined_score": self.combined_score,
            "retrieval_sources": list(self.retrieval_sources),
            "rank": self.rank,
        }


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """Canonical query options shared by API, orchestration, and CLI."""

    query: str
    limit: int = 10
    mode: str = "hybrid"
    filters: Mapping[str, Any] = field(default_factory=dict)
    start: Any = None
    end: Any = None
    fts_weight: float = 0.5
    semantic_weight: float = 0.5
    include_evidence: bool = True
    evidence_limit: int | None = None
    explain: bool = False

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.mode not in {"hybrid", "fts", "semantic"}:
            raise ValueError("mode must be one of: hybrid, fts, semantic")
        if self.fts_weight < 0 or self.semantic_weight < 0:
            raise ValueError("query weights must be non-negative")
        if self.fts_weight == 0 and self.semantic_weight == 0:
            raise ValueError("at least one query weight must be positive")

    @classmethod
    def from_value(cls, value: Any, **overrides: Any) -> QueryRequest:
        if isinstance(value, cls):
            values = {
                "query": value.query,
                "limit": value.limit,
                "mode": value.mode,
                "filters": dict(value.filters),
                "start": value.start,
                "end": value.end,
                "fts_weight": value.fts_weight,
                "semantic_weight": value.semantic_weight,
                "include_evidence": value.include_evidence,
                "evidence_limit": value.evidence_limit,
                "explain": value.explain,
            }
        elif isinstance(value, str):
            values = {"query": value}
        elif hasattr(value, "model_dump") and callable(value.model_dump):
            # The shared contracts are Pydantic models.  Convert them through
            # their public serialization surface so structured filters such as
            # subject/predicate and observation windows are not discarded.
            try:
                values = cls.from_value(value.model_dump()).to_dict()
            except TypeError:
                values = cls.from_value(value.model_dump(mode="json")).to_dict()
        elif isinstance(value, Mapping):
            values = {
                "query": value.get("query", value.get("q", value.get("text", ""))),
                "limit": value.get("limit", 10),
                "mode": str(value.get("mode", "hybrid")).lower(),
                "filters": value.get("filters", {}),
                "start": value.get("start", value.get("from", value.get("observed_after"))),
                "end": value.get("end", value.get("to", value.get("observed_before"))),
                "fts_weight": value.get("fts_weight", value.get("lexical_weight", 0.5)),
                "semantic_weight": value.get("semantic_weight", value.get("vector_weight", 0.5)),
                "include_evidence": value.get("include_evidence", True),
                "evidence_limit": value.get("evidence_limit"),
                "explain": value.get("explain", False),
            }
        else:
            values = {
                "query": getattr(value, "query", getattr(value, "text", "")),
                "limit": getattr(value, "limit", 10),
                "mode": getattr(value, "mode", "hybrid"),
                "filters": getattr(value, "filters", {}),
                "start": getattr(value, "start", getattr(value, "from_", None)),
                "end": getattr(value, "end", getattr(value, "to", None)),
                "fts_weight": getattr(value, "fts_weight", 0.5),
                "semantic_weight": getattr(value, "semantic_weight", 0.5),
                "include_evidence": getattr(value, "include_evidence", True),
                "evidence_limit": getattr(value, "evidence_limit", None),
                "explain": getattr(value, "explain", False),
            }
        values.update(overrides)
        values["query"] = "" if values.get("query") is None else str(values.get("query", ""))
        values["limit"] = int(values.get("limit", 10))
        values["mode"] = str(values.get("mode", "hybrid")).lower()
        filters = dict(values.get("filters") or {})
        if isinstance(value, Mapping):
            # Preserve the structured fields from the shared QueryRequest
            # contract without forcing the lexical/semantic adapters to know
            # about that package's Pydantic model.
            for key in (
                "subject",
                "predicate",
                "source_ids",
                "kinds",
                "statuses",
                "sort_order",
                "cursor",
            ):
                if value.get(key) is not None and key not in filters:
                    filters[key] = value[key]
        values["filters"] = filters
        values["fts_weight"] = _float_or(values.get("fts_weight"), 0.5)
        values["semantic_weight"] = _float_or(values.get("semantic_weight"), 0.5)
        values["include_evidence"] = bool(values.get("include_evidence", True))
        if values.get("evidence_limit") is not None:
            values["evidence_limit"] = max(1, int(values["evidence_limit"]))
        values["explain"] = bool(values.get("explain", False))
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "limit": self.limit,
            "mode": self.mode,
            "filters": _jsonable(dict(self.filters)),
            "start": _jsonable(self.start),
            "end": _jsonable(self.end),
            "fts_weight": self.fts_weight,
            "semantic_weight": self.semantic_weight,
            "include_evidence": self.include_evidence,
            "evidence_limit": self.evidence_limit,
            "explain": self.explain,
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    """A citation-friendly projection of a retrieved result."""

    id: str
    excerpt: str
    source: str | None = None
    score: float = 0.0
    timestamp: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    citation: str | None = None
    retrieval_sources: tuple[str, ...] = ()

    @classmethod
    def from_hit(cls, hit: SearchHit) -> Evidence:
        citation = hit.source or hit.id
        return cls(
            id=hit.id,
            excerpt=hit.text,
            source=hit.source,
            score=hit.combined_score if hit.combined_score is not None else hit.score,
            timestamp=hit.timestamp,
            metadata=hit.metadata,
            citation=citation,
            retrieval_sources=hit.retrieval_sources,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "excerpt": self.excerpt,
            "text": self.excerpt,
            "source": self.source,
            "score": self.score,
            "timestamp": _jsonable(self.timestamp),
            "metadata": _jsonable(dict(self.metadata)),
            "citation": self.citation,
            "retrieval_sources": list(self.retrieval_sources),
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """Normalized answer-generator output."""

    text: str | None = None
    confidence: float | None = None
    evidence_ids: tuple[str, ...] = ()
    status: str = "ok"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "status": self.status,
            "metadata": _jsonable(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class QueryResponse:
    """Answer plus evidence and retrieval diagnostics."""

    request: QueryRequest
    answer: Answer
    evidence: tuple[Evidence, ...] = ()
    results: tuple[SearchHit, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        answer = self.answer.to_dict()
        payload: dict[str, Any] = {
            "query": self.request.query,
            "answer": answer,
            "answer_text": answer["text"],
            "evidence": [item.to_dict() for item in self.evidence],
            "results": [item.to_dict() for item in self.results],
            "meta": _jsonable(dict(self.meta)),
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


def unwrap_hits(value: Any) -> Sequence[Any]:
    """Extract a result sequence from common backend response shapes."""

    if value is None:
        return ()
    if isinstance(value, Mapping):
        for key in ("hits", "results", "items", "documents", "data"):
            if key in value:
                candidate = value[key]
                if candidate is None:
                    return ()
                if isinstance(candidate, Sequence) and not isinstance(
                    candidate, (str, bytes, bytearray)
                ):
                    return candidate
                return (candidate,)
        return (value,)
    for attr in ("hits", "results", "items", "documents"):
        candidate = getattr(value, attr, None)
        if candidate is not None:
            return tuple(candidate)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return (value,)


def jsonable(value: Any) -> Any:
    """Public alias used by API and CLI adapters."""

    return _jsonable(value)


__all__ = [
    "Answer",
    "Evidence",
    "QueryRequest",
    "QueryResponse",
    "SearchHit",
    "jsonable",
    "unwrap_hits",
]
