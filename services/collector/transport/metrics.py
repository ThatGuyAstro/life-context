"""In-process counters and replay/status snapshots for the transport."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Mapping


DEFAULT_COUNTERS = (
    "batches_enqueued",
    "batches_replayed",
    "batches_uploaded",
    "batches_failed",
    "records_enqueued",
    "records_uploaded",
    "artifacts_stored",
    "artifacts_deduplicated",
    "artifacts_replayed",
    "artifacts_uploaded",
    "artifacts_failed",
    "retry_attempts",
    "retry_exhausted",
    "replay_runs",
    "bytes_spooled",
    "bytes_uploaded",
)


class TransportMetrics:
    """Thread-safe counters with stable, JSON-friendly snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in DEFAULT_COUNTERS}
        self._last_error: str | None = None
        self._last_replay_at: str | None = None

    def increment(self, name: str, amount: int = 1) -> int:
        if amount < 0:
            raise ValueError("metric increments cannot be negative")
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount
            return self._counters[name]

    # ``inc`` is intentionally a short alias for embedding code.
    inc = increment

    def record_error(self, error: BaseException | str) -> None:
        with self._lock:
            self._last_error = str(error)

    def mark_replay(self) -> None:
        with self._lock:
            self._counters["replay_runs"] += 1
            self._last_replay_at = datetime.now(timezone.utc).isoformat()

    def snapshot(self, spool_status: Mapping[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            result: dict[str, object] = dict(self._counters)
            result["last_error"] = self._last_error
            result["last_replay_at"] = self._last_replay_at
        if spool_status is not None:
            result["spool"] = dict(spool_status)
            for key in (
                "pending_batches",
                "processing_batches",
                "pending_artifacts",
                "uploaded_artifacts",
                "spool_bytes",
            ):
                if key in spool_status:
                    result[key] = spool_status[key]
        return result
