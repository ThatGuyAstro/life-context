"""Adapt persisted inference jobs into strict JSON multimodal requests.

This module is deliberately a boundary adapter.  It understands the JSON shape
written by the storage job queue and the existing :class:`PromptBundle`, but it
does not open artifact files, contact a model endpoint, or manage a model.  An
artifact is represented by the URI already present in the persisted payload;
when only a content hash is available, an ``artifact://`` URI is emitted so a
later runtime-owned resolver can make the local/remote policy decision.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .contracts import PromptBundle, canonical_json, sha256_json

ADAPTER_VERSION = "storage-job-multimodal-v1"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STRUCTURED_OUTPUT_MODES = {"json_schema", "json_object", "prompt_only"}
_ARTIFACT_KEYS = ("artifacts", "input_artifacts", "image_artifacts", "images")


class PersistedInferencePayloadError(ValueError):
    """Raised when a persisted inference job cannot be adapted safely."""


@dataclass(frozen=True)
class MultimodalPromptRecord:
    """JSON-safe model request plus the provenance needed to reproduce it."""

    job_id: str
    job_kind: str
    model: str | None
    messages: tuple[dict[str, Any], ...]
    response_format: dict[str, Any] | None
    provenance: dict[str, Any]
    artifact_refs: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, strictly JSON-serializable record."""

        value: dict[str, Any] = {
            "schema_version": "1",
            "record_type": "life_context.multimodal_prompt",
            "adapter_version": ADAPTER_VERSION,
            "job_id": self.job_id,
            "job_kind": self.job_kind,
            "model": self.model,
            "messages": copy.deepcopy(list(self.messages)),
            "response_format": copy.deepcopy(self.response_format),
            "artifact_refs": copy.deepcopy(list(self.artifact_refs)),
            "provenance": copy.deepcopy(self.provenance),
        }
        _assert_json_safe(value)
        return value

    def to_json(self) -> str:
        """Return canonical JSON suitable for a request log or fixture."""

        return canonical_json(self.to_dict())

    @property
    def user_content(self) -> list[dict[str, Any]]:
        """Return the multimodal content blocks from the user message."""

        for message in self.messages:
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    return copy.deepcopy(content)
        return []

    def model_request(self) -> dict[str, Any]:
        """Return only the OpenAI-compatible request fields."""

        request = {
            "model": self.model,
            "messages": copy.deepcopy(list(self.messages)),
        }
        if self.response_format is not None:
            request["response_format"] = copy.deepcopy(self.response_format)
        _assert_json_safe(request)
        return request


@dataclass(frozen=True)
class _JobEnvelope:
    job_id: str
    job_kind: str
    payload: Mapping[str, Any]
    model: str | None
    created_at: Any = None


@dataclass(frozen=True)
class _NormalizedArtifact:
    ref: dict[str, Any]
    is_image: bool
    image_url: str | None
    image_source: str | None


