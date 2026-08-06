"""Project context-server runtime results onto the shared contract JSON.

The context-server runtime predates the transport-neutral contracts.  Its
search response contains answers, textual evidence, retrieval diagnostics, and
``results``; its timeline response contains source-event records in ``items``.
The shared wire contract intentionally exposes only ``ObservationV1`` values
inside a paged ``QueryResponse``.

This module is an isolated compatibility boundary.  It does not change either
the runtime's richer response or the shared contract models.  The raw runtime
payload is projected unchanged into ``ObservationV1.value``.  Artifact
references are projected into ``ObservationV1.evidence``; when the runtime
only exposes an artifact digest and URL, the adapter still emits a reference
with ``kind="other"`` rather than dropping the evidence.

Some runtime fields have no shared-contract home.  Their names are listed in
``UNREPRESENTED_RUNTIME_FIELDS`` and, for observation-level input, the actual
values are retained under ``metadata["contracts_adapter"]``.  Query-level
answer and retrieval fields cannot be attached to a ``QueryResponse`` without
violating its ``extra="forbid"`` model, so they are documented by the same
constant rather than being presented as contract fields.
"""

from __future__ import annotations

import base64
import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, time
from enum import Enum
from typing import Any, Final, TypeAlias

try:
    from contracts import (
        ArtifactKind,
        ObservationKind,
        ObservationStatus,
        ObservationV1,
        QueryResponse,
    )
except ModuleNotFoundError:  # Running from a source checkout without an install.
    from packages.contracts.python.contracts import (
        ArtifactKind,
        ObservationKind,
        ObservationStatus,
        ObservationV1,
        QueryResponse,
    )


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

ADAPTER_NAME: Final = "context-server-runtime-to-contracts-v1"
COMPATIBILITY_METADATA_KEY: Final = "contracts_adapter"

# These are runtime field names, not new contract fields.  They make the
# intentional boundary auditable for callers and reviewers.
UNREPRESENTED_RUNTIME_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "observation": (
        "capture_id",
        "captured_at",
        "occurred_at",
        "text",
        "content",
        "source_id",
        "source",
        "timestamp",
        "score",
        "fts_score",
        "semantic_score",
        "combined_score",
        "retrieval_sources",
        "rank",
    ),
    "query_response": (
        "query",
        "answer",
        "answer_text",
        "evidence",
        "results",
        "items",
        "generated_at",
        "meta",
        "warnings",
    ),
}

# Descriptive aliases are useful to callers that want to state the boundary
# explicitly without depending on the internal dictionary key names.
OBSERVATION_UNREPRESENTED_FIELDS: Final = UNREPRESENTED_RUNTIME_FIELDS["observation"]
QUERY_RESPONSE_UNREPRESENTED_FIELDS: Final = UNREPRESENTED_RUNTIME_FIELDS["query_response"]

_MISSING = object()
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ALLOWED_KINDS = frozenset(item.value for item in ObservationKind)
_ALLOWED_STATUSES = frozenset(item.value for item in ObservationStatus)
_ALLOWED_ARTIFACT_KINDS = frozenset(item.value for item in ArtifactKind)
_SHA256_LENGTH = 64
_ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "artifact_url",
        "artifact_kind",
        "artifact_type",
        "digest",
        "hash",
        "kind",
        "media_type",
        "mime_type",
        "name",
        "relative_path",
        "sha256",
        "size",
        "size_bytes",
        "uri",
        "url",
    }
)


