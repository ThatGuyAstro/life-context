"""Application lifecycle hooks and runtime state."""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


Hook = Callable[[], Any | Awaitable[Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _invoke(hook: Hook) -> None:
    result = hook()
    if inspect.isawaitable(result):
        await result


@dataclass
class RuntimeState:
    """Mutable state exposed by the status and health placeholder routes."""

    status: str = "created"
    ready: bool = False
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        current_time = now or _utc_now()
        uptime_seconds: float | None = None
        if self.started_at is not None:
            end = self.stopped_at or current_time
            uptime_seconds = max(0.0, (end - self.started_at).total_seconds())

        return {
            "status": self.status,
            "ready": self.ready,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "uptime_seconds": uptime_seconds,
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }


class Lifecycle:
    """Run registered startup and shutdown hooks in predictable order."""

    def __init__(
        self,
        *,
        startup_hooks: list[Hook] | tuple[Hook, ...] = (),
        shutdown_hooks: list[Hook] | tuple[Hook, ...] = (),
        logger: logging.Logger | None = None,
    ) -> None:
        self.state = RuntimeState()
        self._startup_hooks: list[Hook] = list(startup_hooks)
        self._shutdown_hooks: list[Hook] = list(shutdown_hooks)
        self._logger = logger or logging.getLogger("context_server.lifecycle")

    def register_startup(self, hook: Hook) -> Hook:
        """Register a hook; hooks execute in registration order."""

        self._startup_hooks.append(hook)
        return hook

    def register_shutdown(self, hook: Hook) -> Hook:
        """Register a hook; hooks execute in reverse registration order."""

        self._shutdown_hooks.append(hook)
        return hook

    async def startup(self) -> None:
        if self.state.status == "running":
            return
        if self.state.status in {"starting", "stopping"}:
            raise RuntimeError(f"cannot start lifecycle while {self.state.status}")

        self.state.status = "starting"
        self.state.last_error = None
        try:
            for hook in self._startup_hooks:
                await _invoke(hook)
        except Exception as exc:
            self.state.status = "failed"
            self.state.ready = False
            self.state.last_error = str(exc)
            self._logger.exception("context server startup failed")
            raise

        self.state.started_at = _utc_now()
        self.state.stopped_at = None
        self.state.ready = True
        self.state.status = "running"
        self._logger.info("context server started")

    async def shutdown(self) -> None:
        if self.state.status == "stopped":
            return

        self.state.status = "stopping"
        self.state.ready = False
        first_error: Exception | None = None
        for hook in reversed(self._shutdown_hooks):
            try:
                await _invoke(hook)
            except Exception as exc:  # continue cleanup while preserving failure
                first_error = first_error or exc
                self._logger.exception("context server shutdown hook failed")

        self.state.stopped_at = _utc_now()
        self.state.status = "stopped"
        if first_error is not None:
            self.state.last_error = str(first_error)
            raise first_error
        self._logger.info("context server stopped")


def lifespan_for(lifecycle: Lifecycle):
    """Create a FastAPI lifespan context around a :class:`Lifecycle`."""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await lifecycle.startup()
        try:
            yield
        finally:
            await lifecycle.shutdown()

    return lifespan


__all__ = ["Hook", "Lifecycle", "RuntimeState", "lifespan_for"]
