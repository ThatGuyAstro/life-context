from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.context_server.application import create_runtime_app
from services.context_server.config import Settings


@contextmanager
def runtime_client(
    *,
    base_url: str = "http://testserver",
    **settings_overrides: Any,
) -> Iterator[tuple[TestClient, FastAPI]]:
    """Build a fully wired runtime app against an isolated temporary archive."""

    with TemporaryDirectory() as data_dir:
        settings = Settings(data_dir=Path(data_dir), **settings_overrides)
        app = create_runtime_app(settings)
        with TestClient(app, base_url=base_url) as client:
            yield client, app


def event_payload(
    event_id: str,
    text: str,
    *,
    occurred_at: str = "2026-01-01T12:00:00Z",
    source_id: str = "editor",
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source_id": source_id,
        "source_kind": "application",
        "event_type": "observed",
        "occurred_at": occurred_at,
        "payload": {"text": text, "project": "life-context"},
        "artifacts": artifacts or [],
        "metadata": {"name": "integration-test-editor"},
    }


def envelope_payload(
    envelope_id: str,
    events: list[dict[str, Any]],
    *,
    captured_at: str = "2026-01-01T12:01:00Z",
) -> dict[str, Any]:
    return {
        "envelope_id": envelope_id,
        "schema_version": "1",
        "captured_at": captured_at,
        "source_kind": "application",
        "producer": "integration-test",
        "events": events,
        "metadata": {"test": True},
    }


def post_envelope(
    client: TestClient,
    envelope: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> Any:
    headers = {}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post("/v1/ingest/batches", json=envelope, headers=headers)


def test_runtime_app_exposes_v1_health_capabilities_and_status() -> None:
    with runtime_client() as (client, _):
        health = client.get("/v1/health")
        capabilities = client.get("/v1/capabilities")
        status = client.get("/v1/status")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["ready"] is True

    assert capabilities.status_code == 200
    assert "capture-envelopes" in capabilities.json()["capabilities"]
    assert "hybrid-query" in capabilities.json()["capabilities"]

    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["status"] == "running"
    assert status_payload["ready"] is True
    assert status_payload["capture_count"] == 0
    assert status_payload["event_count"] == 0
    assert status_payload["queue"]["pending"] == 0


def test_runtime_status_readiness_tracks_service_lifecycle_not_capture_pause() -> None:
    with TemporaryDirectory() as data_dir:
        app = create_runtime_app(Settings(data_dir=Path(data_dir), docs_enabled=False))
        runtime = app.state.context_runtime

        assert runtime.status()["ready"] is False

        with TestClient(app) as client:
            assert client.get("/v1/status").json()["ready"] is True

            paused = client.post("/v1/pause")
            assert paused.status_code == 200
            assert paused.json()["status"] == "paused"
            assert paused.json()["ready"] is True


def test_artifact_put_and_get_are_content_addressed() -> None:
    body = b"life-context integration artifact"
    digest = sha256(body).hexdigest()

    with runtime_client() as (client, _):
        put = client.put(
            f"/v1/artifacts/{digest}",
            content=body,
            headers={
                "Content-Type": "text/plain",
                "X-Artifact-Sha256": digest,
            },
        )
        fetched = client.get(f"/v1/artifacts/{digest}")

    assert put.status_code == 201
    assert put.json()["artifact_id"] == digest
    assert put.json()["sha256"] == digest
    assert put.json()["uri"] == f"/v1/artifacts/{digest}"

    assert fetched.status_code == 200
    assert fetched.content == body
    assert fetched.headers["content-type"].startswith("text/plain")


def test_rest_observation_and_query_expose_derived_artifact_evidence() -> None:
    body = b"derived inference REST evidence"
    digest = sha256(body).hexdigest()
    expected_url = f"/v1/artifacts/{digest}"

    with runtime_client() as (client, app):
        runtime = app.state.context_runtime
        runtime.put_artifact(digest, body, media_type="image/png")
        source = runtime.storage.create_source(
            "inference",
            source_id="source:rest-derived-inference",
            external_id="source:rest-derived-inference",
        )
        record = runtime.storage.create_observation(
            source_id=source.id,
            kind="inference_observation",
            observed_at="2026-01-01T12:00:00Z",
            text_content="derived inference REST evidence marker",
            payload={"summary": "derived inference REST evidence marker"},
            metadata={"validation_status": "valid"},
            observation_id="inference:rest-derived-evidence",
            idempotency_key="inference-observation:rest-derived-evidence",
            artifact_digest=digest,
        )
        jobs = runtime.storage.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]

        observations = client.get("/v1/observations", params={"limit": 10})
        query = client.post(
            "/v1/query",
            json={
                "query": "derived inference REST evidence marker",
                "mode": "fts",
                "include_evidence": True,
            },
        )

    assert jobs == 0
    assert observations.status_code == 200
    item = next(item for item in observations.json()["items"] if item["id"] == record.id)
    assert item["metadata"]["validation_status"] == "valid"
    assert item["metadata"]["artifact_digest"] == digest
    assert item["metadata"]["artifact_url"] == expected_url

    assert query.status_code == 200
    payload = query.json()
    result = next(item for item in payload["results"] if item["id"] == record.id)
    evidence = next(item for item in payload["evidence"] if item["id"] == record.id)
    assert result["metadata"]["artifact_digest"] == digest
    assert result["metadata"]["artifact_url"] == expected_url
    assert evidence["metadata"]["artifact_digest"] == digest
    assert evidence["metadata"]["artifact_url"] == expected_url