def _jsonable(value: Any) -> JsonValue:
    """Return a JSON-compatible value without changing JSON strings.

    Runtime values are normally already decoded from SQLite JSON.  The small
    amount of coercion here also makes the adapter safe for dataclass/Pydantic
    test doubles and records supplied directly by an embedding application.
    Bytes use a tagged base64 object because JSON has no byte type.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return _jsonable(value.model_dump(mode="json"))
        except TypeError:
            return _jsonable(value.model_dump())
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    """Normalize a runtime record or response into a shallow mapping."""

    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            dumped = value.model_dump(mode="json")
        except TypeError:
            dumped = value.model_dump()
        return dict(dumped)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return dict(value.to_dict())
    if is_dataclass(value):
        return dict(asdict(value))
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": value}


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = _MISSING) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _nonempty(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _stable_identifier(value: Mapping[str, Any], *, prefix: str) -> str:
    encoded = repr(_jsonable(value)).encode("utf-8", errors="replace")
    return f"{prefix}-{hashlib.sha1(encoded).hexdigest()[:16]}"


def _timestamp(value: Any) -> str:
    """Normalize a runtime timestamp to the aware form required by contracts."""

    if value is None:
        return _UTC_EPOCH.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    if isinstance(value, datetime):
        current = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"runtime timestamp is not ISO-8601: {value!r}") from exc
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


def _metadata_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(_jsonable(value))
    if value is None:
        return {}
    return {"runtime_value": _jsonable(value)}


def _artifact_items(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (str, bytes, bytearray)):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(value)
    return (value,)


def _looks_like_artifact(value: Any, *, allow_id_only: bool = False) -> bool:
    if isinstance(value, (str, bytes, bytearray)):
        return allow_id_only
    candidate = _mapping(value)
    if any(key in candidate for key in _ARTIFACT_KEYS):
        return True
    return allow_id_only and "id" in candidate and not any(
        key in candidate for key in ("excerpt", "text", "source", "score", "timestamp")
    )


def _artifact_candidate(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"artifact_id": value}
    if isinstance(value, bytes):
        return {"artifact_id": value.decode("utf-8", errors="replace")}
    candidate = _mapping(value)
    if not candidate:
        return None
    return candidate


def _infer_artifact_kind(candidate: Mapping[str, Any]) -> str:
    supplied = _first(candidate, "kind", "artifact_kind", "artifact_type")
    if supplied is not _MISSING:
        normalized = str(getattr(supplied, "value", supplied)).strip().lower()
        if normalized in _ALLOWED_ARTIFACT_KINDS:
            return normalized
    media_type = str(_first(candidate, "media_type", "mime_type", default="")).lower()
    if media_type.startswith("text/"):
        return "text"
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    if media_type in {"application/zip", "application/x-tar", "application/gzip"}:
        return "archive"
    if media_type in {"application/json", "application/ld+json"}:
        return "structured"
    if media_type in {"application/pdf", "application/msword"}:
        return "document"
    return "other"


def _artifact_ref(value: Any) -> dict[str, Any] | None:
    candidate = _artifact_candidate(value)
    if candidate is None:
        return None
    metadata = _metadata_mapping(candidate.get("metadata"))
    known = {
        "artifact_id",
        "id",
        "digest",
        "hash",
        "sha256",
        "uri",
        "url",
        "artifact_url",
        "name",
        "media_type",
        "mime_type",
        "size_bytes",
        "size",
        "kind",
        "artifact_kind",
        "artifact_type",
        "metadata",
    }
    unknown = {key: _jsonable(item) for key, item in candidate.items() if key not in known}
    if unknown:
        runtime_metadata = metadata.get("runtime")
        if not isinstance(runtime_metadata, Mapping):
            runtime_metadata = {}
        metadata["runtime"] = {**dict(runtime_metadata), **unknown}

    digest = _first(candidate, "sha256", "digest", "hash")
    digest_text = str(digest).strip().lower() if digest is not _MISSING else ""
    artifact_id = _first(candidate, "artifact_id", "id", "digest", "sha256", "hash")
    uri = _first(candidate, "uri", "artifact_url", "url")
    if artifact_id is _MISSING and uri is not _MISSING:
        artifact_id = f"artifact-{hashlib.sha1(str(uri).encode('utf-8')).hexdigest()[:16]}"
    if artifact_id is _MISSING:
        return None
    artifact_id_text = _nonempty(artifact_id, "artifact-unknown")

    if digest_text and (
        len(digest_text) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in digest_text)
    ):
        metadata.setdefault("runtime_sha256", _jsonable(digest))
        digest_text = ""

    size = _first(candidate, "size_bytes", "size")
    size_bytes: int | None = None
    if size is not _MISSING:
        try:
            size_bytes = int(size)
        except (TypeError, ValueError):
            metadata.setdefault("runtime_size", _jsonable(size))
        if size_bytes is not None and size_bytes < 0:
            metadata.setdefault("runtime_size", size_bytes)
            size_bytes = None

    result: dict[str, Any] = {
        "artifact_id": artifact_id_text,
        "kind": _infer_artifact_kind(candidate),
        "metadata": metadata,
    }
    if uri is not _MISSING:
        result["uri"] = _nonempty(uri, artifact_id_text)
    for output_key, candidate_keys in (
        ("name", ("name",)),
        ("media_type", ("media_type", "mime_type")),
    ):
        current = _first(candidate, *candidate_keys)
        if current is not _MISSING:
            result[output_key] = _nonempty(current, artifact_id_text)
    if size_bytes is not None:
        result["size_bytes"] = size_bytes
    if digest_text:
        result["sha256"] = digest_text
    return result


def _artifact_candidates(
    raw: Mapping[str, Any],
    *,
    artifact_refs: Iterable[Any] | Mapping[str, Any] | None = None,
    artifacts: Iterable[Any] | Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    payload = raw.get("payload")
    candidates: list[Any] = []

    extra_items: tuple[Any, ...] = ()
    for supplied in (artifact_refs, artifacts):
        if supplied is None:
            continue
        if isinstance(supplied, Mapping) and _looks_like_artifact(supplied, allow_id_only=True):
            extra_items += (supplied,)
        elif isinstance(supplied, Mapping):
            extra_items += tuple(supplied.values())
        else:
            extra_items += _artifact_items(supplied)
    if extra_items:
        candidates.extend(extra_items)
        for extra in extra_items:
            extra_mapping = _mapping(extra)
            extra_metadata = (
                extra_mapping.get("metadata")
                if isinstance(extra_mapping.get("metadata"), Mapping)
                else {}
            )
            for container in (extra_mapping, extra_metadata):
                for key in ("artifacts", "artifact_refs", "artifact_ref", "artifact"):
                    candidates.extend(_artifact_items(container.get(key)))
    for container in (raw, metadata, payload if isinstance(payload, Mapping) else {}):
        for key in ("artifacts", "artifact_refs", "artifact_ref"):
            candidates.extend(_artifact_items(container.get(key)))
        for key in ("artifact",):
            item = container.get(key)
            if item is not None:
                candidates.append(item)

    # Runtime._observation_dict exposes only the primary digest and URL.  Make
    # that narrow shape a real ArtifactRef instead of silently omitting it.
    for container in (raw, metadata):
        digest = _first(container, "artifact_digest", "sha256", "digest")
        uri = _first(container, "artifact_url", "uri", "artifact_uri")
        if digest is not _MISSING or uri is not _MISSING:
            candidates.append(
                {
                    "artifact_id": digest if digest is not _MISSING else uri,
                    "sha256": digest if digest is not _MISSING else None,
                    "uri": uri if uri is not _MISSING else None,
                    "kind": _first(container, "artifact_kind", "artifact_type", default=None),
                    "media_type": _first(
                        container, "artifact_media_type", "media_type", default=None
                    ),
                }
            )

    for evidence in _artifact_items(raw.get("evidence")):
        evidence_mapping = _mapping(evidence)
        for key in ("artifact", "artifact_ref", "artifacts", "artifact_refs"):
            candidates.extend(_artifact_items(evidence_mapping.get(key)))
        if _looks_like_artifact(evidence_mapping):
            candidates.append(evidence_mapping)

    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        if not _looks_like_artifact(candidate, allow_id_only=True):
            continue
        reference = _artifact_ref(candidate)
        if reference is None:
            continue
        key = (
            str(reference.get("artifact_id", "")),
            str(reference.get("sha256", "")),
            str(reference.get("uri", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(reference)
    return tuple(output)


def _payload(raw: Mapping[str, Any]) -> Any:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    value = _first(raw, "payload", "value")
    if value is _MISSING:
        value = _first(metadata, "payload", "value")
    if value is _MISSING:
        value = _first(raw, "text", "content", default=None)
    return None if value is _MISSING else _jsonable(value)


def _score(value: Any, default: float = 1.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(score):
        return default
    return max(0.0, min(1.0, score))


def _contract_kind(raw: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    value = _first(raw, "observation_kind", "observation_type", "kind")
    if value is _MISSING:
        value = _first(metadata, "observation_kind", "observation_type", "kind", default="fact")
    normalized = str(getattr(value, "value", value)).strip().lower()
    return normalized if normalized in _ALLOWED_KINDS else "fact"


def _contract_status(raw: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    value = _first(raw, "status")
    if value is _MISSING:
        value = _first(metadata, "status", default="candidate")
    normalized = str(getattr(value, "value", value)).strip().lower()
    return normalized if normalized in _ALLOWED_STATUSES else "candidate"


def _observation_metadata(
    raw: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    source_shape: str,
    artifact_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = _metadata_mapping(metadata)
    for key in (
        "capture_id",
        "source_id",
        "source",
        "observed_at",
        "occurred_at",
        "captured_at",
        "timestamp",
        "text",
        "content",
        "kind",
        "score",
        "fts_score",
        "semantic_score",
        "combined_score",
        "retrieval_sources",
        "rank",
    ):
        if key in raw and key not in result:
            result[key] = _jsonable(raw[key])

    actual_unrepresented = [
        key
        for key in OBSERVATION_UNREPRESENTED_FIELDS
        if key in raw or key in metadata
    ]
    runtime_fields = {
        key: _jsonable(raw[key])
        for key in actual_unrepresented
        if key in raw
    }
    provided_compatibility = result.get(COMPATIBILITY_METADATA_KEY)
    compatibility = (
        dict(provided_compatibility)
        if isinstance(provided_compatibility, Mapping)
        else {}
    )
    if provided_compatibility is not None and not isinstance(provided_compatibility, Mapping):
        compatibility["runtime_metadata_value"] = _jsonable(provided_compatibility)
    compatibility.update(
        {
            "adapter": ADAPTER_NAME,
            "source_shape": source_shape,
            "unrepresented_fields": actual_unrepresented,
        }
    )
    if runtime_fields:
        compatibility["runtime_fields"] = runtime_fields
    if artifact_refs:
        compatibility["artifact_evidence_count"] = len(artifact_refs)
    result[COMPATIBILITY_METADATA_KEY] = compatibility
    return result


def adapt_observation(
    observation: Any,
    *,
    artifact_refs: Iterable[Any] | Mapping[str, Any] | None = None,
    artifacts: Iterable[Any] | Mapping[str, Any] | None = None,
    source_shape: str = "runtime_observation",
) -> dict[str, Any]:
    """Return one runtime observation as JSON compatible with ``ObservationV1``.

    ``observation`` may be a timeline item, a search result, a ``SearchHit``,
    or any mapping with the corresponding runtime field names.  The function
    validates its final result with the shared model before returning a plain
    JSON dictionary.
    """

    raw = _mapping(observation)
    metadata = _metadata_mapping(raw.get("metadata"))
    identifier = _first(raw, "observation_id", "id")
    if identifier is _MISSING:
        identifier = _stable_identifier(raw, prefix="observation")
    subject = _first(raw, "subject")
    if subject is _MISSING:
        subject = _first(metadata, "source_id", "source", default=None)
    if subject is _MISSING or subject is None:
        subject = _first(raw, "source_id", "source", default="context")
    predicate = _first(raw, "predicate")
    if predicate is _MISSING:
        predicate = _first(metadata, "predicate", "kind", default=None)
    if predicate is _MISSING or predicate is None:
        predicate = _first(raw, "kind", "event_type", default="observed")

    observed_at = _first(raw, "observed_at", "timestamp", "occurred_at", "captured_at")
    if observed_at is _MISSING:
        observed_at = _first(metadata, "observed_at", "occurred_at", "captured_at", default=None)
    evidence = _artifact_candidates(raw, artifact_refs=artifact_refs, artifacts=artifacts)
    source_event_id = _first(raw, "source_event_id", "event_id")
    if source_event_id is _MISSING:
        source_event_id = _first(metadata, "event_id", "source_event_id", default=identifier)

    data: dict[str, Any] = {
        "schema_version": "1",
        "observation_id": _nonempty(identifier, _stable_identifier(raw, prefix="observation")),
        "subject": _nonempty(subject, "context"),
        "predicate": _nonempty(predicate, "observed"),
        "value": _payload(raw),
        "kind": _contract_kind(raw, metadata),
        "status": _contract_status(raw, metadata),
        "confidence": _score(_first(raw, "confidence", "score", default=1.0)),
        "observed_at": _timestamp(None if observed_at is _MISSING else observed_at),
        "source_event_id": _nonempty(source_event_id, _nonempty(identifier, "runtime-observation")),
        "evidence": list(evidence),
        "metadata": _observation_metadata(
            raw,
            metadata=metadata,
            source_shape=source_shape,
            artifact_refs=evidence,
        ),
    }
    model = ObservationV1.model_validate(data)
    return dict(model.model_dump(mode="json"))


def _response_mapping(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    if isinstance(response, (list, tuple)):
        return {"items": list(response)}
    return _mapping(response)


def _response_items(response: Mapping[str, Any]) -> tuple[Any, ...]:
    for key in ("observations", "results", "items"):
        if key in response and response[key] is not None:
            return _artifact_items(response[key])
    if any(key in response for key in ("observation_id", "id", "payload", "value")):
        return (response,)
    return ()


def _evidence_by_id(response: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for item in _artifact_items(response.get("evidence")):
        mapping = _mapping(item)
        identifier = _first(mapping, "observation_id", "id")
        if identifier is not _MISSING:
            grouped.setdefault(str(identifier), []).append(item)
    return {key: tuple(value) for key, value in grouped.items()}


def adapt_query_response(
    response: Any,
    *,
    request_id: str | None = None,
    total_count: int | None = None,
    next_cursor: str | None = None,
    source_shape: str = "runtime_query",
) -> dict[str, Any]:
    """Return a runtime search/timeline response as shared ``QueryResponse`` JSON.

    Runtime ``results`` and timeline ``items`` become ``observations``.  A
    matching runtime textual evidence record is supplied to the observation
    adapter so artifact references nested there are not lost.  Runtime answer,
    query, pagination diagnostics, and retrieval metadata have no fields in
    the shared response model; see ``QUERY_RESPONSE_UNREPRESENTED_FIELDS``.
    """

    raw = _response_mapping(response)
    if source_shape == "runtime_timeline" and "items" not in raw and "observations" not in raw:
        source_shape = "runtime_observation"
    items = _response_items(raw)
    evidence_by_id = _evidence_by_id(raw)
    observations: list[dict[str, Any]] = []
    for item in items:
        item_mapping = _mapping(item)
        identifier = _first(item_mapping, "observation_id", "id")
        extras = evidence_by_id.get(str(identifier), ()) if identifier is not _MISSING else ()
        observations.append(
            adapt_observation(
                item_mapping,
                artifact_refs=extras,
                source_shape=source_shape,
            )
        )

    response_request_id = request_id if request_id is not None else _first(raw, "request_id")
    if response_request_id is _MISSING:
        response_request_id = None
    response_cursor = next_cursor if next_cursor is not None else _first(raw, "next_cursor")
    if response_cursor is _MISSING:
        response_cursor = None
    count = total_count if total_count is not None else _first(raw, "total_count")
    if count is _MISSING:
        meta = raw.get("meta") if isinstance(raw.get("meta"), Mapping) else {}
        count = _first(meta, "total_count", "result_count", default=len(observations))
    try:
        normalized_count = max(len(observations), int(count))
    except (TypeError, ValueError):
        normalized_count = len(observations)

    data: dict[str, Any] = {
        "request_id": response_request_id,
        "observations": observations,
        "total_count": normalized_count,
        "next_cursor": response_cursor,
    }
    model = QueryResponse.model_validate(data)
    return dict(model.model_dump(mode="json"))


def adapt_search_response(response: Any, **kwargs: Any) -> dict[str, Any]:
    """Adapt a runtime ``search`` response to shared ``QueryResponse`` JSON."""

    kwargs.setdefault("source_shape", "runtime_search")
    return adapt_query_response(response, **kwargs)


def adapt_timeline(response: Any, **kwargs: Any) -> dict[str, Any]:
    """Adapt a runtime ``timeline``/``observations`` response to shared JSON."""

    kwargs.setdefault("source_shape", "runtime_timeline")
    return adapt_query_response(response, **kwargs)


# Explicit aliases keep the module easy to discover for callers that use
# conversion vocabulary rather than the ``adapt_*`` naming convention.
observation_to_v1 = adapt_observation
search_to_query_response = adapt_search_response
timeline_to_query_response = adapt_timeline
adapt_runtime_observation = adapt_observation
adapt_runtime_search = adapt_search_response
adapt_runtime_timeline = adapt_timeline
project_observation = adapt_observation
project_query_response = adapt_query_response


__all__ = [
    "ADAPTER_NAME",
    "COMPATIBILITY_METADATA_KEY",
    "OBSERVATION_UNREPRESENTED_FIELDS",
    "QUERY_RESPONSE_UNREPRESENTED_FIELDS",
    "UNREPRESENTED_RUNTIME_FIELDS",
    "adapt_observation",
    "adapt_query_response",
    "adapt_runtime_observation",
    "adapt_runtime_search",
    "adapt_runtime_timeline",
    "adapt_search_response",
    "adapt_timeline",
    "observation_to_v1",
    "project_observation",
    "project_query_response",
    "search_to_query_response",
    "timeline_to_query_response",
]
