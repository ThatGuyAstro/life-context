"""Bounded inference worker with retry, dead-letter, and queue metrics."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .client import ModelClient, ModelResponse
from .contracts import extract_strict_json
from .errors import ModelHTTPError, exception_code, is_retryable
from .jobs import (
    ClaimedJob,
    InferenceJobResult,
    JobFailure,
    PersistentJobAdapter,
)
from .manager import ModelManager
from .metrics import BacklogMetrics, WorkerCounters
from .provenance import InferenceProvenance, ModelProvenance, PromptProvenance


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 5.0
    max_delay_s: float = 300.0
    jitter_s: float = 0.0
    retry_contract_errors: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.jitter_s < 0:
            raise ValueError("retry delays cannot be negative")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s cannot be less than base_delay_s")

    def delay(self, attempt: int, *, random_value: float = 0.5) -> float:
        exponential = min(self.max_delay_s, self.base_delay_s * (2 ** max(0, attempt - 1)))
        jitter = (max(0.0, min(1.0, random_value)) - 0.5) * 2 * self.jitter_s
        return max(0.0, min(self.max_delay_s + self.jitter_s, exponential + jitter))


@dataclass(frozen=True)
class WorkerRunReport:
    started_at: float
    finished_at: float
    claimed: int
    succeeded: int
    retried: int
    dead_lettered: int
    failures: tuple[JobFailure, ...] = ()

    @property
    def processed(self) -> int:
        return self.succeeded + self.retried + self.dead_lettered

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "claimed": self.claimed,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
            "failures": [failure.to_dict() for failure in self.failures],
        }


class InferenceWorker:
    """Poll, process, and durably transition inference jobs."""

    def __init__(
        self,
        adapter: PersistentJobAdapter,
        models: ModelManager | Mapping[str, ModelClient] | ModelClient,
        *,
        default_model: str | None = None,
        batch_size: int = 1,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
        idle_sleep_s: float = 1.0,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if idle_sleep_s < 0:
            raise ValueError("idle_sleep_s cannot be negative")
        self.adapter = adapter
        self.models = models
        self.default_model = default_model
        self.batch_size = batch_size
        self.retry_policy = retry_policy or RetryPolicy()
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        self.random_fn = random_fn or random.random
        self.idle_sleep_s = idle_sleep_s
        self.counters = WorkerCounters()

    def _resolve_model(self, model_name: str | None) -> ModelClient:
        requested = model_name or self.default_model
        if isinstance(self.models, ModelManager):
            return self.models.get(requested)
        if isinstance(self.models, Mapping):
            if requested is None:
                if len(self.models) != 1:
                    raise KeyError("job does not select a model and no default_model was provided")
                return next(iter(self.models.values()))
            return self.models[requested]
        if requested is not None and requested not in {
            getattr(self.models, "model_name", None),
            self.default_model,
        }:
            raise KeyError(f"requested model is not available: {requested}")
        return self.models

    @staticmethod
    def _model_metadata(client: ModelClient) -> dict[str, Any]:
        metadata = getattr(client, "provenance_metadata", None)
        if isinstance(metadata, Mapping):
            return dict(metadata)
        return {
            "client_name": getattr(client, "model_name", client.__class__.__name__),
            "mode": "unknown",
            "configured_model": getattr(client, "model_name", "unknown"),
            "endpoint": "unknown",
            "structured_output_mode": None,
        }

    def _provenance(
        self,
        claim: ClaimedJob,
        client: ModelClient,
        response: ModelResponse | None,
        *,
        started_at: float,
        finished_at: float,
    ) -> InferenceProvenance:
        metadata = self._model_metadata(client)
        return InferenceProvenance(
            request_id=claim.job.id,
            attempt=claim.attempt,
            prompt=PromptProvenance.from_prompt(claim.job.prompt),
            model=ModelProvenance(
                client_name=str(
                    metadata.get("client_name", getattr(client, "model_name", "unknown"))
                ),
                mode=str(metadata.get("mode", "unknown")),
                configured_model=str(
                    metadata.get("configured_model", getattr(client, "model_name", "unknown"))
                ),
                returned_model=response.model if response else None,
                endpoint=str(metadata.get("endpoint", "unknown")),
                response_id=response.response_id if response else None,
                structured_output_mode=(
                    str(metadata["structured_output_mode"])
                    if metadata.get("structured_output_mode") is not None
                    else None
                ),
                request_id=response.request_id if response else claim.job.id,
                usage=(
                    dict(response.usage) if response and isinstance(response.usage, Mapping) else {}
                ),
                model_revision=(
                    str(metadata["model_revision"])
                    if metadata.get("model_revision") is not None
                    else None
                ),
            ),
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=max(0.0, (finished_at - started_at) * 1000),
        )

    def _failure(
        self, error: BaseException, claim: ClaimedJob, *, occurred_at: float
    ) -> JobFailure:
        return JobFailure(
            code=exception_code(error),
            message=str(error),
            transient=is_retryable(
                error, retry_contract_errors=self.retry_policy.retry_contract_errors
            ),
            attempt=claim.attempt,
            occurred_at=occurred_at,
        )

    def run_once(self, *, now: float | None = None) -> WorkerRunReport:
        started_at = self.clock() if now is None else now
        claims = tuple(self.adapter.claim_available(limit=self.batch_size, now=started_at))
        self.counters.claimed += len(claims)
        succeeded = 0
        retried = 0
        dead_lettered = 0
        failures: list[JobFailure] = []

        for claim in claims:
            client: ModelClient | None = None
            response: ModelResponse | None = None
            job_started = self.clock()
            try:
                client = self._resolve_model(claim.job.model)
                response = client.complete(
                    claim.job.prompt,
                    contract=claim.job.prompt.contract,
                    request_id=claim.job.id,
                )
                value = extract_strict_json(response.text, claim.job.prompt.contract)
                finished = self.clock()
                provenance = self._provenance(
                    claim,
                    client,
                    response,
                    started_at=job_started,
                    finished_at=finished,
                )
            except Exception as error:
                finished = self.clock()
                failure = self._failure(error, claim, occurred_at=finished)
                failures.append(failure)
                provenance = (
                    self._provenance(
                        claim,
                        client,
                        response,
                        started_at=job_started,
                        finished_at=finished,
                    )
                    if client is not None
                    else None
                )
                max_attempts = claim.job.max_attempts or self.retry_policy.max_attempts
                should_retry = failure.transient and claim.attempt < max_attempts
                if should_retry:
                    delay = self.retry_policy.delay(claim.attempt, random_value=self.random_fn())
                    if isinstance(error, ModelHTTPError) and error.retry_after_s is not None:
                        delay = max(delay, error.retry_after_s)
                    self.adapter.schedule_retry(
                        claim,
                        failure,
                        next_attempt_at=finished + delay,
                        provenance=provenance,
                        now=finished,
                    )
                    retried += 1
                    self.counters.retried += 1
                else:
                    self.adapter.dead_letter(claim, failure, provenance=provenance, now=finished)
                    dead_lettered += 1
                    self.counters.dead_lettered += 1
                self.counters.failed += 1
                continue

            # Storage transition errors deliberately escape this handler.  A
            # database outage is not a model failure and must not be followed
            # by a second transition using the same lease.
            self.adapter.complete(
                claim,
                InferenceJobResult(value=value, raw_text=response.text, provenance=provenance),
                now=finished,
            )
            succeeded += 1
            self.counters.succeeded += 1

        finished_at = self.clock()
        return WorkerRunReport(
            started_at=started_at,
            finished_at=finished_at,
            claimed=len(claims),
            succeeded=succeeded,
            retried=retried,
            dead_lettered=dead_lettered,
            failures=tuple(failures),
        )

    def metrics(self, *, now: float | None = None) -> BacklogMetrics:
        return self.adapter.backlog_metrics(now=self.clock() if now is None else now)

    def run_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> WorkerCounters:
        """Poll until stopped; ``max_iterations`` makes smoke tests finite."""

        stop_event = stop_event or threading.Event()
        iterations = 0
        while not stop_event.is_set() and (max_iterations is None or iterations < max_iterations):
            report = self.run_once()
            iterations += 1
            if report.claimed == 0 and self.idle_sleep_s:
                self.sleeper(self.idle_sleep_s)
        return self.counters
