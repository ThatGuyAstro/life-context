"""Runtime status placeholder endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(tags=["status"])


@router.get("/status", name="status")
async def status(request: Request) -> dict[str, Any]:
    """Return process/application state with runtime operational metrics.

    The runtime-backed ``backlog`` object intentionally extends the shared
    ``BacklogStatus`` counters with derived age and dead-letter fields.  This
    endpoint is an operational status payload, not a strict serialization of
    ``HealthSnapshot.backlog``.
    """

    settings = request.app.state.settings
    runtime = getattr(request.app.state, "context_runtime", None)
    if runtime is not None:
        return runtime.status()
    lifecycle = request.app.state.lifecycle
    return {
        "service": settings.service_name,
        "version": settings.version,
        "environment": settings.environment,
        **lifecycle.state.snapshot(),
    }


__all__ = ["router"]
