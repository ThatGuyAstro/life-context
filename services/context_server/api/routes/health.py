"""Liveness and readiness placeholder endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..schemas import PublicHealthResponse

router = APIRouter(tags=["health"])


def _context(request: Request) -> tuple[Any, Any]:
    settings = request.app.state.settings
    lifecycle = request.app.state.lifecycle
    return settings, lifecycle


@router.get("/health", name="health", response_model=PublicHealthResponse)
async def health(request: Request) -> dict[str, Any]:
    """Return the stable legacy health response used by local clients.

    The public response model also publishes the transport-neutral
    ``HealthSnapshot`` contract, but this endpoint intentionally preserves its
    existing ``ok``/``ready`` wire shape for compatibility.
    """

    settings, lifecycle = _context(request)
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": settings.version,
        "ready": lifecycle.state.ready,
    }


@router.get("/health/live", name="health_live")
async def health_live(request: Request) -> dict[str, Any]:
    """Return liveness without requiring feature dependencies."""

    settings, _ = _context(request)
    return {"status": "ok", "service": settings.service_name}


@router.get("/health/ready", name="health_ready")
async def health_ready(request: Request) -> dict[str, Any]:
    """Return readiness once the application lifespan has started."""

    settings, lifecycle = _context(request)
    if not lifecycle.state.ready:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "service": settings.service_name,
                "state": lifecycle.state.status,
            },
        )
    return {"status": "ok", "service": settings.service_name, "ready": True}


__all__ = ["router"]
