from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from contracts import (
    CONTRACT_MODELS,
    ArtifactKind,
    ArtifactRef,
    BacklogStatus,
    CaptureEnvelope,
    HealthSnapshot,
    HealthState,
    ObservationKind,
    ObservationStatus,
    ObservationV1,
    QueryRequest,
    QueryResponse,
    SourceEvent,
    SourceEventType,
    SourceKind,
    export_json_schemas,
    model_json_schema,
)
from pydantic import ValidationError

EXPECTED_REQUIRED_FIELDS = {
    "ArtifactRef": {"artifact_id", "kind"},
    "SourceEvent": {
        "event_id",
        "source_id",
        "source_kind",
        "event_type",
        "occurred_at",
    },
    "CaptureEnvelope": {"envelope_id", "captured_at", "source_kind", "events"},
    "ObservationV1": {
        "observation_id",
        "subject",
        "predicate",
        "value",
        "observed_at",
        "source_event_id",
    },
    "QueryRequest": set(),
    "QueryResponse": set(),
    "BacklogStatus": {
        "pending_count",
        "processing_count",
        "completed_count",
        "failed_count",
        "updated_at",
    },
    "HealthSnapshot": {"service", "status", "checked_at"},
}

EXPECTED_ENUM_VALUES = {
    "ArtifactKind": [
        "text",
        "image",
        "audio",
        "video",
        "document",
        "archive",
        "structured",
        "other",
    ],
    "SourceKind": [
        "file",
        "email",
        "calendar",
        "message",
        "note",
        "web",
        "application",
        "sensor",
        "manual",
        "other",
    ],
    "SourceEventType": ["created", "updated", "deleted", "observed"],
    "ObservationKind": [
        "fact",
        "measurement",
        "classification",
        "relation",
        "summary",
    ],
    "ObservationStatus": ["candidate", "confirmed", "retracted"],
    "QuerySortOrder": ["ascending", "descending"],
    "HealthState": ["healthy", "degraded", "unhealthy", "unknown"],
}

TIMESTAMP_FIELDS = {
    "ArtifactRef": set(),
    "SourceEvent": {"occurred_at", "captured_at"},
    "CaptureEnvelope": {"captured_at"},
    "ObservationV1": {"observed_at"},
    "QueryRequest": {"observed_after", "observed_before"},
    "QueryResponse": set(),
    "BacklogStatus": {"oldest_pending_at", "updated_at"},
    "HealthSnapshot": {"checked_at"},
}


def timestamp(hour: int = 12) -> datetime:
    return datetime(2026, 8, 5, hour, 0, tzinfo=UTC)


def artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="artifact-1",
        kind=ArtifactKind.TEXT,
        uri="memory://artifact-1",
        media_type="text/plain",
        size_bytes=12,
        sha256="A" * 64,
        metadata={"source": "test"},
    )


def event() -> SourceEvent:
    return SourceEvent(
        event_id="event-1",
        source_id="source-1",
        source_kind=SourceKind.MANUAL,
        event_type=SourceEventType.OBSERVED,
        occurred_at=timestamp(10),
        captured_at=timestamp(11),
        payload={"body": "hello", "nested": [1, True]},
        artifacts=[artifact()],
    )


def observation() -> ObservationV1:
    return ObservationV1(
        observation_id="observation-1",
        subject="person-1",
        predicate="mood",
        value={"label": "good"},
        kind=ObservationKind.CLASSIFICATION,
        status=ObservationStatus.CONFIRMED,
        confidence=0.9,
        observed_at=timestamp(10),
        source_event_id="event-1",
        evidence=[artifact()],
    )


def test_all_contracts_validate_and_round_trip_as_json() -> None:
    envelope = CaptureEnvelope(
        envelope_id="capture-1",
        captured_at=timestamp(11),
        source_kind=SourceKind.MANUAL,
        events=[event()],
        artifacts=[artifact()],
        producer="capture-service",
    )
    request = QueryRequest(
        query="mood",
        kinds=[ObservationKind.CLASSIFICATION],
        observed_after=timestamp(9),
        observed_before=timestamp(12),
        limit=25,
        include_evidence=True,
    )
    response = QueryResponse(
        request_id="request-1",
        observations=[observation()],
        total_count=1,
        next_cursor="cursor-2",
    )
    backlog = BacklogStatus(
        pending_count=2,
        processing_count=1,
        completed_count=10,
        failed_count=0,
        oldest_pending_at=timestamp(8),
        updated_at=timestamp(11),
    )
    health = HealthSnapshot(
        service="query-service",
        status=HealthState.HEALTHY,
        checked_at=timestamp(11),
        version="0.1.0",
        components={"database": HealthState.HEALTHY},
        backlog=backlog,
        details={"region": "local"},
    )

    for model in (envelope, event(), observation(), request, response, backlog, health):
        encoded = model.to_json()
        decoded = type(model).from_json(encoded)
        assert decoded == model
        assert json.loads(encoded) == model.to_dict()

    assert envelope.to_dict()["captured_at"] == "2026-08-05T11:00:00Z"
    assert envelope.to_dict()["events"][0]["event_type"] == "observed"
    assert response.to_dict()["observations"][0]["status"] == "confirmed"