class PersistedInferenceJobAdapter:
    """Build multimodal prompt records from storage ``JobRecord`` values.

    The adapter accepts either a storage ``JobRecord``-like object, a mapping
    containing ``id``/``payload``, or the raw JSON payload itself.  For the raw
    form, pass ``job_id``.  The object is intentionally duck-typed so importing
    inference does not make the storage package a runtime dependency.
    """

    def __init__(
        self,
        *,
        structured_output_mode: str = "json_schema",
        artifact_uri_scheme: str = "artifact",
    ) -> None:
        mode = str(structured_output_mode).strip().lower()
        if mode not in _STRUCTURED_OUTPUT_MODES:
            raise PersistedInferencePayloadError(
                f"structured_output_mode must be one of {sorted(_STRUCTURED_OUTPUT_MODES)}"
            )
        scheme = str(artifact_uri_scheme).strip().rstrip(":")
        if not scheme or any(character in scheme for character in ":/\\"):
            raise PersistedInferencePayloadError("artifact_uri_scheme must be a URI scheme name")
        self.structured_output_mode = mode
        self.artifact_uri_scheme = scheme

    def adapt(
        self,
        job: Any,
        *,
        job_id: str | None = None,
        job_kind: str | None = None,
        model: str | None = None,
    ) -> MultimodalPromptRecord:
        """Convert one persisted job into a strict JSON prompt record."""

        envelope = _coerce_job_envelope(
            job,
            job_id=job_id,
            job_kind=job_kind,
            model=model,
        )
        payload = _json_value(envelope.payload)
        if not isinstance(payload, Mapping):
            raise PersistedInferencePayloadError("inference job payload must be a JSON object")
        _assert_json_safe(payload)

        prompt_payload = payload.get("prompt")
        if prompt_payload is None and {"system_prompt", "user_prompt", "contract"}.issubset(
            payload
        ):
            # Accept a flattened payload for forward compatibility with early
            # queue writers; the canonical writer uses payload["prompt"].
            prompt_payload = payload
        if not isinstance(prompt_payload, Mapping):
            raise PersistedInferencePayloadError(
                "inference job payload must contain a persisted prompt object"
            )
        try:
            prompt = PromptBundle.from_payload(prompt_payload)
        except (TypeError, ValueError, KeyError) as error:
            raise PersistedInferencePayloadError(f"invalid persisted prompt: {error}") from error

        normalized_artifacts = _collect_artifacts(payload, artifact_uri_scheme=self.artifact_uri_scheme)
        artifact_refs = tuple(item.ref for item in normalized_artifacts)
        image_items = tuple(item for item in normalized_artifacts if item.is_image)

        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt.user_prompt},
        ]
        for item in image_items:
            if item.image_url is None:
                raise PersistedInferencePayloadError(
                    f"image artifact {item.ref['artifact_id']} has no resolvable URI"
                )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": item.image_url, "detail": "auto"},
                }
            )

        messages = (
            {"role": "system", "content": prompt.system_prompt},
            {"role": "user", "content": user_content},
        )
        try:
            response_format = prompt.contract.response_format(self.structured_output_mode)
        except ValueError as error:
            raise PersistedInferencePayloadError(str(error)) from error

        artifact_provenance = [
            {
                "artifact_id": item.ref["artifact_id"],
                "kind": item.ref["kind"],
                "media_type": item.ref.get("media_type"),
                "sha256": item.ref.get("sha256"),
                "source": item.image_source,
                "included_as_image": item.is_image,
            }
            for item in normalized_artifacts
        ]
        provenance: dict[str, Any] = {
            "adapter_version": ADAPTER_VERSION,
            "job_id": envelope.job_id,
            "job_kind": envelope.job_kind,
            "model": envelope.model,
            "payload_sha256": sha256_json(payload),
            "prompt_version": prompt.prompt_version,
            "prompt_hash": prompt.prompt_hash,
            "system_prompt_hash": prompt.system_prompt_hash,
            "user_prompt_hash": prompt.user_prompt_hash,
            "schema_hash": prompt.schema_hash,
            "contract_name": prompt.contract.name,
            "contract_version": prompt.contract.version,
            "structured_output_mode": self.structured_output_mode,
            "multimodal": bool(image_items),
            "artifact_count": len(normalized_artifacts),
            "image_artifact_count": len(image_items),
            "artifacts": artifact_provenance,
        }
        if envelope.created_at is not None:
            provenance["job_created_at"] = _json_value(envelope.created_at)

        record = MultimodalPromptRecord(
            job_id=envelope.job_id,
            job_kind=envelope.job_kind,
            model=envelope.model,
            messages=messages,
            response_format=response_format,
            provenance=provenance,
            artifact_refs=artifact_refs,
        )
        # Validate the entire boundary before returning it.  This also catches
        # non-finite numbers in a model schema or source metadata.
        record.to_dict()
        return record


def build_multimodal_prompt_record(
    job: Any,
    *,
    job_id: str | None = None,
    job_kind: str | None = None,
    model: str | None = None,
    structured_output_mode: str = "json_schema",
    artifact_uri_scheme: str = "artifact",
) -> MultimodalPromptRecord:
    """Functional entry point for adapting a persisted inference job."""

    return PersistedInferenceJobAdapter(
        structured_output_mode=structured_output_mode,
        artifact_uri_scheme=artifact_uri_scheme,
    ).adapt(job, job_id=job_id, job_kind=job_kind, model=model)


