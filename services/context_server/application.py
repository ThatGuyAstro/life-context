"""Production composition root for the Life Context server."""

from __future__ import annotations

from .api.router import default_routers, register_routers
from .api.routes import ingest, query
from .app import create_app
from .config import Settings, get_settings
from .query.interfaces import RouteServices
from .runtime import ContextRuntime


def create_runtime_app(settings: Settings | None = None):
    resolved = settings or get_settings()
    runtime = ContextRuntime(resolved)
    services = RouteServices(
        query=runtime.search,
        timeline=runtime,
        observation=runtime,
        status=runtime,
        lifecycle=runtime,
        purge=runtime,
        config=runtime,
        events=runtime.events,
    )
    feature_routers = (query.router, ingest.router)
    app = create_app(
        resolved,
        routers=feature_routers,
        startup_hooks=(runtime.startup,),
        shutdown_hooks=(runtime.shutdown,),
    )
    app.state.context_runtime = runtime
    app.state.context_services = services

    # Keep the short localhost URLs used by the initial CLI/dashboard and
    # expose the versioned contract URLs required for remote/self-hosted use.
    if not resolved.api_prefix:
        register_routers(app, default_routers() + feature_routers, prefix="/v1")

    from .api.routes.query import configure_services

    configure_services(services)
    return app


app = create_runtime_app()


__all__ = ["app", "create_runtime_app"]