def test_input_aliases_preserve_one_canonical_serialized_shape() -> None:
    source_event = SourceEvent(
        id="event-1",
        source_id="source-1",
        source_type="manual",
        type="observed",
        occurred_at="2026-08-05T10:00:00-04:00",
    )
    response = QueryResponse(items=[observation()], total_count=1)

    assert source_event.event_id == "event-1"
    assert source_event.source_kind is SourceKind.MANUAL
    assert source_event.occurred_at == timestamp(14)
    assert "event_id" in source_event.to_dict()
    assert "id" not in source_event.to_dict()
    assert "observations" in response.to_dict()
    assert "items" not in response.to_dict()


def test_validation_rejects_ambiguous_or_invalid_values() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        SourceEvent(
            event_id="event-1",
            source_id="source-1",
            source_kind=SourceKind.FILE,
            event_type=SourceEventType.CREATED,
            occurred_at=datetime(2026, 8, 5, 12),
        )

    with pytest.raises(ValidationError, match="64-character"):
        ArtifactRef(artifact_id="artifact-1", kind=ArtifactKind.TEXT, sha256="bad")

    with pytest.raises(ValidationError, match="extra"):
        ArtifactRef(artifact_id="artifact-1", kind=ArtifactKind.TEXT, unexpected=True)

    with pytest.raises(ValidationError, match="observed_after"):
        QueryRequest(
            observed_after=timestamp(12),
            observed_before=timestamp(11),
        )

    with pytest.raises(ValidationError, match="confidence"):
        ObservationV1(
            observation_id="observation-1",
            subject="person-1",
            predicate="mood",
            value="good",
            confidence=1.1,
            observed_at=timestamp(),
            source_event_id="event-1",
        )

    with pytest.raises(ValidationError, match="total_count"):
        QueryResponse(observations=[observation()], total_count=0)


def test_json_schema_helpers_export_every_public_model(tmp_path) -> None:
    schema = model_json_schema(CaptureEnvelope)
    assert schema["title"] == "CaptureEnvelope"
    assert schema["type"] == "object"
    assert "events" in schema["properties"]
    assert "SourceKind" in schema["$defs"]

    paths = export_json_schemas(tmp_path / "schemas")
    assert set(paths) == {model.__name__ for model in CONTRACT_MODELS}
    for name, path in paths.items():
        assert path.name == f"{name}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8"))["title"] == name


def test_serialization_schema_matches_shared_contract_surface() -> None:
    schemas = {model.__name__: model_json_schema(model) for model in CONTRACT_MODELS}

    for model_name, expected in EXPECTED_REQUIRED_FIELDS.items():
        assert set(schemas[model_name].get("required", ())) == expected

    for enum_name, expected in EXPECTED_ENUM_VALUES.items():
        matching_schemas = [
            schema for schema in schemas.values() if enum_name in schema.get("$defs", {})
        ]
        assert matching_schemas, f"{enum_name} is not reachable from a public model"
        assert all(schema["$defs"][enum_name]["enum"] == expected for schema in matching_schemas)

    for model_name, fields in TIMESTAMP_FIELDS.items():
        properties = schemas[model_name]["properties"]
        for field_name in fields:
            property_schema = properties[field_name]
            variants = property_schema.get("anyOf", [property_schema])
            assert any(variant.get("format") == "date-time" for variant in variants)

    assert schemas["CaptureEnvelope"]["properties"]["events"]["items"] == {
        "$ref": "#/$defs/SourceEvent"
    }
    assert schemas["CaptureEnvelope"]["properties"]["artifacts"]["items"] == {
        "$ref": "#/$defs/ArtifactRef"
    }
    assert schemas["SourceEvent"]["properties"]["artifacts"]["items"] == {
        "$ref": "#/$defs/ArtifactRef"
    }
    assert schemas["ObservationV1"]["properties"]["evidence"]["items"] == {
        "$ref": "#/$defs/ArtifactRef"
    }
    assert schemas["QueryResponse"]["properties"]["observations"]["items"] == {
        "$ref": "#/$defs/ObservationV1"
    }


def test_json_value_preserves_raw_clipboard_whitespace_at_wire_boundary() -> None:
    raw_clipboard = "  λ\u00a0🧠\r\n\tindented line  \ntrailing-space:   "
    source_event = SourceEvent(
        event_id="clipboard-event",
        source_id="clipboard:local",
        source_kind=SourceKind.MANUAL,
        event_type=SourceEventType.OBSERVED,
        occurred_at="2026-08-05T12:00:00-04:00",
        payload={
            "raw_text": raw_clipboard,
            "nested": [raw_clipboard],
        },
        metadata={"raw_text": raw_clipboard},
    )

    encoded = source_event.to_json()
    restored = SourceEvent.from_json(encoded)
    wire = json.loads(encoded)

    assert restored.payload["raw_text"] == raw_clipboard
    assert restored.payload["nested"][0] == raw_clipboard
    assert restored.metadata["raw_text"] == raw_clipboard
    assert wire["payload"]["raw_text"] == raw_clipboard
    assert wire["payload"]["raw_text"].encode("utf-8") == raw_clipboard.encode("utf-8")


def test_timestamp_normalization_is_stable() -> None:
    event_value = event().model_copy(update={"occurred_at": timestamp() - timedelta(hours=1)})
    assert event_value.occurred_at.tzinfo == UTC