# Friendly names for callers that describe the operation in either direction.
adapt_persisted_job = build_multimodal_prompt_record
persisted_job_to_prompt_record = build_multimodal_prompt_record


def _coerce_job_envelope(
    job: Any,
    *,
    job_id: str | None,
    job_kind: str | None,
    model: str | None,
) -> _JobEnvelope:
    if isinstance(job, Mapping):
        has_nested_payload = isinstance(job.get("payload"), Mapping) and "prompt" not in job
        if has_nested_payload:
            payload = job["payload"]
            resolved_id = job_id or job.get("id") or job.get("job_id")
            resolved_kind = job_kind or job.get("kind") or "inference"
            resolved_model = model
            created_at = job.get("created_at")
        else:
            payload = job
            resolved_id = job_id or job.get("job_id") or job.get("id")
            resolved_kind = job_kind or job.get("kind") or "inference"
            resolved_model = model
            created_at = job.get("created_at")
    else:
        payload = getattr(job, "payload", None)
        if payload is None:
            raise PersistedInferencePayloadError("job must provide a persisted payload mapping")
        resolved_id = job_id or getattr(job, "id", None) or getattr(job, "job_id", None)
        resolved_kind = job_kind or getattr(job, "kind", None) or "inference"
        resolved_model = model
        created_at = getattr(job, "created_at", None)

    if not isinstance(payload, Mapping):
        raise PersistedInferencePayloadError("persisted job payload must be a mapping")
    if not resolved_id or not isinstance(resolved_id, str):
        raise PersistedInferencePayloadError("job_id is required for a persisted inference job")
    if not isinstance(resolved_kind, str) or not resolved_kind.strip():
        raise PersistedInferencePayloadError("job_kind must be a non-empty string")
    if resolved_model is None:
        payload_model = payload.get("model")
        resolved_model = str(payload_model) if payload_model is not None else None
    elif not isinstance(resolved_model, str):
        raise PersistedInferencePayloadError("model must be a string when provided")

    return _JobEnvelope(
        job_id=resolved_id.strip(),
        job_kind=resolved_kind.strip(),
        payload=payload,
        model=resolved_model.strip() if isinstance(resolved_model, str) else None,
        created_at=created_at,
    )


