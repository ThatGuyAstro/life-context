"""Focused SourceEvent/CaptureEnvelope wire-contract acceptance tests."""

from __future__ import annotations

import json
from datetime import UTC

import pytest
from contracts import ArtifactRef, CaptureEnvelope, SourceEvent
from pydantic import ValidationError

RAW_CLIPBOARD = "  \t\u03bb\u00a0\U0001f9e0\r\n\tindented line  \ntrailing-space:   "


def _artifact_input() -> dict[str, object]:
    return {
        "id": "artifact-1",
        "kind": "image",
        "uri": "/v1/artifacts/artifact-1",
        "name": "display-1.jpg",
        "media_type": "image/jpeg",
        "size_bytes": 23,
        "sha256": "a" * 64,
        "metadata": {"retention_tier": "raw"},
    }


def _event_input() -> dict[str, object]:
    return {
        "id": "event-1",
        "source_id": "clipboard:local",
        "source_type": "manual",
        "type": "observed",
        "occurred_at": "2026-08-05T12:34:56-04:00",
        "captured_at": "2026-08-05T12:34:56-04:00",
        "payload": {
            "raw_text": RAW_CLIPBOARD,
            "audio": False,
            "video": None,
        },
        "artifacts": [_artifact_input()],
        "metadata": {"audio": False, "video": None},
    }


def test_alias_inputs_emit_only_canonical_source_and_capture_fields() -> None:
    source_event = SourceEvent.model_validate(_event_input())
    envelope = CaptureEnvelope.model_validate(
        {
            "capture_id": "capture-1",
            "captured_at": "2026-08-05T12:34:56-04:00",
            "source_type": "manual",
            "events": [_event_input()],
            "artifacts": [_artifact_input()],
        }
    )

    assert source_event.event_id == "event-1"
    assert envelope.envelope_id == "capture-1"
    assert envelope.events[0].event_id == source_event.event_id

    source_wire = source_event.to_dict()
    envelope_wire = envelope.to_dict()
    assert set(source_wire) == {
        "event_id",
        "source_id",
        "source_kind",
        "event_type",
        "occurred_at",
        "captured_at",
        "payload",
        "artifacts",
        "metadata",
    }
    assert set(envelope_wire) == {
        "envelope_id",
        "schema_version",
        "captured_at",
        "source_kind",
        "events",
        "artifacts",
        "producer",
        "metadata",
    }
    assert {"id", "capture_id", "source_type", "type"}.isdisjoint(source_wire)
    assert {"id", "capture_id", "source_type", "type"}.isdisjoint(envelope_wire)
    assert "artifact_id" in source_wire["artifacts"][0]
    assert "id" not in source_wire["artifacts"][0]


def test_timestamps_and_raw_clipboard_json_values_round_trip_without_normalization() -> None:
    event = SourceEvent.model_validate(_event_input())
    envelope = CaptureEnvelope.model_validate(
        {
            "envelope_id": "capture-1",
            "captured_at": "2026-08-05T12:34:56-04:00",
            "source_kind": "manual",
            "events": [event],
        }
    )

    wire = json.loads(envelope.to_json())
    assert wire["captured_at"] == "2026-08-05T16:34:56Z"
    assert wire["events"][0]["occurred_at"] == "2026-08-05T16:34:56Z"
    assert wire["events"][0]["captured_at"] == "2026-08-05T16:34:56Z"
    assert wire["events"][0]["payload"]["raw_text"] == RAW_CLIPBOARD
    assert wire["events"][0]["payload"]["raw_text"].encode("utf-8") == RAW_CLIPBOARD.encode("utf-8")

    restored = CaptureEnvelope.from_json(envelope.to_json())
    assert restored.captured_at == envelope.captured_at
    assert restored.events[0].occurred_at.tzinfo == UTC
    assert restored.events[0].payload["raw_text"] == RAW_CLIPBOARD
    assert restored.events[0].payload["audio"] is False
    assert restored.events[0].payload["video"] is None


def test_artifact_refs_and_explicit_no_media_capabilities_are_contract_values() -> None:
    artifact = ArtifactRef.model_validate(_artifact_input())
    event = SourceEvent.model_validate(_event_input())
    envelope = CaptureEnvelope(
        envelope_id="capture-1",
        captured_at="2026-08-05T16:34:56Z",
        source_kind="application",
        events=[event],
        artifacts=[artifact],
        metadata={"audio": False, "video": None},
    )

    restored = CaptureEnvelope.from_json(envelope.to_json())
    restored_artifact = restored.artifacts[0]
    assert restored_artifact.artifact_id == "artifact-1"
    assert restored_artifact.kind.value == "image"
    assert restored_artifact.media_type == "image/jpeg"
    assert restored_artifact.size_bytes == 23
    assert restored_artifact.uri == "/v1/artifacts/artifact-1"
    assert restored.metadata["audio"] is False
    assert restored.metadata["video"] is None
    assert restored.events[0].metadata["audio"] is False
    assert restored.events[0].metadata["video"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"unexpected": "field"},
        {"occurred_at": "2026-08-05T12:34:56"},
        {"source_kind": "clipboard"},
        {"payload": {"raw_text": object()}},
    ],
)
def test_source_event_rejects_malformed_source_payloads(
    change: dict[str, object],
) -> None:
    malformed = _event_input()
    malformed.update(change)

    with pytest.raises(ValidationError):
        SourceEvent.model_validate(malformed)


def test_capture_envelope_rejects_malformed_structure() -> None:
    valid = {
        "envelope_id": "capture-1",
        "captured_at": "2026-08-05T16:34:56Z",
        "source_kind": "manual",
        "events": [_event_input()],
    }

    with pytest.raises(ValidationError):
        CaptureEnvelope.model_validate({**valid, "events": []})
    with pytest.raises(ValidationError):
        CaptureEnvelope.model_validate({**valid, "unexpected": True})
