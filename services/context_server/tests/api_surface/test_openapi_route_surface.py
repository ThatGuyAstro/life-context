from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from contracts import CONTRACT_MODELS

from services.context_server.application import create_runtime_app
from services.context_server.config import Settings


# This is the public surface described by README.md's Interfaces section.
# Keep the versioned list authoritative here and derive the localhost aliases
# below so a newly added /v1 route cannot silently miss its short URL.
PLANNED_V1_ROUTES = frozenset(
    {
        ("GET", "/v1/health"),
        ("GET", "/v1/health/live"),
        ("GET", "/v1/health/ready"),
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/status"),
        ("POST", "/v1/query"),
        ("GET", "/v1/timeline"),
        ("GET", "/v1/observations"),
        ("POST", "/v1/observations"),
        ("POST", "/v1/observation"),
        ("POST", "/v1/start"),
        ("POST", "/v1/pause"),
        ("POST", "/v1/purge"),
        ("GET", "/v1/config"),
        ("POST", "/v1/config"),
        ("GET", "/v1/events"),
        ("GET", "/v1/events/stream"),
        ("POST", "/v1/ingest/batches"),
        ("POST", "/v1/ingest/batch"),
        ("PUT", "/v1/artifacts/{digest}"),
        ("GET", "/v1/artifacts/{digest}"),
        ("WEBSOCKET", "/v1/events/ws"),
    }
)


def _short_path(versioned_path: str) -> str:
    short_path = versioned_path.removeprefix("/v1")
    return short_path or "/"


PLANNED_LOCALHOST_ROUTES = frozenset(
    {(method, _short_path(path)) for method, path in PLANNED_V1_ROUTES}
)


@pytest.fixture
def runtime_app(tmp_path):
    app = create_runtime_app(
        Settings(
            data_dir=tmp_path,
            docs_enabled=True,
        )
    )
    try:
        yield app
    finally:
        runtime = getattr(app.state, "context_runtime", None)
        if runtime is not None:
            runtime.shutdown()


def _route_surface(app: Any) -> set[tuple[str, str]]:
    surface: set[tuple[str, str]] = set()

    def visit(routes: Iterable[Any], prefix: str = "") -> None:
        for route in routes:
            included_router = getattr(route, "original_router", None)
            if included_router is not None:
                context = getattr(route, "include_context", None)
                nested_prefix = prefix + str(getattr(context, "prefix", ""))
                visit(getattr(included_router, "routes", ()), nested_prefix)
                continue

            path = getattr(route, "path", None)
            if not path:
                continue
            path = prefix + path

            methods = getattr(route, "methods", None)
            if methods:
                surface.update((str(method).upper(), path) for method in methods)
                continue

            # FastAPI/Starlette websocket routes are not represented in OpenAPI,
            # so they must be discovered directly from the app route registry.
            if route.__class__.__name__ in {"APIWebSocketRoute", "WebSocketRoute"}:
                surface.add(("WEBSOCKET", path))

    visit(getattr(app, "routes", ()))
    return surface


def _http_routes(routes: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(method, path) for method, path in routes if method != "WEBSOCKET"}


