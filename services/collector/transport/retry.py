"""Small, injectable retry/backoff policy used by the HTTP client."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar


T = TypeVar("T")


def is_retryable(error: BaseException) -> bool:
    """Read the retry classification without coupling retry to HTTP types."""

    return bool(getattr(error, "retryable", False))


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with optional bounded proportional jitter."""

    max_attempts: int = 4
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0
    multiplier: float = 2.0
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")

    def delay_for(
        self,
        failed_attempt: int,
        *,
        retry_after_seconds: float | None = None,
        random_value: float | None = None,
    ) -> float:
        """Return the delay after a failed attempt (attempts are 1-based)."""

        exponential = min(
            self.max_delay_seconds,
            self.initial_delay_seconds * self.multiplier ** max(failed_attempt - 1, 0),
        )
        if retry_after_seconds is not None and retry_after_seconds > exponential:
            exponential = min(self.max_delay_seconds, retry_after_seconds)
        if self.jitter:
            value = random.random() if random_value is None else random_value
            exponential *= 1 + ((value * 2) - 1) * self.jitter
        return max(0.0, min(self.max_delay_seconds, exponential))

    def run(
        self,
        operation: Callable[[int], T],
        *,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] | None = None,
        retry_observer: Callable[[int, float, BaseException], None] | None = None,
    ) -> T:
        """Run ``operation(attempt_number)`` until success or exhaustion."""

        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation(attempt)
            except Exception as error:
                if not is_retryable(error) or attempt >= self.max_attempts:
                    raise
                delay = self.delay_for(
                    attempt,
                    retry_after_seconds=getattr(error, "retry_after_seconds", None),
                    random_value=random_value() if random_value is not None else None,
                )
                if retry_observer is not None:
                    retry_observer(attempt, delay, error)
                if delay:
                    sleep(delay)
        raise AssertionError("retry loop exited without returning or raising")
