"""High-level durable queue, replay, and status orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .client import ClientResponse, JsonBatchClient
from .config import TransportConfig
from .errors import ReplayError
from .metrics import TransportMetrics
from .spool import LocalSpool, SpoolBatch, StoredArtifact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ReplayReport:
    """Outcome of one best-effort replay pass."""

    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    artifacts_attempted: int = 0
    artifacts_uploaded: int = 0
    artifacts_failed: int = 0
    batches_attempted: int = 0
    batches_uploaded: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def finish(self) -> None:
        self.finished_at = _now()

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifacts": {
                "attempted": self.artifacts_attempted,
                "uploaded": self.artifacts_uploaded,
                "failed": self.artifacts_failed,
            },
            "batches": {
                "attempted": self.batches_attempted,
                "uploaded": self.batches_uploaded,
                "failed": self.batches_failed,
            },
            "errors": list(self.errors),
        }


class CollectorTransport:
    """Source-independent durable transport for generic contract dictionaries."""

    def __init__(
        self,
        config: TransportConfig | None = None,
        *,
        spool: LocalSpool | None = None,
        client: JsonBatchClient | None = None,
        metrics: TransportMetrics | None = None,
    ) -> None:
        self.config = config or TransportConfig.from_env()
        self.metrics = metrics or TransportMetrics()
        self.spool = spool or LocalSpool(self.config.spool_dir, fsync=self.config.fsync)
        self.client = client or JsonBatchClient(
            self.config,
            retry_observer=self._on_retry,
        )

    def _on_retry(self, _attempt: int, _delay: float, _error: BaseException) -> None:
        self.metrics.increment("retry_attempts")

    @staticmethod
    def _record_count(batch: SpoolBatch) -> int:
        return len(batch.records)

    def enqueue_batch(
        self,
        records: Iterable[Mapping[str, Any]] | Mapping[str, Any],
        *,
        batch_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> SpoolBatch:
        batch = self.spool.enqueue_batch(records, batch_id=batch_id, payload=payload)
        self.metrics.increment("batches_enqueued")
        self.metrics.increment("records_enqueued", self._record_count(batch))
        try:
            self.metrics.increment("bytes_spooled", batch.path.stat().st_size)
        except OSError:
            pass
        return batch

    # Queue terminology is useful when transport is used as an adapter.
    queue_batch = enqueue_batch

    def enqueue_artifact(
        self,
        source: object,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoredArtifact:
        artifact = self.spool.put_artifact(source, metadata=metadata)
        if artifact.deduplicated:
            self.metrics.increment("artifacts_deduplicated")
        else:
            self.metrics.increment("artifacts_stored")
            self.metrics.increment("bytes_spooled", artifact.size)
        return artifact

    store_artifact = enqueue_artifact

    @staticmethod
    def _response_is_success(response: ClientResponse) -> bool:
        return response.ok or response.idempotent_conflict

    def upload_artifact(
        self,
        artifact: StoredArtifact | object,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClientResponse | None:
        """Deliver one stored artifact, skipping a durable upload marker."""

        if not isinstance(artifact, StoredArtifact):
            artifact = self.enqueue_artifact(artifact, metadata=metadata)
        if self.spool.is_artifact_uploaded(artifact.sha256):
            return None
        try:
            response = self.client.upload_artifact(artifact)
            if not self._response_is_success(response):
                raise RuntimeError(f"artifact upload returned HTTP {response.status_code}")
            self.spool.mark_artifact_uploaded(artifact.sha256, response.body)
            self.metrics.increment("artifacts_uploaded")
            if response.ok:
                self.metrics.increment("bytes_uploaded", artifact.size)
            return response
        except Exception as error:
            self._record_delivery_error(error)
            if getattr(error, "attempts", 1) > 1:
                self.metrics.increment("retry_exhausted")
            self.metrics.increment("artifacts_failed")
            raise

    def _record_delivery_error(self, error: BaseException) -> None:
        self.metrics.record_error(error)

    def replay(
        self,
        *,
        limit: int | None = None,
        raise_on_error: bool = False,
    ) -> ReplayReport:
        """Replay artifacts and batches, retaining every failed item locally."""

        if limit is not None and limit < 0:
            raise ValueError("limit cannot be negative")
        self.metrics.mark_replay()
        report = ReplayReport()
        errors: list[BaseException] = []

        for artifact in self.spool.list_pending_artifacts()[:limit]:
            report.artifacts_attempted += 1
            self.metrics.increment("artifacts_replayed")
            try:
                self.upload_artifact(artifact)
                report.artifacts_uploaded += 1
            except Exception as error:
                errors.append(error)
                report.artifacts_failed += 1
                report.errors.append(f"artifact {artifact.sha256}: {error}")

        for batch in self.spool.claim_pending_batches(limit=limit):
            report.batches_attempted += 1
            self.metrics.increment("batches_replayed")
            try:
                response = self.client.ingest_batch(batch.payload, batch_id=batch.batch_id)
                if not self._response_is_success(response):
                    raise RuntimeError(f"batch ingest returned HTTP {response.status_code}")
                try:
                    batch_bytes = batch.path.stat().st_size
                except OSError:
                    batch_bytes = 0
                self.spool.acknowledge_batch(batch)
                report.batches_uploaded += 1
                self.metrics.increment("batches_uploaded")
                self.metrics.increment("records_uploaded", self._record_count(batch))
                self.metrics.increment("bytes_uploaded", batch_bytes)
            except BaseException as error:
                errors.append(error)
                report.batches_failed += 1
                report.errors.append(f"batch {batch.batch_id}: {error}")
                self.metrics.increment("batches_failed")
                self._record_delivery_error(error)
                if getattr(error, "attempts", 1) > 1:
                    self.metrics.increment("retry_exhausted")
                self.spool.release_batch(batch)

        report.finish()
        if raise_on_error and errors:
            raise ReplayError(errors)
        return report

    # ``flush`` is intentionally an alias rather than an eager enqueue API;
    # callers can always persist first and choose when network I/O occurs.
    flush = replay
    replay_once = replay

    def status(self) -> dict[str, object]:
        """Return counters plus current durable pending/processing state."""

        return self.metrics.snapshot(self.spool.status())

    metrics_snapshot = status


Transport = CollectorTransport
