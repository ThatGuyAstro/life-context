from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.context_server.config import Settings
from services.context_server.inference import SQLitePersistentJobAdapter
from services.context_server.runtime import ContextRuntime
from services.context_server.storage import SQLiteStorage


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
BASE_EPOCH = BASE_TIME.timestamp()


@pytest.fixture()
def storage():
    value = SQLiteStorage(":memory:", load_sqlite_vec_extension=False)
    try:
        yield value
    finally:
        value.close()


def _enqueue(
    storage: SQLiteStorage,
    job_id: str,
    *,
    created_at: datetime,
    max_attempts: int = 3,
):
    return storage.enqueue_job(
        "inference",
        {"job_id": job_id},
        idempotency_key=f"queue-test:{job_id}",
        job_id=job_id,
        max_attempts=max_attempts,
        now=created_at,
    )


def test_queue_metrics_report_pending_age_and_terminal_counts(storage: SQLiteStorage) -> None:
    completed = _enqueue(
        storage,
        "completed",
        created_at=BASE_TIME - timedelta(seconds=10),
    )
    completed_claim = storage.claim_next_job("completion-worker", now=BASE_TIME)
    assert completed_claim is not None
    assert completed_claim.id == completed.id
    storage.complete_job(completed.id, result={"ok": True}, now=BASE_TIME)

    retrying = _enqueue(
        storage,
        "retrying",
        created_at=BASE_TIME - timedelta(seconds=90),
        max_attempts=2,
    )
    retry_claim = storage.claim_next_job("retry-worker", now=BASE_TIME)
    assert retry_claim is not None
    assert retry_claim.id == retrying.id
    storage.fail_job(
        retrying.id,
        "temporary provider failure",
        retry_delay=20,
        now=BASE_TIME,
    )

    dead = _enqueue(
        storage,
        "dead",
        created_at=BASE_TIME - timedelta(seconds=50),
        max_attempts=1,
    )
    dead_claim = storage.claim_next_job("dead-worker", now=BASE_TIME)
    assert dead_claim is not None
    assert dead_claim.id == dead.id
    storage.fail_job(dead.id, "permanent provider failure", now=BASE_TIME)

    _enqueue(storage, "pending-old", created_at=BASE_TIME - timedelta(seconds=120))
    _enqueue(storage, "pending-new", created_at=BASE_TIME - timedelta(seconds=30))

    metrics = SQLitePersistentJobAdapter(storage).backlog_metrics(now=BASE_EPOCH)

    assert metrics.queued == 3
    assert metrics.pending_count == 2
    assert metrics.retrying == 1
    assert metrics.oldest_pending_age_s == pytest.approx(120.0)
    assert metrics.succeeded == 1
    assert metrics.failed == 2
    assert metrics.dead_lettered == 1

    serialized = metrics.to_dict()
    assert serialized["pending"] == 2
    assert serialized["succeeded"] == 1
    assert serialized["failed"] == 2
    assert serialized["dead_lettered"] == 1
    assert serialized["oldest_pending_age_s"] == pytest.approx(120.0)


def test_retry_transition_updates_queue_metrics_and_attempts(storage: SQLiteStorage) -> None:
    job = _enqueue(storage, "retry-transition", created_at=BASE_TIME, max_attempts=2)
    adapter = SQLitePersistentJobAdapter(storage)

    initial = adapter.backlog_metrics(now=BASE_EPOCH)
    assert initial.pending_count == 1
    assert initial.retrying == 0

    first_claim = storage.claim_next_job("retry-worker", now=BASE_TIME)
    assert first_claim is not None
    assert first_claim.id == job.id
    assert first_claim.attempts == 1

    running = adapter.backlog_metrics(now=BASE_EPOCH + 1)
    assert running.queued == 0
    assert running.running == 1
    assert running.pending_count == 0

    retried = storage.fail_job(
        job.id,
        "temporary failure",
        retry_delay=10,
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert retried.status == "pending"
    assert retried.attempts == 1
    assert retried.available_at == BASE_TIME + timedelta(seconds=11)

    waiting = adapter.backlog_metrics(now=BASE_EPOCH + 2)
    assert waiting.queued == 1
    assert waiting.running == 0
    assert waiting.retrying == 1
    assert waiting.pending_count == 0
    assert waiting.failed == 1
    assert storage.claim_next_job("retry-worker", now=BASE_TIME + timedelta(seconds=10)) is None

    second_claim = storage.claim_next_job(
        "retry-worker",
        now=BASE_TIME + timedelta(seconds=11),
    )
    assert second_claim is not None
    assert second_claim.id == job.id
    assert second_claim.attempts == 2

    due = adapter.backlog_metrics(now=BASE_EPOCH + 11)
    assert due.queued == 0
    assert due.running == 1
    assert due.retrying == 0
    assert due.pending_count == 0
    assert due.failed == 1


def test_context_runtime_status_persists_queue_metrics_across_restart(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path, docs_enabled=False)
    created_at = datetime.now(timezone.utc)
    runtime = ContextRuntime(settings)

    try:
        completed = _enqueue(
            runtime.storage,
            "runtime-completed",
            created_at=created_at - timedelta(seconds=10),
        )
        completed_claim = runtime.storage.claim_next_job("completion-worker", now=created_at)
        assert completed_claim is not None
        assert completed_claim.id == completed.id
        runtime.storage.complete_job(completed.id, result={"ok": True}, now=created_at)

        retrying = _enqueue(
            runtime.storage,
            "runtime-retrying",
            created_at=created_at - timedelta(seconds=20),
            max_attempts=2,
        )
        retry_claim = runtime.storage.claim_next_job("retry-worker", now=created_at)
        assert retry_claim is not None
        assert retry_claim.id == retrying.id
        runtime.storage.fail_job(
            retrying.id,
            "temporary failure",
            retry_delay=300,
            now=created_at,
        )

        dead = _enqueue(
            runtime.storage,
            "runtime-dead",
            created_at=created_at - timedelta(seconds=15),
            max_attempts=1,
        )
        dead_claim = runtime.storage.claim_next_job("dead-worker", now=created_at)
        assert dead_claim is not None
        assert dead_claim.id == dead.id
        runtime.storage.fail_job(dead.id, "permanent failure", now=created_at)

        pending_old = _enqueue(
            runtime.storage,
            "runtime-pending-old",
            created_at=created_at - timedelta(seconds=60),
        )
        _enqueue(
            runtime.storage,
            "runtime-pending-new",
            created_at=created_at - timedelta(seconds=15),
        )

        before = runtime.status()
    finally:
        runtime.shutdown()

    restarted = ContextRuntime(settings)
    try:
        after = restarted.status()

        for key in ("pending_count", "completed_count", "failed_count", "dead_lettered_count"):
            assert after["backlog"][key] == before["backlog"][key]
        # The runtime status reports every persisted pending row, including a
        # deferred retry; BacklogMetrics exposes that retry separately.
        assert after["queue"]["pending"] == 3
        assert after["queue"]["completed"] == 1
        assert after["queue"]["failed"] == 1
        assert after["backlog"]["dead_lettered_count"] == 1
        assert after["queue"]["oldest_pending_at"] == pending_old.created_at.isoformat()
        assert after["queue"]["oldest_pending_age_seconds"] == pytest.approx(
            before["queue"]["oldest_pending_age_seconds"], abs=1.0
        )
    finally:
        restarted.shutdown()
