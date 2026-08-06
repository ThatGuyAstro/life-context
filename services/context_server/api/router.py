"""Modular HTTP router registration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI

from .routes import capabilities, health, status

if TYPE_CHECKING:
    from fastapi.routing import APIRoute


def default_routers() -> tuple[APIRouter, ...]:
    """Return the foundation routers in deterministic registration order."""

    return (health.router, capabilities.router, status.router)


def register_routers(
    app: FastAPI,
    routers: Iterable[APIRouter],
    *,
    prefix: str = "",
) -> tuple[APIRoute, ...]:
    """Include routers and return the resulting route objects.

    Feature agents can expose their own ``APIRouter`` and pass it to this
    function or to :func:`context_server.create_app`; no feature module needs
    to modify this registry.
    """

    before = len(app.routes)
    for router in routers:
        app.include_router(router, prefix=prefix)
    return tuple(route for route in app.routes[before:] if hasattr(route, "path"))


__all__ = ["default_routers", "register_routers"]
