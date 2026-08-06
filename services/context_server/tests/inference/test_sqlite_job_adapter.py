from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from services.context_server.inference import (
    InferenceJobResult,
    InferenceProvenance,
    JobFailure,
    JsonSchemaContract,
    ModelProvenance,
    PromptProvenance,
    SQLiteJobLeaseError,
    SQLitePersistentJobAdapter,
    build_strict_json_prompt,
)
from services.context_server.storage import SQLiteStorage

NOW = 1_800_000_000.0


def _prompt():
    contract = JsonSchemaContract(
        name="screen_observation",
        version="1",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"activity": {"type": "string"}},
            "required": ["activity"],
        },
    )
    return build_strict_json_prompt("Describe the screenshot.", contract)


def _enqueue(
    storage: SQLiteStorage,
    *,
    key: str,
    job_id: str | None = None,
    stale_after_s=300,
    now: float = NOW,
    source_id: str = "screen:display-1",
    capture_kind: str = "monitor",
    kind: str = "inference.screenshot",
):
    prompt = _prompt()
    return storage.enqueue_job(
        kind,
        {
            "model": "fake-local",
            "prompt": prompt.to_payload(),
            "stale_after_s": stale_after_s,
            "source_id": source_id,
            "metadata": {
                "capture_id": f"capture-{job_id or key}",
                "capture_kind": capture_kind,
            },
        },
        idempotency_key=key,
        job_id=job_id,
        now=datetime.fromtimestamp(now, tz=UTC),
    )


def _provenance(claim):
    prompt = claim.job.prompt
    return InferenceProvenance(
        request_id=claim.job.id,
        attempt=claim.attempt,
        prompt=PromptProvenance.from_prompt(prompt),
        model=ModelProvenance(
            client_name="fake-local",
            mode="managed",
            configured_model="fake-local",
            returned_model="fake-local",
            endpoint="fake://model",
            response_id="response-1",
            structured_output_mode="json_schema",
            request_id=claim.job.id,
            usage={"total_tokens": 1},
        ),
        started_at=NOW,
        finished_at=NOW + 0.25,
        latency_ms=250.0,
    )


@pytest.fixture()
def storage():
    value = SQLiteStorage(":memory:", load_sqlite_vec_extension=False)
    try:
        yield value
    finally:
        value.close()


def test_claim_and_complete_persist_result_and_provenance(storage: SQLiteStorage) -> None:
    job = _enqueue(storage, key="job:1", job_id="job-1")
    callbacks: list[tuple[str, str]] = []
    adapter = SQLitePersistentJobAdapter(
        storage,
        worker_id="worker-a",
        lease_seconds=30,
        on_complete=lambda row, result: callbacks.append(
            (row.status, result.provenance.request_id)
        ),
    )

    claims = adapter.claim_available(limit=1, now=NOW)
    assert len(claims) == 1
    claim = claims[0]
    assert claim.job.id == job.id
    assert claim.attempt == 1
    running = storage.get_job(job.id)
    assert running is not None
    assert running.status == "running"
    assert running.locked_by == claim.lease_id

    provenance = _provenance(claim)
    result = InferenceJobResult(
        value={"activity": "coding"},
        raw_text='{"activity":"coding"}',
        provenance=provenance,
    )
    adapter.complete(claim, result, now=NOW + 1)
    completed = storage.get_job(job.id)
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.lease_until is None
    assert completed.locked_by is None
    assert completed.last_error is None
    assert completed.result["value"] == {"activity": "coding"}
    assert completed.result["provenance"]["prompt"]["prompt_hash"] == provenance.prompt_hash
    assert callbacks == [("succeeded", "job-1")]

    # A duplicate delivery is a no-op and cannot duplicate derived persistence.
    adapter.complete(claim, result, now=NOW + 2)
    assert len(callbacks) == 1


def test_idempotent_enqueue_claims_one_row(storage: SQLiteStorage) -> None:
    first = _enqueue(storage, key="same-key", job_id="job-idempotent")
    second = _enqueue(storage, key="same-key", job_id="different-id")
    assert first.id == second.id

    adapter = SQLitePersistentJobAdapter(storage, worker_id="worker")
    claims = adapter.claim_available(limit=3, now=NOW)
    assert [claim.job.id for claim in claims] == [first.id]


