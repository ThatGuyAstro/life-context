"""Full-story purge acceptance coverage through the real runtime API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from services.context_server.tests.integration.test_runtime_app import runtime_client


def _put_artifact(
    client: TestClient,
    *,
    label: str,
    source: str,
    project: str,
    retention_tier: str,
) -> tuple[str, bytes]:
    body = f"purge-acceptance-artifact:{label}".encode()
    digest = hashlib.sha256(body).hexdigest()
    response = client.put(
        f"/v1/artifacts/{digest}",
        content=body,
        headers={
            "Content-Type": "text/plain",
            "X-Artifact-Sha256": digest,
            "X-Artifact-Metadata": json.dumps(
                {
                    "source": source,
                    "project": project,
                    "retention_tier": retention_tier,
                    "acceptance_label": label,
                }
            ),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["sha256"] == digest
    return digest, body


def _artifact_ref(digest: str, size_bytes: int) -> dict[str, Any]:
    return {
        "artifact_id": digest,
        "kind": "text",
        "uri": f"/v1/artifacts/{digest}",
        "media_type": "text/plain",
        "size_bytes": size_bytes,
        "sha256": digest,
    }


def _ingest_event(
    client: TestClient,
    *,
    event_id: str,
    marker: str,
    source: str,
    project: str,
    occurred_at: datetime,
    evidence: tuple[str, bytes] | None = None,
) -> None:
    artifact_refs = []
    if evidence is not None:
        digest, body = evidence
        artifact_refs.append(_artifact_ref(digest, len(body)))

    response = client.post(
        "/v1/ingest/batches",
        headers={"Idempotency-Key": f"purge-acceptance:{event_id}"},
        json={
            # This is the records wrapper emitted by the collector transport;
            # the event itself is the same public envelope accepted by the
            # dashboard/runtime integration.
            "records": [
                {
                    "envelope_id": f"capture:{event_id}",
                    "schema_version": "1",
                    "captured_at": occurred_at.isoformat(),
                    "source_kind": "application",
                    "producer": "purge-acceptance",
                    "events": [
                        {
                            "event_id": event_id,
                            "source_id": source,
                            "source_kind": "application",
                            "event_type": "observed",
                            "occurred_at": occurred_at.isoformat(),
                            "payload": {
                                "text": marker,
                                "source": source,
                                "project": project,
                            },
                            "artifacts": artifact_refs,
                            "metadata": {
                                "source": source,
                                "project": project,
                                "acceptance": "purge-full-story",
                            },
                        }
                    ],
                    "metadata": {
                        "source": source,
                        "project": project,
                        "acceptance": "purge-full-story",
                    },
                }
            ]
        },
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["accepted"] == 1


def _assert_purge_counts(
    payload: dict[str, Any],
    *,
    dry_run: bool,
    observations: int,
    artifacts: int,
    deleted_observations: int,
    deleted_artifacts: int,
    skipped_referenced_artifacts: int,
) -> None:
    assert payload["dry_run"] is dry_run
    assert payload["eligible"] == observations
    assert payload["eligible_observations"] == observations
    assert payload["expired_observations"] == observations
    assert payload["deleted_observations"] == deleted_observations
    assert payload["deleted"] == deleted_observations
    assert payload["eligible_artifacts"] == artifacts
    assert payload["expired_artifacts"] == artifacts
    assert payload["deleted_artifacts"] == deleted_artifacts
    assert payload["skipped_referenced_artifacts"] == skipped_referenced_artifacts
    assert payload["missing_artifact_files"] == 0
    assert payload["file_errors"] == []


def _assert_artifact(client: TestClient, digest: str, body: bytes, *, status: int) -> None:
    response = client.get(f"/v1/artifacts/{digest}")
    assert response.status_code == status
    if status == 200:
        assert response.content == body


def _query_evidence(client: TestClient, marker: str, digest: str, body: bytes) -> str:
    response = client.post(
        "/v1/query",
        json={
            "query": marker,
            "mode": "fts",
            "limit": 5,
            "include_evidence": True,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["results"]) == 1
    assert payload["answer"]["evidence_ids"] == [payload["results"][0]["id"]]
    assert len(payload["evidence"]) == 1
    evidence_url = payload["evidence"][0]["metadata"]["artifact_url"]
    assert evidence_url == f"/v1/artifacts/{digest}"
    _assert_artifact(client, digest, body, status=200)
    return evidence_url


def test_full_story_purge_uses_runtime_api_and_preserves_scoped_evidence() -> None:
    """Exercise dashboard/CLI purge payloads against a complete API-backed archive."""

    anchor = datetime.now(UTC)
    purge_before = anchor + timedelta(minutes=5)
    old_event_at = anchor - timedelta(days=2)
    future_event_at = anchor + timedelta(days=2)

    with runtime_client(keyframe_retention_days=0) as (client, _):
        date_referenced = _put_artifact(
            client,
            label="date-referenced-raw",
            source="clipboard",
            project="date-project",
            retention_tier="raw",
        )
        date_unreferenced = _put_artifact(
            client,
            label="date-unreferenced-raw",
            source="clipboard",
            project="date-project",
            retention_tier="raw",
        )
        date_keyframe = _put_artifact(
            client,
            label="date-keyframe-must-remain",
            source="clipboard",
            project="date-project",
            retention_tier="keyframe",
        )
        date_derived = _put_artifact(
            client,
            label="date-derived-must-remain",
            source="clipboard",
            project="date-project",
            retention_tier="derived",
        )
        _ingest_event(
            client,
            event_id="date-event",
            marker="purge date scoped marker",
            source="clipboard",
            project="date-project",
            occurred_at=old_event_at,
            evidence=date_referenced,
        )

        source_referenced = _put_artifact(
            client,
            label="source-referenced-raw",
            source="window",
            project="source-project",
            retention_tier="raw",
        )
        source_unreferenced = _put_artifact(
            client,
            label="source-unreferenced-raw",
            source="window",
            project="source-project",
            retention_tier="raw",
        )
        _ingest_event(
            client,
            event_id="source-event",
            marker="purge source scoped marker",
            source="window",
            project="source-project",
            occurred_at=old_event_at,
            evidence=source_referenced,
        )

        project_referenced = _put_artifact(
            client,
            label="project-referenced-raw",
            source="calendar",
            project="project-target",
            retention_tier="raw",
        )
        project_unreferenced = _put_artifact(
            client,
            label="project-unreferenced-raw",
            source="calendar",
            project="project-target",
            retention_tier="raw",
        )
        _ingest_event(
            client,
            event_id="project-event",
            marker="purge project scoped marker",
            source="calendar",
            project="project-target",
            occurred_at=old_event_at,
            evidence=project_referenced,
        )

        retention_referenced = _put_artifact(
            client,
            label="retention-referenced-keyframe",
            source="documents",
            project="retention-project",
            retention_tier="keyframe",
        )
        retention_unreferenced = _put_artifact(
            client,
            label="retention-unreferenced-keyframe",
            source="documents",
            project="retention-project",
            retention_tier="keyframe",
        )
        retention_derived = _put_artifact(
            client,
            label="retention-derived-must-remain",
            source="documents",
            project="retention-project",
            retention_tier="derived",
        )
        retention_marker = "purge retention evidence marker"
        _ingest_event(
            client,
            event_id="retention-event",
            marker=retention_marker,
            source="documents",
            project="retention-project",
            occurred_at=future_event_at,
            evidence=retention_referenced,
        )

        # Dashboard-shaped request: date plus both optional scope fields and
        # the UI's confirmation field.
        date_response = client.post(
            "/v1/purge",
            json={
                "before": purge_before.isoformat(),
                "source": "clipboard",
                "project": "date-project",
                "confirmed": True,
            },
        )
        assert date_response.status_code == 200, date_response.text
        date_payload = date_response.json()
        _assert_purge_counts(
            date_payload,
            dry_run=False,
            observations=1,
            artifacts=2,
            deleted_observations=1,
            deleted_artifacts=1,
            skipped_referenced_artifacts=1,
        )
        assert date_payload["before"] == purge_before.isoformat()
        assert date_payload["source"] == "clipboard"
        assert date_payload["project"] == "date-project"
        _assert_artifact(client, date_referenced[0], date_referenced[1], status=200)
        _assert_artifact(client, date_unreferenced[0], date_unreferenced[1], status=404)
        _assert_artifact(client, date_keyframe[0], date_keyframe[1], status=200)
        _assert_artifact(client, date_derived[0], date_derived[1], status=200)

        deleted_date_query = client.post(
            "/v1/query",
            json={"query": "purge date scoped marker", "mode": "fts", "limit": 5},
        )
        assert deleted_date_query.status_code == 200
        assert deleted_date_query.json()["results"] == []

        # CLI-shaped dry run: --all is false when --before is supplied, and
        # confirmation is false because this request must not mutate state.
        source_dry_run_response = client.post(
            "/v1/purge",
            json={
                "before": purge_before.isoformat(),
                "all": False,
                "confirmed": False,
                "dry_run": True,
                "source": "window",
            },
        )
        assert source_dry_run_response.status_code == 200, source_dry_run_response.text
        source_dry_run = source_dry_run_response.json()
        _assert_purge_counts(
            source_dry_run,
            dry_run=True,
            observations=1,
            artifacts=2,
            deleted_observations=0,
            deleted_artifacts=0,
            skipped_referenced_artifacts=1,
        )
        _assert_artifact(client, source_referenced[0], source_referenced[1], status=200)
        _assert_artifact(client, source_unreferenced[0], source_unreferenced[1], status=200)

        source_response = client.post(
            "/v1/purge",
            json={
                "before": purge_before.isoformat(),
                "all": False,
                "confirmed": True,
                "dry_run": False,
                "source": "window",
            },
        )
        assert source_response.status_code == 200, source_response.text
        source_payload = source_response.json()
        _assert_purge_counts(
            source_payload,
            dry_run=False,
            observations=1,
            artifacts=2,
            deleted_observations=1,
            deleted_artifacts=1,
            skipped_referenced_artifacts=1,
        )
        assert source_payload["before"] == purge_before.isoformat()
        assert source_payload["source"] == "window"
        _assert_artifact(client, source_referenced[0], source_referenced[1], status=200)
        _assert_artifact(client, source_unreferenced[0], source_unreferenced[1], status=404)

        # The date alias is the server-side equivalent of before.  Project
        # scope is intentionally independent from the source scope above.
        project_response = client.post(
            "/v1/purge",
            json={
                "date": purge_before.isoformat(),
                "project": "project-target",
                "confirmed": True,
            },
        )
        assert project_response.status_code == 200, project_response.text
        project_payload = project_response.json()
        _assert_purge_counts(
            project_payload,
            dry_run=False,
            observations=1,
            artifacts=2,
            deleted_observations=1,
            deleted_artifacts=1,
            skipped_referenced_artifacts=1,
        )
        assert project_payload["before"] == purge_before.isoformat()
        assert project_payload["date"] == purge_before.isoformat()
        assert project_payload["project"] == "project-target"
        _assert_artifact(client, project_referenced[0], project_referenced[1], status=200)
        _assert_artifact(client, project_unreferenced[0], project_unreferenced[1], status=404)

        # Retention-only purge: the CLI's --all shape supplies no before date,
        # while the explicit tier opts the keyframe artifact into expiration.
        evidence_url_before = _query_evidence(
            client,
            retention_marker,
            retention_referenced[0],
            retention_referenced[1],
        )
        retention_response = client.post(
            "/v1/purge",
            json={
                "before": None,
                "all": True,
                "confirmed": True,
                "dry_run": False,
                "source": "documents",
                "project": "retention-project",
                "retention_tiers": ["keyframe"],
            },
        )
        assert retention_response.status_code == 200, retention_response.text
        retention_payload = retention_response.json()
        _assert_purge_counts(
            retention_payload,
            dry_run=False,
            observations=0,
            artifacts=2,
            deleted_observations=0,
            deleted_artifacts=1,
            skipped_referenced_artifacts=1,
        )
        assert retention_payload["source"] == "documents"
        assert retention_payload["project"] == "retention-project"
        assert retention_payload["retention_tiers"] == ["keyframe"]
        _assert_artifact(client, retention_referenced[0], retention_referenced[1], status=200)
        _assert_artifact(client, retention_unreferenced[0], retention_unreferenced[1], status=404)
        _assert_artifact(client, retention_derived[0], retention_derived[1], status=200)

        # The surviving observation's evidence URL and bytes are unchanged by
        # all four purge modes, proving that scope and reference protection do
        # not break dashboard query links.
        assert (
            _query_evidence(
                client,
                retention_marker,
                retention_referenced[0],
                retention_referenced[1],
            )
            == evidence_url_before
        )

        status = client.get("/v1/status")
        assert status.status_code == 200
        assert status.json()["event_count"] == 1
        assert status.json()["artifact_count"] == 7
