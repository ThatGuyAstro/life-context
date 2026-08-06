"""Capability discovery placeholder endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(tags=["capabilities"])


@router.get("/capabilities", name="capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    """Describe currently registered capabilities.

    The empty list is intentional until feature agents register concrete
    capabilities through application settings or a later integration layer.
    """

    settings = request.app.state.settings
    runtime = getattr(request.app.state, "context_runtime", None)
    if runtime is not None:
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": settings.version,
            "capabilities": [
                "capture-envelopes",
                "content-addressed-artifacts",
                "sqlite-fts5",
                "hybrid-query",
                "sse-events",
                "websocket-events",
                "plaintext-local-archive",
            ],
            "config": runtime.config_operation({"operation": "get"}),
        }
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": settings.version,
        "capabilities": list(settings.capabilities),
    }


__all__ = ["router"]