def _operation(spec: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    return spec["paths"][path][method.lower()]


def _resolve_schema(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        reference = schema["$ref"]
        name = reference.rsplit("/", 1)[-1]
        return components["schemas"][name]
    return schema


def _schema_properties(schema: dict[str, Any], components: dict[str, Any]) -> set[str]:
    resolved = _resolve_schema(schema, components)
    properties = set(resolved.get("properties", {}))
    for branch in ("allOf", "anyOf", "oneOf"):
        for child in resolved.get(branch, []):
            properties.update(_schema_properties(child, components))
    return properties


def _request_schema(
    spec: dict[str, Any],
    components: dict[str, Any],
    method: str,
    path: str,
) -> set[str]:
    request_body = _operation(spec, method, path).get("requestBody", {})
    content = request_body.get("content", {})
    schema = content.get("application/json", {}).get("schema", {})
    return _schema_properties(schema, components)


def _response_schema(
    spec: dict[str, Any],
    components: dict[str, Any],
    method: str,
    path: str,
    status_code: str = "200",
) -> set[str]:
    response = _operation(spec, method, path)["responses"][status_code]
    content = response.get("content", {})
    schema = next(iter(content.values()), {}).get("schema", {})
    return _schema_properties(schema, components)


def test_all_planned_v1_routes_have_localhost_compatibility_aliases(runtime_app) -> None:
    surface = _route_surface(runtime_app)

    missing_v1 = sorted(PLANNED_V1_ROUTES - surface)
    missing_localhost = sorted(PLANNED_LOCALHOST_ROUTES - surface)

    assert not missing_v1, f"planned /v1 routes missing from app registry: {missing_v1}"
    assert not missing_localhost, (
        "planned localhost compatibility aliases missing from app registry: "
        f"{missing_localhost}"
    )


def test_openapi_documents_every_planned_http_route(runtime_app) -> None:
    spec = runtime_app.openapi()
    documented = {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }

    missing_v1 = sorted(_http_routes(PLANNED_V1_ROUTES) - documented)
    missing_localhost = sorted(_http_routes(PLANNED_LOCALHOST_ROUTES) - documented)

    assert not missing_v1, f"planned /v1 HTTP routes missing from OpenAPI: {missing_v1}"
    assert not missing_localhost, (
        "planned localhost HTTP aliases missing from OpenAPI: " f"{missing_localhost}"
    )


def test_artifact_and_event_stream_routes_are_discoverable(runtime_app) -> None:
    surface = _route_surface(runtime_app)
    spec = runtime_app.openapi()

    assert ("PUT", "/v1/artifacts/{digest}") in surface
    assert ("GET", "/v1/artifacts/{digest}") in surface
    assert ("GET", "/v1/events") in surface
    assert ("GET", "/v1/events/stream") in surface
    assert ("WEBSOCKET", "/v1/events/ws") in surface

    assert "/v1/artifacts/{digest}" in spec["paths"]
    assert {"get", "put"} <= set(spec["paths"]["/v1/artifacts/{digest}"])
    assert "/v1/events" in spec["paths"]
    assert "/v1/events/stream" in spec["paths"]
    assert "get" in spec["paths"]["/v1/events/stream"]


def test_openapi_exposes_every_shared_contract_schema_and_field(runtime_app) -> None:
    """The API document must publish the same fields as packages/contracts."""

    spec = runtime_app.openapi()

    schemas = spec.get("components", {}).get("schemas", {})
    missing_models: list[str] = []
    missing_fields: dict[str, list[str]] = {}
    for model in CONTRACT_MODELS:
        model_name = model.__name__
        schema = schemas.get(model_name)
        if schema is None:
            missing_models.append(model_name)
            continue
        expected = set(model.model_json_schema().get("properties", {}))
        actual = set(schema.get("properties", {}))
        if expected - actual:
            missing_fields[model_name] = sorted(expected - actual)

    assert not missing_models, f"shared contract schemas missing from OpenAPI: {missing_models}"
    assert not missing_fields, f"shared contract fields missing from OpenAPI: {missing_fields}"


@pytest.mark.parametrize(
    ("label", "method", "path", "schema_side", "model"),
    (
        ("query request", "POST", "/v1/query", "request", "QueryRequest"),
        ("query response", "POST", "/v1/query", "response", "QueryResponse"),
        ("ingest request", "POST", "/v1/ingest/batches", "request", "CaptureEnvelope"),
        ("ingest compatibility request", "POST", "/v1/ingest/batch", "request", "CaptureEnvelope"),
        ("observation request", "POST", "/v1/observations", "request", "ObservationV1"),
        ("observation collection response", "GET", "/v1/observations", "response", "QueryResponse"),
        ("health response", "GET", "/v1/health", "response", "HealthSnapshot"),
        ("artifact write response", "PUT", "/v1/artifacts/{digest}", "response", "ArtifactRef"),
    ),
)
def test_public_operation_schema_exposes_shared_contract_fields(
    runtime_app,
    label: str,
    method: str,
    path: str,
    schema_side: str,
    model: str,
) -> None:
    spec = runtime_app.openapi()
    components = spec.get("components", {})
    contract_model = next(item for item in CONTRACT_MODELS if item.__name__ == model)
    expected = set(contract_model.model_json_schema().get("properties", {}))

    if schema_side == "request":
        actual = _request_schema(spec, components, method, path)
    else:
        actual = _response_schema(spec, components, method, path, "201" if method == "PUT" else "200")

    assert expected <= actual, (
        f"{label} at {method} {path} does not expose shared {model} fields; "
        f"missing={sorted(expected - actual)} actual={sorted(actual)}"
    )
