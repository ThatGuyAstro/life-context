"""Persistent-job adapter interfaces and an in-memory deterministic adapter.

The protocol is the storage seam.  A production adapter can implement its
methods with a database transaction/lease without changing the worker or
model client.  The in-memory implementation is intentionally test-only in
spirit, but useful for integration tests and local demos.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .contracts import PromptBundle
from .metrics import BacklogMetrics
from .provenance import InferenceProvenance


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class InferenceJob:
    id: str
    prompt: PromptBundle
    model: str | None = None
    created_at: float = field(default_factory=time.time)
    available_at: float | None = None
    attempts: int = 0
    max_attempts: int | None = None
    stale_after_s: float = 300.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: float | None = None
    last_error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("job id is required")
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be positive when provided")
        if self.stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.created_at)

    def is_stale(self, now: float) -> bool:
        updated_at = self.updated_at if self.updated_at is not None else self.created_at
        return max(0.0, now - updated_at) > self.stale_after_s

    def to_payload(self) -> dict[str, Any]:
        """Return the payload shape a persistent adapter can store as JSON."""

        return {
            "model": self.model,
            "prompt": self.prompt.to_payload(),
            "stale_after_s": self.stale_after_s,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(
        cls,
        job_id: str,
        payload: Mapping[str, Any],
        *,
        created_at: float,
        available_at: float | None = None,
        attempts: int = 0,
        max_attempts: int | None = None,
        updated_at: float | None = None,
        last_error: Mapping[str, Any] | None = None,
    ) -> InferenceJob:
        prompt_payload = payload.get("prompt")
        if not isinstance(prompt_payload, Mapping):
            raise ValueError("inference job payload must contain a prompt object")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        return cls(
            id=job_id,
            prompt=PromptBundle.from_payload(prompt_payload),
            model=str(payload["model"]) if payload.get("model") is not None else None,
            created_at=created_at,
            available_at=available_at,
            attempts=attempts,
            max_attempts=max_attempts,
            stale_after_s=float(payload.get("stale_after_s", 300.0)),
            metadata=metadata,
            updated_at=updated_at,
            last_error=last_error,
        )


@dataclass(frozen=True)
class ClaimedJob:
    job: InferenceJob
    lease_id: str
    attempt: int
    leased_at: float


@dataclass(frozen=True)
class InferenceJobResult:
    value: Any
    raw_text: str
    provenance: InferenceProvenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "raw_text": self.raw_text,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class JobFailure:
    code: str
    message: str
    transient: bool
    attempt: int
    occurred_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "transient": self.transient,
            "attempt": self.attempt,
            "occurred_at": self.occurred_at,
        }


@runtime_checkable
class PersistentJobAdapter(Protocol):
    """Storage contract required by :class:`InferenceWorker`.

    ``claim_available`` must atomically move jobs into a leased/running state.
    Completion, retry scheduling, and dead-lettering must verify the lease so a
    late worker cannot overwrite a newer attempt.
    """

    def claim_available(self, *, limit: int, now: float) -> Sequence[ClaimedJob]: ...

    def complete(self, claim: ClaimedJob, result: InferenceJobResult, *, now: float) -> None: ...

    def schedule_retry(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        next_attempt_at: float,
        provenance: InferenceProvenance | None,
        now: float,
    ) -> None: ...

    def dead_letter(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        provenance: InferenceProvenance | None,
        now: float,
    ) -> None: ...

    def backlog_metrics(self, *, now: float) -> BacklogMetrics: ...


JobAdapter = PersistentJobAdapter


@dataclass
class _StoredJob:
    job: InferenceJob
    status: JobStatus = JobStatus.QUEUED
    lease_id: str | None = None
    leased_at: float | None = None
    result: InferenceJobResult | None = None
    failure: JobFailure | None = None
    failure_provenance: InferenceProvenance | None = None


@dataclass(frozen=True)
class JobSnapshot:
    job: InferenceJob
    status: JobStatus
    result: InferenceJobResult | None
    failure: JobFailure | None
    provenance: InferenceProvenance | None


class InMemoryJobAdapter:
    """Thread-safe adapter used by deterministic tests and local smoke runs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, _StoredJob] = {}
        self._lease_counter = 0
        self.events: list[tuple[str, str, float]] = []

    def enqueue(self, job: InferenceJob) -> None:
        with self._lock:
            if job.id in self._records:
                raise ValueError(f"job already exists: {job.id}")
            self._records[job.id] = _StoredJob(job=job)
            self.events.append((job.id, JobStatus.QUEUED.value, job.created_at))

    def claim_available(self, *, limit: int, now: float) -> Sequence[ClaimedJob]:
        if limit < 1:
            return ()
        claimed: list[ClaimedJob] = []
        with self._lock:
            candidates = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.status is JobStatus.QUEUED
                    and (record.job.available_at is None or record.job.available_at <= now)
                ),
                key=lambda record: (record.job.created_at, record.job.id),
            )
            for record in candidates[:limit]:
                self._lease_counter += 1
                lease_id = f"lease-{self._lease_counter:06d}"
                attempt = record.job.attempts + 1
                record.job = replace(record.job, attempts=attempt, updated_at=now)
                record.status = JobStatus.RUNNING
                record.lease_id = lease_id
                record.leased_at = now
                record.failure = None
                self.events.append((record.job.id, JobStatus.RUNNING.value, now))
                claimed.append(ClaimedJob(record.job, lease_id, attempt, now))
        return tuple(claimed)

    def _record_for_claim(self, claim: ClaimedJob) -> _StoredJob:
        record = self._records.get(claim.job.id)
        if (
            record is None
            or record.status is not JobStatus.RUNNING
            or record.lease_id != claim.lease_id
        ):
            raise ValueError(f"claim is no longer active for job {claim.job.id}")
        return record

    def complete(self, claim: ClaimedJob, result: InferenceJobResult, *, now: float) -> None:
        with self._lock:
            record = self._record_for_claim(claim)
            record.status = JobStatus.SUCCEEDED
            record.job = replace(record.job, available_at=None, updated_at=now, last_error=None)
            record.result = result
            record.failure = None
            record.failure_provenance = result.provenance
            record.lease_id = None
            record.leased_at = None
            self.events.append((record.job.id, JobStatus.SUCCEEDED.value, now))

    def schedule_retry(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        next_attempt_at: float,
        provenance: InferenceProvenance | None,
        now: float,
    ) -> None:
        with self._lock:
            record = self._record_for_claim(claim)
            record.status = JobStatus.QUEUED
            record.job = replace(
                record.job,
                available_at=next_attempt_at,
                updated_at=now,
                last_error=failure.to_dict(),
            )
            record.failure = failure
            record.failure_provenance = provenance
            record.lease_id = None
            record.leased_at = None
            self.events.append((record.job.id, "retry", now))

    def dead_letter(
        self,
        claim: ClaimedJob,
        failure: JobFailure,
        *,
        provenance: InferenceProvenance | None,
        now: float,
    ) -> None:
        with self._lock:
            record = self._record_for_claim(claim)
            record.status = JobStatus.DEAD_LETTER
            record.job = replace(
                record.job, available_at=None, updated_at=now, last_error=failure.to_dict()
            )
            record.failure = failure
            record.failure_provenance = provenance
            record.lease_id = None
            record.leased_at = None
            self.events.append((record.job.id, JobStatus.DEAD_LETTER.value, now))

    def backlog_metrics(self, *, now: float) -> BacklogMetrics:
        with self._lock:
            records = tuple(self._records.values())
        queued = [record for record in records if record.status is JobStatus.QUEUED]
        running = [record for record in records if record.status is JobStatus.RUNNING]
        retrying = [
            record
            for record in queued
            if record.job.available_at is not None and record.job.available_at > now
        ]
        active = queued + running
        stale = [record for record in active if record.job.is_stale(now)]
        oldest_pending = max((record.job.age_s(now) for record in queued), default=0.0)
        oldest_active = max((record.job.age_s(now) for record in active), default=0.0)
        oldest_stale = max((record.job.age_s(now) for record in stale), default=0.0)
        return BacklogMetrics(
            observed_at=now,
            queued=len(queued),
            running=len(running),
            retrying=len(retrying),
            succeeded=sum(record.status is JobStatus.SUCCEEDED for record in records),
            failed=sum(record.failure is not None for record in records),
            dead_lettered=sum(record.status is JobStatus.DEAD_LETTER for record in records),
            oldest_pending_age_s=oldest_pending,
            oldest_active_age_s=oldest_active,
            stale_count=len(stale),
            oldest_stale_age_s=oldest_stale,
        )

    metrics = backlog_metrics

    def get(self, job_id: str) -> JobSnapshot:
        with self._lock:
            record = self._records[job_id]
            return JobSnapshot(
                job=record.job,
                status=record.status,
                result=record.result,
                failure=record.failure,
                provenance=record.failure_provenance,
            )

    def status(self, job_id: str) -> JobStatus:
        return self.get(job_id).status

    def recover_expired(self, *, now: float) -> int:
        """Requeue running jobs whose lease exceeded their per-job staleness window."""

        recovered = 0
        with self._lock:
            for record in self._records.values():
                if (
                    record.status is JobStatus.RUNNING
                    and record.leased_at is not None
                    and now - record.leased_at > record.job.stale_after_s
                ):
                    record.status = JobStatus.QUEUED
                    record.job = replace(record.job, available_at=now, updated_at=now)
                    record.lease_id = None
                    record.leased_at = None
                    recovered += 1
                    self.events.append((record.job.id, "lease_recovered", now))
        return recovered
