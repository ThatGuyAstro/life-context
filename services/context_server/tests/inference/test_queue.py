from __future__ import annotations

from dataclasses import dataclass

from services.context_server.inference import (
    DeterministicFakeModelClient,
    InferenceJob,
    InferenceWorker,
    InMemoryJobAdapter,
    JobStatus,
    JsonSchemaContract,
    ModelHTTPError,
    ModelTransportError,
    RetryPolicy,
    build_strict_json_prompt,
)


@dataclass
class FakeClock:
    value: float = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _prompt(*, capture_id: str | None = None):
    contract = JsonSchemaContract(
        name="result",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )
    return build_strict_json_prompt(
        "Produce a result.",
        contract,
        context={"capture_id": capture_id} if capture_id else None,
    )


def test_worker_completes_job_and_persists_provenance_and_backlog_metrics() -> None:
    clock = FakeClock()
    adapter = InMemoryJobAdapter()
    model = DeterministicFakeModelClient([{"value": 7}], model_name="fake-local", clock=clock)
    adapter.enqueue(InferenceJob("job-1", _prompt(), model="fake", created_at=clock()))
    worker = InferenceWorker(
        adapter,
        {"fake": model},
        clock=clock,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=10, max_delay_s=10),
    )

    report = worker.run_once()
    snapshot = adapter.get("job-1")

    assert report.succeeded == 1
    assert snapshot.status is JobStatus.SUCCEEDED
    assert snapshot.result is not None
    assert snapshot.result.value == {"value": 7}
    assert snapshot.result.provenance.model.mode == "managed"
    assert snapshot.result.provenance.model.configured_model == "fake-local"
    assert snapshot.result.provenance.prompt.schema_hash == snapshot.job.prompt.schema_hash
    assert snapshot.result.provenance.prompt.prompt_hash == snapshot.job.prompt.prompt_hash
    assert worker.metrics().backlog_count == 0
    assert model.calls[0].request_id == "job-1"


def test_inference_job_payload_round_trip_is_storage_safe() -> None:
    job = InferenceJob("payload-job", _prompt(), model="fake", created_at=12.0)

    restored = InferenceJob.from_payload(
        job.id,
        job.to_payload(),
        created_at=job.created_at,
        max_attempts=3,
    )

    assert restored.prompt.to_dict() == job.prompt.to_dict()
    assert restored.model == "fake"
    assert restored.max_attempts == 3


def test_worker_retries_contract_failure_then_succeeds_when_backoff_is_due() -> None:
    clock = FakeClock()
    adapter = InMemoryJobAdapter()
    model = DeterministicFakeModelClient(["not json", {"value": 9}], model_name="fake", clock=clock)
    adapter.enqueue(InferenceJob("job-2", _prompt(), created_at=clock()))
    worker = InferenceWorker(
        adapter,
        {"fake": model},
        clock=clock,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=10, max_delay_s=10),
    )

    first = worker.run_once()
    assert first.retried == 1
    assert adapter.status("job-2") is JobStatus.QUEUED
    assert worker.metrics().retrying == 1

    clock.advance(10)
    second = worker.run_once()
    assert second.succeeded == 1
    assert adapter.status("job-2") is JobStatus.SUCCEEDED
    assert len(model.calls) == 2


def test_worker_dead_letters_after_retry_budget_and_reports_staleness() -> None:
    clock = FakeClock(100.0)
    adapter = InMemoryJobAdapter()
    model = DeterministicFakeModelClient(["bad", "still bad"], model_name="fake", clock=clock)
    adapter.enqueue(InferenceJob("job-3", _prompt(), created_at=0.0, stale_after_s=5))
    worker = InferenceWorker(
        adapter,
        {"fake": model},
        clock=clock,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=1, max_delay_s=1),
    )

    metrics_before = worker.metrics()
    assert metrics_before.stale_count == 1
    assert metrics_before.oldest_pending_age_s == 100

    assert worker.run_once().retried == 1
    clock.advance(1)
    final = worker.run_once()

    assert final.dead_lettered == 1
    assert adapter.status("job-3") is JobStatus.DEAD_LETTER
    assert adapter.get("job-3").failure is not None
    assert adapter.get("job-3").failure.code == "json_extraction"
    assert worker.metrics().dead_lettered == 1


def test_transient_http_error_uses_provider_retry_after() -> None:
    clock = FakeClock()
    adapter = InMemoryJobAdapter()
    model = DeterministicFakeModelClient(
        [ModelHTTPError(503, "unavailable", retry_after_s=20), {"value": 1}],
        model_name="fake",
        clock=clock,
    )
    adapter.enqueue(InferenceJob("job-4", _prompt(), created_at=clock()))
    worker = InferenceWorker(
        adapter,
        {"fake": model},
        clock=clock,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=2, max_delay_s=2),
    )

    assert worker.run_once().retried == 1
    assert adapter.get("job-4").job.available_at == 1020


def test_unavailable_model_retries_without_dropping_capture_context() -> None:
    clock = FakeClock()
    adapter = InMemoryJobAdapter()
    prompt = _prompt(capture_id="capture-model-unavailable")
    model = DeterministicFakeModelClient(
        [ModelTransportError("connection refused")], model_name="fake"
    )
    adapter.enqueue(
        InferenceJob(
            "job-unavailable",
            prompt,
            model="fake",
            created_at=clock(),
            max_attempts=2,
            metadata={"capture_id": "capture-model-unavailable"},
        )
    )
    worker = InferenceWorker(
        adapter,
        {"fake": model},
        clock=clock,
        retry_policy=RetryPolicy(max_attempts=2, base_delay_s=10, max_delay_s=10),
    )

    report = worker.run_once()
    snapshot = adapter.get("job-unavailable")

    assert report.retried == 1
    assert snapshot.status is JobStatus.QUEUED
    assert snapshot.result is None
    assert snapshot.job.metadata["capture_id"] == "capture-model-unavailable"
    assert "capture-model-unavailable" in snapshot.job.prompt.user_prompt
    assert snapshot.job.last_error["code"] == "model_transport"
