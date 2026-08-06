"""Context query and lightweight lifecycle API routes.

FastAPI is optional at import time.  When it is installed, ``router`` is a
normal ``APIRouter``; when it is not, the handler functions and a tiny route
registry remain importable for contract and unit tests.  No route owns a
database, embedding model, or answer model: all of those are supplied in
``RouteServices`` by the application composition root.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Mapping
from typing import Any

from services.context_server.query.interfaces import RouteServices
from services.context_server.query.models import QueryRequest, QueryResponse, jsonable
from services.context_server.query.stream import Event, event_to_sse
from services.context_server.query.utils import acall_compatible, get_callable

from ..schemas import (
    PublicObservationRequest,
    PublicQueryRequest,
    PublicQueryResponse,
    PublicTimelineResponse,
)

try:  # pragma: no cover - exercised when the API extra is installed
    from fastapi import (
        APIRouter,
        Body,
        Depends,
        HTTPException,
        Request,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.responses import StreamingResponse

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - the fallback is covered in minimal envs
    FASTAPI_AVAILABLE = False

    class _FallbackRouter:
        def __init__(self) -> None:
            self.routes: list[dict[str, Any]] = []

        def _register(self, method: str, path: str, **kwargs: Any):
            def decorator(function: Any) -> Any:
                self.routes.append({"method": method, "path": path, "endpoint": function, **kwargs})
                return function

            return decorator

        def get(self, path: str, **kwargs: Any):
            return self._register("GET", path, **kwargs)

        def post(self, path: str, **kwargs: Any):
            return self._register("POST", path, **kwargs)

        def websocket(self, path: str, **kwargs: Any):
            return self._register("WEBSOCKET", path, **kwargs)

    class _Dependency:
        def __init__(self, dependency: Any) -> None:
            self.dependency = dependency

    def Depends(dependency: Any) -> Any:  # type: ignore[misc]
        return _Dependency(dependency)

    def Body(default: Any = None, **_: Any) -> Any:  # type: ignore[misc]
        return default

    class Request:  # type: ignore[no-redef]
        pass

    class WebSocket:  # type: ignore[no-redef]
        pass

    class WebSocketDisconnect(Exception):
        pass

    class StreamingResponse:  # type: ignore[no-redef]
        def __init__(self, content: Any, *, media_type: str | None = None, **kwargs: Any) -> None:
            self.body_iterator = content
            self.media_type = media_type
            self.headers = kwargs

    APIRouter = _FallbackRouter  # type: ignore[misc,assignment]


router = APIRouter(tags=["query"])
_configured_services: RouteServices | Any | None = None


def configure_services(services: RouteServices | Any) -> RouteServices | Any:
    """Set process-local route dependencies for direct mounting/smoke tests."""

    global _configured_services
    _configured_services = services
    return services


def _state_services(request: Any) -> Any | None:
    state = getattr(getattr(request, "app", None), "state", None)
    if state is None:
        return None
    for name in ("context_services", "route_services", "services"):
        candidate = getattr(state, name, None)
        if candidate is not None:
            return candidate
    return None


def get_services(request: Request = None) -> RouteServices | Any:
    """FastAPI dependency and direct-call resolver for route services."""

    candidate = _state_services(request) if request is not None else None
    candidate = candidate or _configured_services
    if candidate is None and request is not None:
        app = getattr(request, "app", None)
        lifecycle = getattr(getattr(app, "state", None), "lifecycle", None)
        if lifecycle is not None:
            candidate = RouteServices(lifecycle=lifecycle)
    if candidate is None:
        raise RuntimeError("context route services have not been configured")
    return candidate


def _service_value(services: Any, names: tuple[str, ...]) -> Any:
    if isinstance(services, Mapping):
        for name in names:
            if name in services:
                return services[name]
    for name in names:
        value = getattr(services, name, None)
        if value is not None:
            return value
    return None


def _normalize_payload(value: Any) -> Any:
    if isinstance(value, QueryResponse):
        return value.to_dict()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return jsonable(value)


def _mapping_payload(value: Any) -> dict[str, Any]:
    """Convert a validated Pydantic body or mapping to a JSON-shaped mapping."""

    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return dict(model_dump(mode="json", exclude_unset=True))
        except TypeError:
            return dict(model_dump(exclude_unset=True))
    return dict(value)


async def _call_target(
    target: Any, names: tuple[str, ...], payload: Any = None, *, no_args: bool = False
) -> Any:
    method = get_callable(target, names)
    if method is None:
        raise RuntimeError(f"route dependency does not implement any of: {', '.join(names)}")
    if no_args:
        result = method()
        if inspect.isawaitable(result):
            result = await result
    else:
        options = payload if isinstance(payload, Mapping) else {}
        query = payload if isinstance(payload, str) else None
        result = await acall_compatible(method, payload, query=query, options=options)
    return _normalize_payload(result)


async def handle_query(payload: Any, services: RouteServices | Any) -> dict[str, Any]:
    request_payload = payload if isinstance(payload, Mapping) else _mapping_payload(payload)
    request = QueryRequest.from_value(request_payload or {})
    target = _service_value(services, ("query_service", "query", "orchestrator"))
    if target is None:
        raise RuntimeError("query service is not configured")
    method = get_callable(target, ("aquery", "query"))
    if method is None:
        raise RuntimeError("query service does not implement query/aquery")
    result = await acall_compatible(method, request, query=request.query, options=request.to_dict())
    return _normalize_payload(result)


async def handle_timeline(options: Mapping[str, Any] | None, services: RouteServices | Any) -> Any:
    target = _service_value(services, ("timeline_provider", "timeline", "context_store"))
    return await _call_target(
        target,
        ("timeline", "list_timeline", "query_timeline", "read_timeline"),
        dict(options or {}),
    )


async def handle_observation(observation: Mapping[str, Any], services: RouteServices | Any) -> Any:
    target = _service_value(services, ("observation_store", "observations", "context_store"))
    return await _call_target(
        target,
        ("observe", "record", "record_observation", "append_observation", "create"),
        _mapping_payload(observation),
    )


async def handle_status(services: RouteServices | Any) -> Any:
    target = _service_value(services, ("status_provider", "status", "lifecycle", "context_store"))
    method = get_callable(target, ("status", "health", "get_status"))
    if method is not None:
        return _normalize_payload(await acall_compatible(method, None, options={}))
    state = getattr(target, "state", None)
    snapshot = get_callable(state, ("snapshot",))
    if snapshot is not None:
        return _normalize_payload(await acall_compatible(snapshot, None, options={}))
    raise RuntimeError("route dependency does not implement status/health/get_status")


async def handle_start(options: Mapping[str, Any] | None, services: RouteServices | Any) -> Any:
    target = _service_value(services, ("lifecycle", "controller", "status_provider"))
    return await _call_target(target, ("start", "resume", "startup"), dict(options or {}))


async def handle_pause(services: RouteServices | Any) -> Any:
    target = _service_value(services, ("lifecycle", "controller", "status_provider"))
    return await _call_target(target, ("pause", "stop", "shutdown"), no_args=True)


async def handle_purge(options: Mapping[str, Any] | None, services: RouteServices | Any) -> Any:
    target = _service_value(services, ("purge_store", "purge", "context_store"))
    return await _call_target(target, ("purge", "delete_before", "clear"), dict(options or {}))


async def handle_config(operation: Mapping[str, Any] | None, services: RouteServices | Any) -> Any:
    target = _service_value(services, ("config_provider", "config", "settings"))
    return await _call_target(
        target, ("config", "get_config", "update_config", "get", "set"), dict(operation or {})
    )


async def _event_items(stream: Any, topic: str | None = None) -> AsyncIterator[Any]:
    if stream is None:
        yield Event(type="ready", data={"stream": "stub", "message": "no event source configured"})
        return

    source = None
    for name in ("astream", "stream", "subscribe"):
        candidate = getattr(stream, name, None)
        if callable(candidate):
            source = candidate
            break
    if source is None and callable(stream):
        source = stream
    if source is None:
        yield Event(type="ready", data={"stream": "stub"})
        return

    try:
        result = source(topic) if topic is not None else source()
    except TypeError:
        result = source()
    if inspect.isawaitable(result):
        result = await result
    if hasattr(result, "__aiter__"):
        iterator = result.__aiter__()
        try:
            async for item in iterator:
                yield item
        finally:
            await _aclose_async_iterator(iterator)
    else:
        for item in result or ():
            yield item


async def _aclose_async_iterator(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _query_params(request: Any) -> dict[str, Any]:
    params = getattr(request, "query_params", None)
    if params is None:
        return {}
    try:
        return dict(params)
    except (TypeError, ValueError):
        return {}


if FASTAPI_AVAILABLE:
    from services.context_server.api.security import require_request_auth, require_websocket_auth

    @router.post(
        "/query",
        response_model=PublicQueryResponse,
        dependencies=[Depends(require_request_auth)],
    )
    async def query_route(
        payload: PublicQueryRequest = Body(...), services: Any = Depends(get_services)
    ) -> dict[str, Any]:
        return await handle_query(payload, services)

    @router.get(
        "/timeline",
        response_model=PublicTimelineResponse,
        dependencies=[Depends(require_request_auth)],
    )
    async def timeline_route(request: Request, services: Any = Depends(get_services)) -> Any:
        options = _query_params(request) if isinstance(request, Request) else dict(request)
        return await handle_timeline(options, services)

    @router.post("/observations", dependencies=[Depends(require_request_auth)])
    async def observation_route(
        payload: PublicObservationRequest = Body(...), services: Any = Depends(get_services)
    ) -> Any:
        return await handle_observation(payload or {}, services)

    @router.post("/observation", dependencies=[Depends(require_request_auth)])
    async def observation_alias_route(
        payload: PublicObservationRequest = Body(...), services: Any = Depends(get_services)
    ) -> Any:
        return await handle_observation(payload or {}, services)

    @router.get("/status", dependencies=[Depends(require_request_auth)])
    async def status_route(services: Any = Depends(get_services)) -> Any:
        return await handle_status(services)

    @router.post("/start", dependencies=[Depends(require_request_auth)])
    async def start_route(
        payload: Any = Body(default={}), services: Any = Depends(get_services)
    ) -> Any:
        return await handle_start(payload or {}, services)

    @router.post("/pause", dependencies=[Depends(require_request_auth)])
    async def pause_route(services: Any = Depends(get_services)) -> Any:
        return await handle_pause(services)

    @router.post("/purge", dependencies=[Depends(require_request_auth)])
    async def purge_route(
        payload: Any = Body(default={}), services: Any = Depends(get_services)
    ) -> Any:
        return await handle_purge(payload or {}, services)

    @router.get("/config", dependencies=[Depends(require_request_auth)])
    async def config_get_route(request: Request, services: Any = Depends(get_services)) -> Any:
        options = _query_params(request) if isinstance(request, Request) else dict(request)
        return await handle_config({"operation": "get", **options}, services)

    @router.post("/config", dependencies=[Depends(require_request_auth)])
    async def config_route(
        payload: Any = Body(default={}), services: Any = Depends(get_services)
    ) -> Any:
        try:
            return await handle_config(payload or {}, services)
        except ValueError as error:
            # Config validation errors are client input errors, not server
            # failures.  Keep the response detail free of raw URL userinfo.
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/events/stream", dependencies=[Depends(require_request_auth)])
    @router.get("/events", dependencies=[Depends(require_request_auth)])
    async def events_route(
        request: Request, services: Any = Depends(get_services)
    ) -> StreamingResponse:
        topic = _query_params(request).get("topic")
        stream = _service_value(services, ("event_stream", "events", "event_source"))

        async def body() -> AsyncIterator[str]:
            items = _event_items(stream, topic)
            try:
                async for item in items:
                    yield event_to_sse(item)
            finally:
                await _aclose_async_iterator(items)

        return StreamingResponse(
            body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @router.websocket("/events/ws")
    async def events_websocket(websocket: WebSocket) -> None:
        require_websocket_auth(websocket)
        await websocket.accept()
        services = get_services(websocket)
        topic = getattr(websocket, "query_params", {}).get("topic")
        stream = _service_value(services, ("event_stream", "events", "event_source"))

        async def send_events() -> None:
            items = _event_items(stream, topic)
            try:
                async for item in items:
                    payload = _normalize_payload(item)
                    if hasattr(websocket, "send_json"):
                        await websocket.send_json(payload)
                    else:
                        await websocket.send_text(event_to_sse(item))
            finally:
                await _aclose_async_iterator(items)

        async def wait_for_disconnect() -> None:
            while True:
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    return
                if isinstance(message, Mapping) and message.get("type") == "websocket.disconnect":
                    return

        stream_task = asyncio.create_task(send_events(), name="context-event-stream")
        disconnect_task = asyncio.create_task(
            wait_for_disconnect(), name="context-event-disconnect"
        )
        try:
            done, _ = await asyncio.wait(
                {stream_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream_task in done:
                await stream_task
        except WebSocketDisconnect:
            return
        finally:
            for task in (stream_task, disconnect_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream_task, disconnect_task, return_exceptions=True)

else:

    async def query_route(payload: Any = None, services: Any = None) -> dict[str, Any]:
        return await handle_query(payload or {}, get_services() if services is None else services)

    async def timeline_route(options: Mapping[str, Any] | None = None, services: Any = None) -> Any:
        return await handle_timeline(options, get_services() if services is None else services)

    async def observation_route(
        payload: Mapping[str, Any] | None = None, services: Any = None
    ) -> Any:
        return await handle_observation(
            payload or {}, get_services() if services is None else services
        )

    async def observation_alias_route(
        payload: Mapping[str, Any] | None = None, services: Any = None
    ) -> Any:
        return await handle_observation(
            payload or {}, get_services() if services is None else services
        )

    async def status_route(services: Any = None) -> Any:
        return await handle_status(get_services() if services is None else services)

    async def start_route(payload: Mapping[str, Any] | None = None, services: Any = None) -> Any:
        return await handle_start(payload or {}, get_services() if services is None else services)

    async def pause_route(services: Any = None) -> Any:
        return await handle_pause(get_services() if services is None else services)

    async def purge_route(payload: Mapping[str, Any] | None = None, services: Any = None) -> Any:
        return await handle_purge(payload or {}, get_services() if services is None else services)

    async def config_route(payload: Mapping[str, Any] | None = None, services: Any = None) -> Any:
        return await handle_config(payload or {}, get_services() if services is None else services)

    async def events_route(services: Any = None, topic: str | None = None) -> Any:
        stream = _service_value(
            get_services() if services is None else services,
            ("event_stream", "events", "event_source"),
        )
        return _event_items(stream, topic)

    async def events_websocket(
        websocket: Any, services: Any = None, topic: str | None = None
    ) -> None:
        if hasattr(websocket, "accept"):
            await websocket.accept()
        stream = _service_value(
            get_services() if services is None else services,
            ("event_stream", "events", "event_source"),
        )
        async for item in _event_items(stream, topic):
            if hasattr(websocket, "send_json"):
                await websocket.send_json(_normalize_payload(item))

    # Keep the fallback router useful for introspection and simple adapters.
    for _method, _path, _endpoint in (
        ("POST", "/query", query_route),
        ("GET", "/timeline", timeline_route),
        ("POST", "/observations", observation_route),
        ("GET", "/status", status_route),
        ("POST", "/start", start_route),
        ("POST", "/pause", pause_route),
        ("POST", "/purge", purge_route),
        ("POST", "/config", config_route),
        ("GET", "/events", events_route),
        ("WEBSOCKET", "/events/ws", events_websocket),
    ):
        router.routes.append({"method": _method, "path": _path, "endpoint": _endpoint})


query = query_route
timeline = timeline_route
observation = observation_route
status = status_route
start = start_route
pause = pause_route
purge = purge_route
config = config_route
events = events_route


__all__ = [
    "FASTAPI_AVAILABLE",
    "RouteServices",
    "configure_services",
    "config",
    "events",
    "get_services",
    "handle_config",
    "handle_observation",
    "handle_pause",
    "handle_purge",
    "handle_query",
    "handle_start",
    "handle_status",
    "handle_timeline",
    "observation",
    "pause",
    "purge",
    "query",
    "router",
    "start",
    "status",
    "timeline",
]