def test_claim_compacts_old_pristine_screenshots_and_keeps_newest(
    storage: SQLiteStorage,
) -> None:
    jobs = [
        _enqueue(
            storage,
            key=f"screenshot:{index}",
            job_id=f"screenshot-{index}",
            now=NOW + index,
            kind="inference",
        )
        for index in range(5)
    ]
    adapter = SQLitePersistentJobAdapter(
        storage,
        worker_id="worker",
        max_pending_screenshot_jobs=3,
    )

    claims = adapter.claim_available(limit=1, now=NOW + 10)

    assert [claim.job.id for claim in claims] == ["screenshot-2"]
    assert storage.get_job(jobs[0].id).status == "succeeded"
    assert storage.get_job(jobs[1].id).status == "succeeded"
    assert storage.get_job(jobs[2].id).status == "running"
    assert storage.get_job(jobs[3].id).status == "pending"
    assert storage.get_job(jobs[4].id).status == "pending"
    compacted = storage.get_job(jobs[0].id)
    assert compacted.result["code"] == "backpressure_superseded"
    assert compacted.result["superseded_by"] == "screenshot-4"
    assert compacted.completed_at is not None


def test_compaction_preserves_idempotency_and_non_screenshot_jobs(
    storage: SQLiteStorage,
) -> None:
    old = _enqueue(storage, key="old", job_id="old", now=NOW)
    _enqueue(storage, key="new", job_id="new", now=NOW + 1)
    terminal = _enqueue(
        storage,
        key="terminal",
        job_id="terminal",
        now=NOW - 10,
        source_id="terminal:powershell",
        capture_kind="",
        kind="inference",
    )
    adapter = SQLitePersistentJobAdapter(storage, max_pending_screenshot_jobs=1)

    adapter.claim_available(limit=1, now=NOW + 2)

    duplicate = _enqueue(storage, key="old", job_id="replacement", now=NOW + 3)
    assert duplicate.id == old.id
    assert duplicate.status == "succeeded"
    assert storage.get_job(terminal.id).status == "running"


def test_compaction_keeps_only_newest_pristine_job_per_metadata_source(
    storage: SQLiteStorage,
) -> None:
    old = _enqueue(
        storage,
        key="window-old",
        job_id="window-old",
        now=NOW,
        source_id="window:foreground",
        capture_kind="",
        kind="inference",
    )
    middle = _enqueue(
        storage,
        key="window-middle",
        job_id="window-middle",
        now=NOW + 1,
        source_id="window:foreground",
        capture_kind="",
        kind="inference",
    )
    newest = _enqueue(
        storage,
        key="window-newest",
        job_id="window-newest",
        now=NOW + 2,
        source_id="window:foreground",
        capture_kind="",
        kind="inference",
    )
    adapter = SQLitePersistentJobAdapter(storage, max_pending_source_jobs=1)

    claims = adapter.claim_available(limit=1, now=NOW + 3)

    assert [claim.job.id for claim in claims] == [newest.id]
    for superseded in (old, middle):
        record = storage.get_job(superseded.id)
        assert record.status == "succeeded"
        assert record.result["code"] == "backpressure_source_superseded"
    assert storage.get_job(newest.id).status == "running"


def test_compaction_never_supersedes_retried_screenshot_jobs(
    storage: SQLiteStorage,
) -> None:
    retry_job = _enqueue(storage, key="retry", job_id="retry", now=NOW)
    adapter = SQLitePersistentJobAdapter(storage, max_pending_screenshot_jobs=1)
    retry_claim = adapter.claim_available(limit=1, now=NOW)[0]
    adapter.schedule_retry(
        retry_claim,
        JobFailure("transport", "retry me", True, retry_claim.attempt, NOW + 1),
        next_attempt_at=NOW + 100,
        provenance=None,
        now=NOW + 1,
    )
    _enqueue(storage, key="fresh-1", job_id="fresh-1", now=NOW + 2)
    _enqueue(storage, key="fresh-2", job_id="fresh-2", now=NOW + 3)

    claims = adapter.claim_available(limit=1, now=NOW + 4)

    assert [claim.job.id for claim in claims] == ["fresh-2"]
    retried = storage.get_job(retry_job.id)
    assert retried.status == "pending"
    assert retried.last_error is not None


