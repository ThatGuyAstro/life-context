"""Backlog, retry, and staleness metric value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BacklogMetrics:
    """Point-in-time queue metrics calculated by the persistence adapter."""

    observed_at: float
    queued: int = 0
    running: int = 0
    retrying: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0
    oldest_pending_age_s: float = 0.0
    oldest_active_age_s: float = 0.0
    stale_count: int = 0
    oldest_stale_age_s: float = 0.0

    @property
    def backlog_count(self) -> int:
        return self.queued + self.running

    @property
    def pending_count(self) -> int:
        return max(0, self.queued - self.retrying)

    @property
    def staleness_seconds(self) -> float:
        return self.oldest_stale_age_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "queued": self.queued,
            "running": self.running,
            "retrying": self.retrying,
            "pending": self.pending_count,
            "running_count": self.running,
            "backlog": self.backlog_count,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "dead_lettered": self.dead_lettered,
            "oldest_pending_age_s": self.oldest_pending_age_s,
            "oldest_active_age_s": self.oldest_active_age_s,
            "stale_count": self.stale_count,
            "oldest_stale_age_s": self.oldest_stale_age_s,
            "staleness_seconds": self.staleness_seconds,
        }


@dataclass
class WorkerCounters:
    """Process-local counters; durable state remains the adapter's concern."""

    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead_lettered: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
            "failed": self.failed,
        }
