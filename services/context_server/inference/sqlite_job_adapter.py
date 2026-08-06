"""SQLite bridge for the inference worker's persistent-job protocol.

The storage package owns the database schema and its general-purpose job
facade.  This module owns only the inference interpretation of those rows.  It
uses the public ``SQLiteStorage.claim_next_job``/``get_job`` methods and its
public ``connection``/``transaction`` seams for lease-checked transitions;
``runtime.py`` and ``storage/database.py`` remain untouched.
"""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .jobs import (
    ClaimedJob,
    InferenceJob,
    InferenceJobResult,
    JobFailure,
)
from .metrics import BacklogMetrics
from .provenance import InferenceProvenance


class SQLiteJobLeaseError(ValueError):
    """Raised when a worker tries to transition a job with an expired lease."""


CompleteCallback = Callable[[Any, InferenceJobResult], None]


class SQLitePersistentJobAdapter:
    """Implement :class:`PersistentJobAdapter` over ``SQLiteStorage``.

    ``storage`` is intentionally duck-typed.  A production instance is the
    storage package's ``SQLiteStorage``; keeping the constructor structural
    makes the inference lane easy to test with a narrow facade.  The callback,
    when supplied, is invoked inside the completion transaction after the
    result row is updated.  A callback failure rolls back the completion and
    should therefore be idempotent when it writes derived observations.
    """

    def __init__(
        self,
        storage: Any,
        *,
        worker_id: str = "inference-worker",
        lease_seconds: float = 300.0,
        on_complete: CompleteCallback | None = None,
        max_pending_screenshot_jobs: int = 24,
        max_pending_source_jobs: int = 1,
    ) -> None:
        if not hasattr(storage, "claim_next_job") or not hasattr(storage, "get_job"):
            raise TypeError("storage must expose the SQLiteStorage public job API")
        if not hasattr(storage, "connection") or not hasattr(storage, "transaction"):
            raise TypeError("storage must expose connection and transaction seams")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if not math.isfinite(float(lease_seconds)) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive finite number")
        if (
            isinstance(max_pending_screenshot_jobs, bool)
            or not isinstance(max_pending_screenshot_jobs, int)
            or max_pending_screenshot_jobs < 1
        ):
            raise ValueError("max_pending_screenshot_jobs must be a positive integer")
        if (
            isinstance(max_pending_source_jobs, bool)
            or not isinstance(max_pending_source_jobs, int)
            or max_pending_source_jobs < 1
        ):
            raise ValueError("max_pending_source_jobs must be a positive integer")
        self.storage = storage
        self.worker_id = worker_id.strip()
        self.lease_seconds = float(lease_seconds)
        self.on_complete = on_complete
        self.max_pending_screenshot_jobs = int(max_pending_screenshot_jobs)
        self.max_pending_source_jobs = int(max_pending_source_jobs)
        self._claim_lock = threading.Lock()

    def claim_available(self, *, limit: int, now: float) -> Sequence[ClaimedJob]:
        """Atomically claim up to ``limit`` ready rows and hydrate inference jobs."""

        if limit < 1:
            return ()
        current = _datetime_from_epoch(now)
        self._compact_pending_jobs(current)
        claimed: list[ClaimedJob] = []
        for _ in range(int(limit)):
            lease_id = self._new_lease_id()
            record = self.storage.claim_next_job(
                lease_id,
                lease_seconds=self.lease_seconds,
                now=current,
            )
            if record is None:
                break
            try:
                job = _inference_job_from_storage_record(record)
            except Exception as error:
                # Do not leave a malformed row invisibly locked.  It remains
                # pending with an explicit adapter error for inspection; the
                # caller still receives the original parse failure.
                self._release_unreadable_claim(record.id, lease_id, current, error)
                raise
            actual_lease = record.locked_by or lease_id
            if actual_lease != lease_id:
                raise SQLiteJobLeaseError(
                    f"storage returned an unexpected lease for job {record.id}"
                )
            claimed.append(
                ClaimedJob(
                    job=job,
                    lease_id=actual_lease,
                    attempt=record.attempts,
                    leased_at=float(now),
                )
            )
        return tuple(claimed)

    def complete(self, claim: ClaimedJob, result: InferenceJobResult, *, now: float) -> None:
        """Persist a successful result, verifying the current lease first."""

        current = _datetime_from_epoch(now)
        result_json = _strict_json_text(result.to_dict())
        completed_job = None
        with self.storage.transaction() as connection:
            row = self._active_row(connection, claim)
            if row["status"] == "succeeded":
                # Storage completion is idempotent.  In particular, do not
                # invoke on_complete twice for a retried HTTP request.
                return
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', result_json = ?,
                    lease_until = NULL, locked_by = NULL,
                    last_error = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND locked_by = ?
                """,
                (
                    result_json,
                    _timestamp(current),
                    _timestamp(current),
                    claim.job.id,
                    claim.lease_id,
                ),
            )
            if updated.rowcount != 1:
                raise SQLiteJobLeaseError(f"claim is no longer active for job {claim.job.id}")
        if self.on_complete is not None:
            completed_job = self.storage.get_job(claim.job.id)
            if completed_job is None:
                raise KeyError(f"completed job disappeared: {claim.job.id}")
            self.on_complete(completed_job, result)

    def schedule_retry(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        next_attempt_at: float,
        provenance: InferenceProvenance | None,
        now: float,
    ) -> None:
        """Release a lease and persist a retryable failure with provenance."""

        current = _datetime_from_epoch(now)
        next_attempt = _datetime_from_epoch(next_attempt_at)
        error_json = _failure_json(failure, provenance)
        with self.storage.transaction() as connection:
            self._active_row(connection, claim)
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', last_error = ?, available_at = ?,
                    lease_until = NULL, locked_by = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND locked_by = ?
                """,
                (
                    error_json,
                    _timestamp(next_attempt),
                    _timestamp(current),
                    claim.job.id,
                    claim.lease_id,
                ),
            )
            if updated.rowcount != 1:
                raise SQLiteJobLeaseError(f"claim is no longer active for job {claim.job.id}")

    def dead_letter(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        provenance: InferenceProvenance | None,
        now: float,
    ) -> None:
        """Persist a terminal failure and clear the active lease."""

        current = _datetime_from_epoch(now)
        error_json = _failure_json(failure, provenance)
        with self.storage.transaction() as connection:
            self._active_row(connection, claim)
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = 'dead', last_error = ?, available_at = ?,
                    lease_until = NULL, locked_by = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND locked_by = ?
                """,
                (
                    error_json,
                    _timestamp(current),
                    _timestamp(current),
                    _timestamp(current),
                    claim.job.id,
                    claim.lease_id,
                ),
            )
            if updated.rowcount != 1:
                raise SQLiteJobLeaseError(f"claim is no longer active for job {claim.job.id}")

    def backlog_metrics(self, *, now: float) -> BacklogMetrics:
        """Calculate queue and staleness metrics from committed job rows."""

        observed_at = _finite_epoch(now)
        with self.storage.transaction() as connection:
            rows = connection.execute(
                """
                SELECT status, payload_json, last_error, created_at,
                       updated_at, available_at
                FROM jobs
                """
            ).fetchall()

        queued_rows = [row for row in rows if str(row["status"]) == "pending"]
        running_rows = [row for row in rows if str(row["status"]) == "running"]
        succeeded = sum(str(row["status"]) == "succeeded" for row in rows)
        dead_lettered = sum(str(row["status"]) in {"dead", "failed"} for row in rows)
        failed = sum(bool(row["last_error"]) for row in rows)
        retrying = sum(
            _epoch_from_value(row["available_at"]) > observed_at for row in queued_rows
        )
        active_rows = queued_rows + running_rows
        stale_ages = [
            age
            for row in active_rows
            if (age := max(0.0, observed_at - _epoch_from_value(row["updated_at"])))
            > _stale_after_seconds(row["payload_json"])
        ]
        pending_ages = [
            max(0.0, observed_at - _epoch_from_value(row["created_at"]))
            for row in queued_rows
        ]
        active_ages = [
            max(0.0, observed_at - _epoch_from_value(row["created_at"]))
            for row in active_rows
        ]
        return BacklogMetrics(
            observed_at=observed_at,
            queued=len(queued_rows),
            running=len(running_rows),
            retrying=retrying,
            succeeded=int(succeeded),
            failed=failed,
            dead_lettered=int(dead_lettered),
            oldest_pending_age_s=max(pending_ages, default=0.0),
            oldest_active_age_s=max(active_ages, default=0.0),
            stale_count=len(stale_ages),
            oldest_stale_age_s=max(stale_ages, default=0.0),
        )

    metrics = backlog_metrics

    def _compact_pending_jobs(self, current: datetime) -> int:
        """Terminalize redundant pristine jobs while retaining newest evidence.

        Capture, source-event, observation, and artifact rows are deliberately
        outside this adapter and remain durable.  Retried jobs are also left
        alone: only pending rows that have never failed or been attempted are
        eligible for backpressure compaction. Screenshots use a global rolling
        window; metadata sources retain their newest unattempted snapshot per
        stable source and event kind.
        """

        with self.storage.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, payload_json, created_at
                FROM jobs
                WHERE status = 'pending' AND attempts = 0 AND last_error IS NULL
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
            screenshot_rows = [row for row in rows if _is_screenshot_job(row)]
            redundant = list(screenshot_rows[self.max_pending_screenshot_jobs :])
            metadata_counts: dict[str, int] = {}
            for row in rows:
                if _is_screenshot_job(row):
                    continue
                key = _metadata_compaction_key(row)
                if key is None:
                    continue
                retained_count = metadata_counts.get(key, 0)
                if retained_count >= self.max_pending_source_jobs:
                    redundant.append(row)
                metadata_counts[key] = retained_count + 1
            if not redundant:
                return 0

            retained = screenshot_rows[: self.max_pending_screenshot_jobs]
            retained_by_source: dict[str, str] = {}
            for row in retained:
                if (source := _screenshot_source(row)) is not None:
                    retained_by_source.setdefault(source, str(row["id"]))
            completed_at = _timestamp(current)
            compacted = 0
            for row in redundant:
                source = _screenshot_source(row)
                is_screenshot = _is_screenshot_job(row)
                result_json = _strict_json_text(
                    {
                        "status": "skipped",
                        "code": (
                            "backpressure_superseded"
                            if is_screenshot
                            else "backpressure_source_superseded"
                        ),
                        "message": (
                            "Inference was superseded by newer context; the original "
                            "capture, source event, observation, and artifacts remain stored."
                        ),
                        "superseded_by": (
                            retained_by_source.get(source) if is_screenshot else None
                        ),
                    }
                )
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'succeeded', result_json = ?,
                        completed_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                        AND attempts = 0 AND last_error IS NULL
                    """,
                    (result_json, completed_at, completed_at, str(row["id"])),
                )
                compacted += int(updated.rowcount)
            return compacted

    def _new_lease_id(self) -> str:
        # The token, rather than the human worker name, is written to the
        # single locked_by column.  That makes an expired claim unable to
        # mutate a later attempt even if the same worker process reclaims it.
        with self._claim_lock:
            return f"{self.worker_id}:{uuid.uuid4().hex}"

    @staticmethod
    def _active_row(connection: Any, claim: ClaimedJob) -> Any:
        row = connection.execute(
            "SELECT status, locked_by FROM jobs WHERE id = ?", (claim.job.id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown job: {claim.job.id}")
        if row["status"] == "succeeded":
            return row
        if row["status"] != "running" or row["locked_by"] != claim.lease_id:
            raise SQLiteJobLeaseError(f"claim is no longer active for job {claim.job.id}")
        return row

    def _release_unreadable_claim(
        self,
        job_id: str,
        lease_id: str,
        current: datetime,
        error: BaseException,
    ) -> None:
        message = _strict_json_text(
            {
                "code": "invalid_inference_job_payload",
                "message": str(error),
            }
        )
        with self.storage.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', available_at = ?, lease_until = NULL,
                    locked_by = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND locked_by = ?
                """,
                (_timestamp(current), message, _timestamp(current), job_id, lease_id),
            )


def _is_screenshot_job(row: Any) -> bool:
    kind = str(row["kind"]).strip().lower()
    if kind == "inference.screenshot":
        return True
    payload = _job_payload(row)
    metadata = payload.get("metadata")
    capture_kind = (
        str(metadata.get("capture_kind", "")).strip().lower()
        if isinstance(metadata, Mapping)
        else ""
    )
    source_id = str(payload.get("source_id", "")).strip().lower()
    return capture_kind in {"monitor", "contact_sheet"} or source_id.startswith("screen:")


def _screenshot_source(row: Any) -> str | None:
    payload = _job_payload(row)
    source_id = str(payload.get("source_id", "")).strip()
    if source_id:
        return source_id
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        monitor_id = str(metadata.get("monitor_id", "")).strip()
        if monitor_id:
            return f"screen:{monitor_id}"
        capture_kind = str(metadata.get("capture_kind", "")).strip()
        if capture_kind:
            return capture_kind
    return None


def _metadata_compaction_key(row: Any) -> str | None:
    """Return a stable key for repeated non-visual source snapshots."""

    payload = _job_payload(row)
    source_id = str(payload.get("source_id", "")).strip()
    if not source_id:
        return None
    prompt = payload.get("prompt")
    user_prompt = (
        str(prompt.get("user_prompt", ""))
        if isinstance(prompt, Mapping)
        else ""
    )
    event_kind_match = re.search(r'"event_kind"\s*:\s*"([^"]+)"', user_prompt)
    event_kind = event_kind_match.group(1) if event_kind_match else ""
    return f"{source_id}\x1f{event_kind}"


def _job_payload(row: Any) -> Mapping[str, Any]:
    value = row["payload_json"]
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _inference_job_from_storage_record(record: Any) -> InferenceJob:
    payload = record.payload
    if not isinstance(payload, Mapping):
        raise ValueError("storage job payload must be an object")
    last_error = _decode_last_error(record.last_error)
    return InferenceJob.from_payload(
        record.id,
        payload,
        created_at=_epoch_from_value(record.created_at),
        available_at=_epoch_from_value(record.available_at),
        attempts=int(record.attempts),
        max_attempts=int(record.max_attempts),
        updated_at=_epoch_from_value(record.updated_at),
        last_error=last_error,
    )


def _failure_json(
    failure: JobFailure,
    provenance: InferenceProvenance | None,
) -> str:
    value = {
        "failure": failure.to_dict(),
        "provenance": provenance.to_dict() if provenance is not None else None,
    }
    return _strict_json_text(value)


def _decode_last_error(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {"message": str(value)}
    return dict(decoded) if isinstance(decoded, Mapping) else {"message": str(value)}


def _stale_after_seconds(payload_json: Any) -> float:
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
        value = (
            float(payload.get("stale_after_s", 300.0))
            if isinstance(payload, Mapping)
            else 300.0
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        value = 300.0
    return value if math.isfinite(value) and value > 0 else 300.0


def _strict_json_text(value: Any) -> str:
    normalized = _json_value(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"inference persistence value is not strict JSON: {error}") from error


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported persistence value: {type(value).__name__}")


def _finite_epoch(value: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("timestamp must be numeric seconds since the Unix epoch") from error
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite")
    return result


def _datetime_from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(_finite_epoch(value), tz=UTC)


def _epoch_from_value(value: Any) -> float:
    if isinstance(value, datetime):
        current = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        current = datetime.fromisoformat(text)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).timestamp()


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


__all__ = [
    "CompleteCallback",
    "SQLiteJobLeaseError",
    "SQLitePersistentJobAdapter",
]