def test_capture_envelope_replay_is_idempotent() -> None:
    envelope = envelope_payload(
        "capture-idempotency",
        [
            event_payload("event-a", "opened the Life Context project"),
            event_payload("event-b", "reviewed the capture integration tests"),
        ],
    )

    with runtime_client() as (client, _):
        first = post_envelope(client, envelope, idempotency_key="batch-idempotency")
        first_status = client.get("/v1/status").json()
        second = post_envelope(client, envelope, idempotency_key="batch-idempotency")
        second_status = client.get("/v1/status").json()

    assert first.status_code == 202
    assert second.status_code == 202
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["accepted"] == 2
    assert second_payload["accepted"] == 2
    assert first_payload["captures"] == ["capture-idempotency"]
    assert second_payload["captures"] == first_payload["captures"]

    assert first_status["capture_count"] == 1
    assert first_status["event_count"] == 2
    assert first_status["queue"]["pending"] == 2
    assert second_status["capture_count"] == 1
    assert second_status["event_count"] == 2
    assert second_status["queue"]["pending"] == 2


def test_query_returns_ranked_results_and_clickable_artifact_evidence() -> None:
    artifact_body = b"screenshot evidence for the Gemma coding session"
    digest = sha256(artifact_body).hexdigest()
    artifact_ref = {
        "artifact_id": digest,
        "kind": "image",
        "uri": f"/v1/artifacts/{digest}",
        "media_type": "image/png",
        "size_bytes": len(artifact_body),
        "sha256": digest,
    }
    envelope = envelope_payload(
        "capture-query",
        [
            event_payload(
                "event-query-1",
                "Gemma coding task: implement the query endpoint",
                artifacts=[artifact_ref],
            ),
            event_payload(
                "event-query-2",
                "Gemma coding task: verify ranked evidence results",
                occurred_at="2026-01-01T11:59:00Z",
            ),
        ],
    )

    with runtime_client() as (client, _):
        artifact = client.put(
            f"/v1/artifacts/{digest}",
            content=artifact_body,
            headers={"Content-Type": "image/png", "X-Artifact-Sha256": digest},
        )
        ingested = post_envelope(client, envelope)
        query = client.post(
            "/v1/query",
            json={"query": "Gemma coding task", "mode": "fts", "limit": 2, "include_evidence": True},
        )

    assert artifact.status_code == 201
    assert ingested.status_code == 202
    assert query.status_code == 200

    payload = query.json()
    assert payload["meta"]["sources"] == ["fts"]
    assert payload["meta"]["result_count"] == 2
    assert [item["rank"] for item in payload["results"]] == [1, 2]
    assert payload["results"][0]["combined_score"] >= payload["results"][1]["combined_score"]
    assert [item["id"] for item in payload["evidence"]] == [
        item["id"] for item in payload["results"]
    ]
    assert payload["answer"]["evidence_ids"] == [item["id"] for item in payload["evidence"]]
    assert payload["results"][0]["metadata"]["artifact_url"] == f"/v1/artifacts/{digest}"
    assert "Gemma coding task" in payload["answer"]["text"]


