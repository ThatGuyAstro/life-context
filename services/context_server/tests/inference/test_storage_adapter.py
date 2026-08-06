from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from services.context_server.inference import (
    JsonSchemaContract,
    PersistedInferencePayloadError,
    adapt_persisted_job,
    build_strict_json_prompt,
)


def _payload(*, image: dict[str, object] | None = None) -> dict[str, object]:
    contract = JsonSchemaContract(
        name="screenshot_observation",
        version="1",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "activity": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["activity", "confidence"],
        },
    )
    prompt = build_strict_json_prompt(
        "Describe the visible coding activity and return the requested JSON.",
        contract,
        context={"window": "editor", "captured_at": "2026-08-05T12:00:00Z"},
    )
    payload: dict[str, object] = {
        "model": "gemma4-local",
        "prompt": prompt.to_payload(),
        "metadata": {"capture_id": "capture-1"},
    }
    if image is not None:
        payload["artifacts"] = [image]
    return payload


def test_storage_job_becomes_strict_json_multimodal_record() -> None:
    image = {
        "artifact_id": "contact-sheet-1",
        "kind": "image",
        "media_type": "image/jpeg",
        "uri": "artifact://abc123",
        "sha256": "a" * 64,
        "size_bytes": 128,
    }
    job = {
        "id": "job-1",
        "kind": "screenshot_observation",
        "created_at": datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        "payload": _payload(image=image),
    }

    record = adapt_persisted_job(job)
    value = record.to_dict()
    round_trip = json.loads(record.to_json())

    assert round_trip == value
    assert value["job_id"] == "job-1"
    assert value["job_kind"] == "screenshot_observation"
    assert value["model"] == "gemma4-local"
    assert value["response_format"]["type"] == "json_schema"
    assert value["messages"][0]["role"] == "system"
    user_content = value["messages"][1]["content"]
    assert user_content[0]["type"] == "text"
    assert user_content[1] == {
        "type": "image_url",
        "image_url": {"url": "artifact://abc123", "detail": "auto"},
    }
    assert value["provenance"]["prompt_hash"] == record.provenance["prompt_hash"]
    assert value["provenance"]["schema_hash"] == record.provenance["schema_hash"]
    assert value["provenance"]["artifacts"][0]["sha256"] == "a" * 64
    assert value["provenance"]["job_created_at"].endswith("+00:00")


def test_embedded_image_is_preserved_without_file_or_model_access() -> None:
    encoded = base64.b64encode(b"jpeg-bytes").decode("ascii")
    record = adapt_persisted_job(
        _payload(
            image={
                "artifact_id": "monitor-1",
                "kind": "image",
                "media_type": "image/jpeg",
                "base64": encoded,
            }
        ),
        job_id="job-embedded",
    )

    image_block = record.user_content[1]
    assert image_block["image_url"]["url"] == f"data:image/jpeg;base64,{encoded}"
    assert record.provenance["artifacts"][0]["source"] == "embedded"


def test_non_image_artifacts_are_retained_as_provenance_but_not_sent_as_media() -> None:
    payload = _payload(
        image={
            "artifact_id": "screen-1",
            "kind": "image",
            "uri": "artifact://screen",
            "sha256": "b" * 64,
        }
    )
    payload["artifacts"] = [
        payload["artifacts"][0],
        {
            "artifact_id": "terminal-log",
            "kind": "text",
            "media_type": "text/plain",
            "uri": "artifact://terminal-log",
        },
        {
            "artifact_id": "unsupported-video",
            "kind": "video",
            "media_type": "video/mp4",
            "uri": "artifact://video",
        },
    ]

    record = adapt_persisted_job(payload, job_id="job-mixed")
    image_blocks = [item for item in record.user_content if item["type"] == "image_url"]

    assert len(image_blocks) == 1
    assert record.provenance["artifact_count"] == 3
    assert [item["included_as_image"] for item in record.provenance["artifacts"]] == [
        True,
        False,
        False,
    ]


def test_prompt_hash_tampering_is_rejected_before_model_request() -> None:
    payload = _payload()
    prompt = dict(payload["prompt"])
    prompt["prompt_hash"] = "0" * 64
    payload["prompt"] = prompt

    with pytest.raises(PersistedInferencePayloadError, match="hash mismatch"):
        adapt_persisted_job(payload, job_id="job-tampered")


def test_flattened_payload_and_hash_only_image_use_deterministic_deferred_uri() -> None:
    payload = _payload(
        image={
            "artifact_id": "screen-2",
            "kind": "image",
            "sha256": "c" * 64,
        }
    )
    prompt = payload.pop("prompt")
    flattened = dict(prompt)
    flattened["model"] = payload["model"]
    flattened["images"] = payload["artifacts"]

    record = adapt_persisted_job(flattened, job_id="job-flat")

    assert record.user_content[1]["image_url"]["url"] == f"artifact://{'c' * 64}"
    assert record.provenance["artifacts"][0]["source"] == "deferred"


def test_invalid_embedded_data_and_non_json_values_fail_closed() -> None:
    with pytest.raises(PersistedInferencePayloadError, match="valid base64"):
        adapt_persisted_job(
            _payload(
                image={
                    "artifact_id": "bad-image",
                    "kind": "image",
                    "base64": "not-base64!",
                }
            ),
            job_id="job-bad-image",
        )

    payload = _payload()
    payload["metadata"] = {"bad": float("nan")}
    with pytest.raises(PersistedInferencePayloadError, match="strict JSON"):
        adapt_persisted_job(payload, job_id="job-nan")