def _collect_artifacts(
    payload: Mapping[str, Any], *, artifact_uri_scheme: str
) -> tuple[_NormalizedArtifact, ...]:
    candidates: list[tuple[Any, str | None]] = []

    def add(value: Any, hint: str | None) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple)):
            candidates.extend((item, hint) for item in value)
        else:
            candidates.append((value, hint))

    for key in _ARTIFACT_KEYS:
        add(payload.get(key), "image" if key in {"images", "image_artifacts"} else None)
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        for key in _ARTIFACT_KEYS:
            add(metadata.get(key), "image" if key in {"images", "image_artifacts"} else None)

    result: list[_NormalizedArtifact] = []
    seen: set[str] = set()
    for index, (raw, hint) in enumerate(candidates):
        item = _normalize_artifact(raw, hint=hint, index=index, artifact_uri_scheme=artifact_uri_scheme)
        key = canonical_json(item.ref)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _normalize_artifact(
    raw: Any,
    *,
    hint: str | None,
    index: int,
    artifact_uri_scheme: str,
) -> _NormalizedArtifact:
    if hasattr(raw, "to_dict") and callable(raw.to_dict):
        raw = raw.to_dict()
    if isinstance(raw, str):
        data: dict[str, Any] = {"uri": raw}
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        raise PersistedInferencePayloadError(
            f"artifact at index {index} must be a mapping or URI string"
        )

    kind = _string_value(data.get("kind") or data.get("artifact_kind") or hint or "other")
    media_type_value = data.get("media_type") or data.get("mime_type")
    media_type = (
        _string_value(media_type_value)
        if media_type_value is not None
        else None
    )
    uri = _artifact_uri(data)
    if not media_type and uri:
        guessed, _ = mimetypes.guess_type(uri.split("?", 1)[0])
        media_type = guessed
    is_image = kind.lower() in {"image", "screenshot", "contact_sheet"} or bool(
        media_type and media_type.lower().startswith("image/")
    )
    if hint == "image":
        is_image = True
    if is_image and not media_type:
        media_type = "image/jpeg"

    digest = data.get("sha256")
    if digest is not None:
        digest = _string_value(digest).lower()
        if not _SHA256_RE.fullmatch(digest):
            raise PersistedInferencePayloadError(
                f"artifact at index {index} has an invalid sha256 digest"
            )

    artifact_id = data.get("artifact_id", data.get("id"))
    if artifact_id is None:
        artifact_id = digest or uri or f"artifact-{index}"
    artifact_id = _string_value(artifact_id)

    metadata = data.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise PersistedInferencePayloadError(f"artifact {artifact_id} metadata must be an object")

    ref: dict[str, Any] = {
        "artifact_id": artifact_id,
        "kind": kind,
        "media_type": media_type,
        "sha256": digest,
        "uri": uri,
        "metadata": _json_value(metadata),
    }
    if data.get("name") is not None:
        ref["name"] = _string_value(data["name"])
    if data.get("size_bytes") is not None:
        try:
            size_bytes = int(data["size_bytes"])
        except (TypeError, ValueError) as error:
            raise PersistedInferencePayloadError(
                f"artifact {artifact_id} size_bytes must be an integer"
            ) from error
        if size_bytes < 0:
            raise PersistedInferencePayloadError(f"artifact {artifact_id} size_bytes cannot be negative")
        ref["size_bytes"] = size_bytes

    image_url: str | None = uri
    image_source = "uri" if uri else None
    embedded = _embedded_image_data(data, media_type)
    if embedded is not None:
        image_url = embedded
        image_source = "embedded"
    elif image_url is None and is_image and digest:
        # This is a reference, not a download.  A runtime resolver may map it
        # to a local file, data URI, or authenticated remote URL later.
        image_url = f"{artifact_uri_scheme}://{digest}"
        image_source = "deferred"

    return _NormalizedArtifact(
        ref=ref,
        is_image=is_image,
        image_url=image_url,
        image_source=image_source,
    )


def _artifact_uri(data: Mapping[str, Any]) -> str | None:
    value = data.get("uri") or data.get("url") or data.get("path")
    image_url = data.get("image_url")
    if value is None and isinstance(image_url, Mapping):
        value = image_url.get("url")
    elif value is None and isinstance(image_url, str):
        value = image_url
    if value is None:
        return None
    return _string_value(value)


def _embedded_image_data(data: Mapping[str, Any], media_type: str | None) -> str | None:
    direct = data.get("data_url") or data.get("data_uri")
    if direct is not None:
        value = _string_value(direct)
        if not value.lower().startswith("data:image/"):
            raise PersistedInferencePayloadError("embedded image data URL must use an image media type")
        return value

    encoded = data.get("base64") or data.get("data_base64")
    if encoded is None and data.get("data") is not None and not isinstance(data["data"], Mapping):
        encoded = data["data"]
    if encoded is None:
        return None
    value = _string_value(encoded)
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise PersistedInferencePayloadError("embedded image data must be valid base64") from error
    return f"data:{media_type or 'image/jpeg'};base64,{value}"


def _string_value(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or not value.strip():
        raise PersistedInferencePayloadError("artifact string fields must be non-empty strings")
    return value.strip()


def _json_value(value: Any) -> Any:
    """Convert common persisted values and fail closed for non-JSON data."""

    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise PersistedInferencePayloadError(
        f"persisted inference payload contains non-JSON value: {type(value).__name__}"
    )


def _assert_json_safe(value: Any) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PersistedInferencePayloadError("prompt record is not strict JSON") from error
