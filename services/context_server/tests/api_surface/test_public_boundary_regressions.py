from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from contracts import ArtifactRef
from fastapi.testclient import TestClient

from services.context_server.api.schemas import LegacyHealthResponse, LegacyQueryResponse
from services.context_server.application import create_runtime_app
from services.context_server.config import Settings


def capture_envelope() -> dict[str, Any]:
    return {
        "envelope_id": "boundary-envelope",
        "schema_version": "1",
        "captured_at": "2026-01-01T12:01:00Z",
        "source_kind": "application",
        "producer": "boundary-regression",
        "events": [
            {
                "event_id": "boundary-event",
                "source_id": "boundary-source",
                "source_kind": "application",
                "event_type": "observed",
                "occurred_at": "2026-01-01T12:00:00Z",
                "payload": {"text": "boundary regression"},
                "artifacts": [],
                "metadata": {},
            }
        ],
        "artifacts": [],
        "metadata": {},
    }


@pytest.fixture
def client(tmp_path: Path):
    app = create_runtime_app(Settings(data_dir=tmp_path, docs_enabled=True))
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "payload",
    (
        {**capture_envelope(), "events": []},
        {
            **capture_envelope(),
            "events": [{**capture_envelope()["events"][0], "occurred_at": "2026-01-01T12:00:00"}],
        },
        {**capture_envelope(), "unexpected": True},
    ),
    ids=("empty-events", "naive-event-timestamp", "unknown-field"),
)
def test_malformed_capture_envelopes_are_rejected(
    client: TestClient, payload: dict[str, Any]
) -> None:
    response = client.post("/v1/ingest/batches", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    (
        {"query": "boundary", "limit": 0},
        {
            "query": "boundary",
            "observed_after": "2026-01-02T00:00:00Z",
            "observed_before": "2026-01-01T00:00:00Z",
        },
        {"query": "boundary", "unexpected": True},
    ),
    ids=("limit-too-small", "reverse-time-window", "unknown-field"),
)
def test_malformed_query_requests_are_rejected(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.post("/v1/query", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"event_id": "boundary-event", "source_id": "boundary-source", "unexpected": True},
        {
            "observation_id": "boundary-observation",
            "subject": "",
            "predicate": "status",
            "value": "ready",
            "observed_at": "2026-01-01T12:00:00",
            "source_event_id": "boundary-event",
        },
    ),
    ids=("missing-content", "unknown-field", "invalid-canonical-fields"),
)
def test_malformed_observations_are_rejected(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.post("/v1/observations", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]


def test_artifact_digest_header_mismatch_is_rejected_before_storage(
    client: TestClient,
) -> None:
    body = b"artifact digest boundary regression"
    digest = sha256(body).hexdigest()
    response = client.put(
        f"/v1/artifacts/{digest}",
        content=body,
        headers={"X-Artifact-Sha256": "0" * 64},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "artifact digest header does not match URL"}


def test_legacy_ingest_batch_payload_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/v1/ingest/batch",
        json={
            "records": [
                {
                    "event_id": "legacy-event",
                    "source_id": "legacy-source",
                    "source_kind": "application",
                    "event_type": "observed",
                    "occurred_at": "2026-01-01T12:00:00Z",
                    "payload": {"text": "legacy ingest"},
                }
            ]
        },
    )

    assert response.status_code == 202
    assert response.json()["accepted"] == 1
    assert response.json()["records"] == 1


def test_legacy_query_payload_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/v1/query",
        json={
            "text": "legacy query",
            "mode": "fts",
            "limit": 3,
            "filters": {"source_id": "legacy-source"},
            "fts_weight": 1.0,
            "semantic_weight": 0.0,
            "evidence_limit": 1,
            "explain": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["query"] == "legacy query"


def test_legacy_observation_payload_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/v1/observations",
        json={
            "event_id": "legacy-observation",
            "source_id": "legacy-source",
            "text": "legacy observation",
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == "observation:legacy-observation"


def test_health_response_serializes_through_its_public_model(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    # Preserve the legacy wire shape: HealthSnapshot is published as a
    # compatible OpenAPI branch, but is not emitted by this endpoint yet.
    validated = LegacyHealthResponse.model_validate(body)
    assert validated.model_dump(mode="json") == body
    assert body == {
        "status": "ok",
        "service": "context-server",
        "version": "0.1.0",
        "ready": True,
    }


def test_query_response_serializes_through_its_public_model(client: TestClient) -> None:
    response = client.post("/v1/query", json={"query": "no matching boundary record", "limit": 1})

    assert response.status_code == 200
    body = response.json()
    validated = LegacyQueryResponse.model_validate(body)
    assert validated.query == "no matching boundary record"
    assert set(("answer", "evidence", "results", "meta")) <= body.keys()


def test_artifact_write_and_read_responses_serialize_at_the_boundary(
    client: TestClient,
) -> None:
    body = b"artifact serialization boundary regression"
    digest = sha256(body).hexdigest()
    put_response = client.put(
        f"/v1/artifacts/{digest}",
        content=body,
        headers={
            "Content-Type": "text/plain",
            "X-Artifact-Sha256": digest,
        },
    )

    assert put_response.status_code == 201
    artifact = ArtifactRef.model_validate(put_response.json())
    assert artifact.artifact_id == digest
    assert artifact.sha256 == digest
    assert artifact.kind.value == "text"
    assert artifact.size_bytes == len(body)

    get_response = client.get(f"/v1/artifacts/{digest}")
    assert get_response.status_code == 200
    assert get_response.content == body
    assert get_response.headers["content-type"].startswith("text/plain")