def test_timeline_returns_filtered_source_events_in_timestamp_order() -> None:
    envelope = envelope_payload(
        "capture-timeline",
        [
            event_payload(
                "event-timeline-old",
                "older editor activity",
                occurred_at="2026-01-01T11:00:00Z",
            ),
            event_payload(
                "event-timeline-new",
                "newer editor activity",
                occurred_at="2026-01-01T12:00:00Z",
            ),
        ],
    )

    with runtime_client() as (client, _):
        response = post_envelope(client, envelope)
        timeline = client.get("/v1/timeline", params={"source_id": "editor", "limit": 10})

    assert response.status_code == 202
    assert timeline.status_code == 200
    payload = timeline.json()
    assert payload["total_count"] == 2
    assert [item["id"] for item in payload["items"]] == [
        "observation:event-timeline-new",
        "observation:event-timeline-old",
    ]
    assert [item["text"] for item in payload["items"]] == [
        "newer editor activity",
        "older editor activity",
    ]


def test_purge_before_removes_old_observations_from_timeline() -> None:
    envelope = envelope_payload(
        "capture-purge",
        [
            event_payload(
                "event-purge-old",
                "old activity eligible for purge",
                occurred_at="2020-01-01T00:00:00Z",
            )
        ],
        captured_at="2020-01-01T00:01:00Z",
    )

    with runtime_client() as (client, _):
        ingested = post_envelope(client, envelope)
        purge = client.post("/v1/purge", json={"before": "2021-01-01T00:00:00Z"})
        timeline = client.get("/v1/timeline")

    assert ingested.status_code == 202
    assert purge.status_code == 200
    assert purge.json()["eligible"] == 1
    assert purge.json()["deleted"] == 1
    assert timeline.status_code == 200
    assert timeline.json()["total_count"] == 0


def test_bearer_auth_and_remote_https_policy() -> None:
    digest = "0" * 64

    with runtime_client(auth_token="local-secret") as (client, _):
        missing_bearer = client.get(f"/v1/artifacts/{digest}")
        authorized_missing_artifact = client.get(
            f"/v1/artifacts/{digest}",
            headers={"Authorization": "Bearer local-secret"},
        )

    assert missing_bearer.status_code == 401
    assert authorized_missing_artifact.status_code == 404

    with runtime_client(
        base_url="https://remote.example.test",
        host="remote.example.test",
        auth_token="remote-secret",
        require_https_remote=True,
    ) as (client, _):
        insecure_remote = client.get(
            f"http://remote.example.test/v1/artifacts/{digest}",
            headers={"Authorization": "Bearer remote-secret"},
        )
        missing_remote_bearer = client.get(f"/v1/artifacts/{digest}")
        authorized_remote_missing_artifact = client.get(
            f"/v1/artifacts/{digest}",
            headers={"Authorization": "Bearer remote-secret"},
        )

    assert insecure_remote.status_code == 400
    assert "HTTPS" in insecure_remote.json()["detail"]
    assert missing_remote_bearer.status_code == 401
    assert authorized_remote_missing_artifact.status_code == 404