def test_expired_claim_cannot_overwrite_reclaimed_attempt(storage: SQLiteStorage) -> None:
    job = _enqueue(storage, key="job:lease", job_id="job-lease")
    first_adapter = SQLitePersistentJobAdapter(storage, worker_id="worker-a", lease_seconds=5)
    second_adapter = SQLitePersistentJobAdapter(storage, worker_id="worker-b", lease_seconds=5)

    first = first_adapter.claim_available(limit=1, now=NOW)[0]
    second = second_adapter.claim_available(limit=1, now=NOW + 6)[0]
    assert second.job.id == job.id
    assert second.attempt == 2
    assert second.lease_id != first.lease_id

    result = InferenceJobResult(
        value={"activity": "stale"},
        raw_text='{"activity":"stale"}',
        provenance=_provenance(first),
    )
    with pytest.raises(SQLiteJobLeaseError, match="no longer active"):
        first_adapter.complete(first, result, now=NOW + 7)

    fresh_result = InferenceJobResult(
        value={"activity": "fresh"},
        raw_text='{"activity":"fresh"}',
        provenance=_provenance(second),
    )
    second_adapter.complete(second, fresh_result, now=NOW + 8)
    stored = storage.get_job(job.id)
    assert stored is not None
    assert stored.result["value"] == {"activity": "fresh"}


def test_retry_serializes_failure_and_provenance_then_dead_letters(storage: SQLiteStorage) -> None:
    job = _enqueue(storage, key="job:retry", job_id="job-retry")
    adapter = SQLitePersistentJobAdapter(storage, worker_id="worker", lease_seconds=30)
    claim = adapter.claim_available(limit=1, now=NOW)[0]
    provenance = _provenance(claim)
    failure = JobFailure(
        code="model_transport",
        message="endpoint unavailable",
        transient=True,
        attempt=claim.attempt,
        occurred_at=NOW + 1,
    )

    adapter.schedule_retry(
        claim,
        failure,
        next_attempt_at=NOW + 10,
        provenance=provenance,
        now=NOW + 1,
    )
    retried = storage.get_job(job.id)
    assert retried is not None
    assert retried.status == "pending"
    assert retried.locked_by is None
    assert retried.lease_until is None
    assert retried.available_at.timestamp() == NOW + 10
    error_payload = json.loads(retried.last_error)
    assert error_payload["failure"]["code"] == "model_transport"
    assert error_payload["provenance"]["prompt"]["prompt_hash"] == provenance.prompt_hash

    metrics = adapter.backlog_metrics(now=NOW + 2)
    assert metrics.queued == 1
    assert metrics.retrying == 1
    assert metrics.failed == 1
    assert metrics.dead_lettered == 0

    retry_claim = adapter.claim_available(limit=1, now=NOW + 10)[0]
    terminal_failure = JobFailure(
        code="json_extraction",
        message="invalid response",
        transient=False,
        attempt=retry_claim.attempt,
        occurred_at=NOW + 11,
    )
    adapter.dead_letter(
        retry_claim,
        terminal_failure,
        provenance=_provenance(retry_claim),
        now=NOW + 11,
    )
    dead = storage.get_job(job.id)
    assert dead is not None
    assert dead.status == "dead"
    assert dead.completed_at is not None
    assert dead.locked_by is None
    assert json.loads(dead.last_error)["failure"]["code"] == "json_extraction"


def test_backlog_metrics_report_staleness_and_terminal_counts(storage: SQLiteStorage) -> None:
    _enqueue(storage, key="job:old", job_id="job-old", stale_after_s=5)
    _enqueue(storage, key="job:new", job_id="job-new", stale_after_s=300)
    adapter = SQLitePersistentJobAdapter(storage, worker_id="worker", lease_seconds=30)
    claim = adapter.claim_available(limit=1, now=NOW)[0]
    adapter.dead_letter(
        claim,
        JobFailure("bad", "permanent", False, claim.attempt, NOW + 1),
        provenance=None,
        now=NOW + 1,
    )

    metrics = adapter.backlog_metrics(now=NOW + 10)
    assert metrics.queued == 1
    assert metrics.running == 0
    assert metrics.dead_lettered == 1
    assert metrics.failed == 1
    assert metrics.stale_count == 1
    assert metrics.oldest_stale_age_s == pytest.approx(10.0)


def test_strict_result_serialization_rejects_nan_without_releasing_lease(
    storage: SQLiteStorage,
) -> None:
    job = _enqueue(storage, key="job:nan", job_id="job-nan")
    adapter = SQLitePersistentJobAdapter(storage, worker_id="worker")
    claim = adapter.claim_available(limit=1, now=NOW)[0]
    result = InferenceJobResult(
        value={"activity": float("nan")},
        raw_text="nan",
        provenance=_provenance(claim),
    )

    with pytest.raises(ValueError, match="strict JSON"):
        adapter.complete(claim, result, now=NOW + 1)
    still_running = storage.get_job(job.id)
    assert still_running is not None
    assert still_running.status == "running"
    assert still_running.locked_by == claim.lease_id
