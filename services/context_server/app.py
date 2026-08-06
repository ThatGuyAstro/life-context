"""FastAPI application factory for the context server foundation."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, FastAPI

from .api.router import default_routers, register_routers
from .config import Settings, get_settings
from .lifecycle import Hook, Lifecycle, lifespan_for
from .logging import configure_logging, get_logger


def create_app(
    settings: Settings | None = None,
    *,
    routers: Iterable[APIRouter] = (),
    startup_hooks: Iterable[Hook] = (),
    shutdown_hooks: Iterable[Hook] = (),
    include_default_routers: bool = True,
) -> FastAPI:
    """Create an independently configured FastAPI application.

    ``routers`` is intentionally additive: feature agents can pass their
    routers here without changing the foundation registry.  Set
    ``include_default_routers=False`` when an embedding service wants to own
    every route while reusing the lifecycle and settings plumbing.
    """

    resolved_settings = settings or get_settings()
    logger = configure_logging(
        resolved_settings.log_level,
        json_logs=resolved_settings.json_logs,
    )
    lifecycle = Lifecycle(
        startup_hooks=list(startup_hooks),
        shutdown_hooks=list(shutdown_hooks),
        logger=get_logger("context_server.lifecycle"),
    )

    app = FastAPI(
        title=resolved_settings.service_name,
        version=resolved_settings.version,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.docs_enabled else None,
        openapi_url="/openapi.json" if resolved_settings.docs_enabled else None,
        # The shared contracts are the wire schema for both directions.  A
        # single component name keeps nested ObservationV1/ArtifactRef
        # references discoverable to generated clients without changing
        # Pydantic's request or response validation.
        separate_input_output_schemas=False,
        lifespan=lifespan_for(lifecycle),
    )
    app.state.settings = resolved_settings
    app.state.lifecycle = lifecycle
    app.state.logger = logger

    if include_default_routers:
        register_routers(
            app,
            default_routers(),
            prefix=resolved_settings.api_prefix,
        )
    register_routers(app, routers, prefix=resolved_settings.api_prefix)
    return app


app = create_app()


__all__ = ["app", "create_app"]
