"""Transport-specific exceptions."""

from __future__ import annotations

from typing import Any


class TransportError(Exception):
    """Base class for failures that can occur while delivering queued data."""

    retryable = False


class RetryableTransportError(TransportError):
    retryable = True


class HttpTransportError(TransportError):
    """An HTTP response outside the caller's accepted status set."""

    def __init__(
        self,
        status_code: int,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        attempts: int = 1,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        detail = f"HTTP {status_code}"
        if body not in (None, "", b""):
            detail += f": {body}"
        super().__init__(detail)


class SpoolError(TransportError):
    """The local durable spool could not persist or decode an item."""


class SpoolCorruptionError(SpoolError):
    """A spool item is not valid JSON or has an invalid contract envelope."""


class ReplayError(TransportError):
    """Raised when strict replay is requested and one or more items fail."""

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        super().__init__(f"replay failed for {len(errors)} item(s)")
