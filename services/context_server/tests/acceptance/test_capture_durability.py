from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Any

from services.context_server.config import Settings
from services.context_server.inference import (
    DeterministicFakeModelClient,
    InferenceWorker,
    ModelHTTPError,
    RetryPolicy,
    SQLitePersistentJobAdapter,
)
from services.context_server.runtime import ContextRuntime


@dataclass
class _Clock:
    value: float

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _artifact_ref(body: bytes) -> dict[str, Any]:
    digest = sha256(body).hexdigest()
    return {
        "artifact_id": digest,
        "kind": "image",
        "uri": f"/v1/artifacts/{digest}",
        "media_type": "image/png",
        "size_bytes": len(body),
        "sha256": digest,
    }


def _capture_envelope(
    envelope_id: str,
    event_id: str,
    marker: str,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    return {
        "envelope_id": envelope_id,
        "schema_version": "1",
        "captured_at": "2026-01-01T12:01:00Z",
        "source_kind": "application",
        "producer": "capture-durability-acceptance",
        "events": [
            {
                "event_id": event_id,
                "source_id": "acceptance-editor",
                "source_kind": "application",
                "event_type": "observed",
                "occurred_at": "2026-01-01T12:00:00Z",
                "payload": {"text": marker, "sequence": event_id},
                "artifacts": [artifact],
                "metadata": {"name": "acceptance-editor"},
            }
        ],
        "metadata": {"acceptance": "capture-never-discarded"},
    }


def test_capture_envelopes_survive_inference_lag_restart_and_failure_transitions(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "context-data", docs_enabled=False)
    seeded: list[tuple[str, str, bytes, dict[str, Any]]] = []
    for index in range(3):
        body = f"durable-artifact-{index}".encode("ascii")
        artifact = _artifact_ref(body)
        seeded.append(
            (
                f"capture-durable-{index}",
                f"event-durable-{index}",
                body,
                artifact,
            )
        )

    runtime = ContextRuntime(settings)
    try:
        for envelope_id, event_id, body, artifact in seeded:
            stored = runtime.put_artifact(
                artifact["sha256"],
                body,
                media_type="image/png",
                retention_tier="raw",
            )
            assert stored["artifact_id"] == artifact["sha256"]

            result = runtime.ingest_batch(
                _capture_envelope(
                    envelope_id,
                    event_id,
                    f"durability marker {event_id}",
                    artifact,
                ),
                idempotency_key=envelope_id,
            )
            assert result["accepted"] == 1
    finally:
        runtime.shutdown()

    # No model endpoint is configured: capture must still commit observations,
    # artifacts, and inference jobs without starting a worker.
    restarted = ContextRuntime(settings)
    try:
        restarted.startup()
        status = restarted.status()
        assert status["model"]["status"] == "not_configured"
        assert status["model"]["active_workers"] == 0
        assert status["queue"]["pending"] == len(seeded)
        assert status["queue"]["oldest_pending_at"] is not None
        assert status["queue"]["oldest_pending_age_seconds"] is not None
        assert status["queue"]["oldest_pending_age_seconds"] >= 0

        timeline = restarted.timeline({"limit": 20})
        source_events = {
            item["metadata"]["event_id"]: item
            for item in timeline["items"]
            if item["kind"] == "source_event:observed"
        }
        assert set(source_events) == {event_id for _, event_id, _, _ in seeded}

        for envelope_id, event_id, body, artifact in seeded:
            capture = restarted.storage.get_capture(envelope_id)
            assert capture is not None
            assert restarted.storage.get_artifact(artifact["sha256"]) is not None
            assert restarted.storage.artifacts.read(artifact["sha256"]) == body

            event = source_events[event_id]
            assert event["text"] == f"durability marker {event_id}"
            assert event["capture_id"] == envelope_id
            assert event["metadata"]["artifact_url"] == f"/v1/artifacts/{artifact['sha256']}"
            queried = restarted.timeline({"query": f"durability marker {event_id}", "limit": 10})
            assert {item["metadata"]["event_id"] for item in queried["items"]} == {event_id}

        job_ids = [
            str(row["id"])
            for row in restarted.storage.connection.execute(
                "SELECT id FROM jobs WHERE kind = 'inference' ORDER BY created_at, id"
            ).fetchall()
        ]
        assert len(job_ids) == len(seeded)

        now = time()
        adapter = SQLitePersistentJobAdapter(
            restarted.storage,
            worker_id="capture-durability-acceptance",
        )
        pending = adapter.backlog_metrics(now=now)
        assert pending.queued == len(seeded)
        assert pending.pending_count == len(seeded)
        assert pending.retrying == 0
        assert pending.oldest_pending_age_s >= 0

        clock = _Clock(now)
        model = DeterministicFakeModelClient(
            [
                ModelHTTPError(503, "inference endpoint unavailable"),
                ModelHTTPError(400, "model returned an invalid response"),
            ],
            model_name="acceptance-failure-model",
            clock=clock,
        )
        worker = InferenceWorker(
            adapter,
            {"default": model},
            default_model="default",
            batch_size=1,
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_s=60, max_delay_s=60),
        )

        first_run = worker.run_once()
        assert first_run.claimed == 1
        assert first_run.retried == 1
        assert first_run.dead_lettered == 0

        retry_metrics = adapter.backlog_metrics(now=now + 1)
        assert retry_metrics.backlog_count == len(seeded)
        assert retry_metrics.queued == 3
        assert retry_metrics.running == 0
        assert retry_metrics.retrying == 1
        assert retry_metrics.failed == 1
        assert retry_metrics.dead_lettered == 0

        clock.advance(1)
        second_run = worker.run_once()
        assert second_run.claimed == 1
        assert second_run.retried == 0
        assert second_run.dead_lettered == 1

        terminal_metrics = adapter.backlog_metrics(now=clock.value + 1)
        assert terminal_metrics.backlog_count == 2
        assert terminal_metrics.queued == 2
        assert terminal_metrics.running == 0
        assert terminal_metrics.pending_count == 1
        assert terminal_metrics.retrying == 1
        assert terminal_metrics.failed == 2
        assert terminal_metrics.dead_lettered == 1

        job_records = [restarted.storage.get_job(job_id) for job_id in job_ids]
        assert all(record is not None for record in job_records)
        assert sum(record.status == "pending" for record in job_records if record is not None) == 2
        assert sum(record.status == "dead" for record in job_records if record is not None) == 1

        final_status = restarted.status()
        assert final_status["queue"]["pending"] == 2
        assert final_status["backlog"]["dead_lettered_count"] == 1
    finally:
        restarted.shutdown()
