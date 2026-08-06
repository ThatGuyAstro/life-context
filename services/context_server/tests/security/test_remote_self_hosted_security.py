from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.context_server.application import create_runtime_app
from services.context_server.config import Settings


REMOTE_HOST = "remote.example.test"
REMOTE_TOKEN = "remote-security-test-token"
ARTIFACT_BODY = b"remote self-hosted security acceptance artifact"
ARTIFACT_DIGEST = sha256(ARTIFACT_BODY).hexdigest()
AUTH_HEADERS = {"Authorization": f"Bearer {REMOTE_TOKEN}"}


@contextmanager
def runtime_client(
    *,
    base_url: str,
    **settings_overrides: Any,
) -> Iterator[TestClient]:
    """Build a fully wired runtime app with an isolated temporary archive."""

    with TemporaryDirectory() as data_dir:
        settings = Settings(data_dir=Path(data_dir), **settings_overrides)
        app = create_runtime_app(settings)
        with TestClient(app, base_url=base_url) as client:
            yield client


def security_envelope() -> dict[str, Any]:
    return {
        "envelope_id": "security-acceptance-envelope",
        "schema_version": "1",
        "captured_at": "2026-01-01T12:01:00Z",
        "source_kind": "application",
        "producer": "security-acceptance",
        "events": [
            {
                "event_id": "security-acceptance-event",
                "source_id": "security-acceptance",
                "source_kind": "application",
                "event_type": "observed",
                "occurred_at": "2026-01-01T12:00:00Z",
                "payload": {"text": "remote self-hosted security acceptance"},
                "artifacts": [],
                "metadata": {},
            }
        ],
        "metadata": {},
    }


def manual_observation() -> dict[str, Any]:
    return {
        "event_id": "security-acceptance-manual-observation",
        "source_id": "security-acceptance",
        "text": "remote self-hosted security acceptance",
    }


# Each case represents a protected feature route.  The artifact write case
# includes its non-authentication header so a 401 cannot be confused with
# request validation.
PROTECTED_FEATURE_REQUESTS = (
    (
        "query",
        "POST",
        "/v1/query",
        {"json": {"query": "security acceptance", "mode": "fts", "limit": 5}},
        {},
    ),
    ("timeline", "GET", "/v1/timeline", {}, {}),
    ("data-read", "GET", "/v1/observations", {}, {}),
    (
        "data-write",
        "POST",
        "/v1/observations",
        {"json": manual_observation()},
        {},
    ),
    (
        "ingest",
        "POST",
        "/v1/ingest/batches",
        {"json": security_envelope()},
        {},
    ),
    (
        "ingest-compatibility-alias",
        "POST",
        "/v1/ingest/batch",
        {"json": security_envelope()},
        {},
    ),
    (
        "artifact-read",
        "GET",
        f"/v1/artifacts/{ARTIFACT_DIGEST}",
        {},
        {},
    ),
    (
        "artifact-write",
        "PUT",
        f"/v1/artifacts/{ARTIFACT_DIGEST}",
        {"content": ARTIFACT_BODY},
        {"X-Artifact-Sha256": ARTIFACT_DIGEST},
    ),
)


def send_case(
    client: TestClient,
    method: str,
    path: str,
    request_kwargs: dict[str, Any],
    base_headers: dict[str, str],
    auth_headers: dict[str, str] | None = None,
):
    headers = dict(base_headers)
    if auth_headers:
        headers.update(auth_headers)
    return client.request(method, path, headers=headers, **request_kwargs)


def test_localhost_remains_usable_without_auth() -> None:
    with runtime_client(base_url="http://localhost", host="localhost") as client:
        health = client.get("/v1/health")
        artifact_put = client.put(
            f"/v1/artifacts/{ARTIFACT_DIGEST}",
            content=ARTIFACT_BODY,
            headers={"X-Artifact-Sha256": ARTIFACT_DIGEST},
        )
        artifact_get = client.get(f"/v1/artifacts/{ARTIFACT_DIGEST}")
        ingest = client.post("/v1/ingest/batches", json=security_envelope())
        query = client.post(
            "/v1/query",
            json={"query": "security acceptance", "mode": "fts", "limit": 5},
        )
        data = client.get("/v1/observations")

    assert health.status_code == 200
    assert artifact_put.status_code == 201
    assert artifact_get.status_code == 200
    assert artifact_get.content == ARTIFACT_BODY
    assert ingest.status_code == 202
    assert query.status_code == 200
    assert data.status_code == 200


@pytest.mark.parametrize(
    ("route_name", "method", "path", "request_kwargs", "base_headers"),
    PROTECTED_FEATURE_REQUESTS,
    ids=[case[0] for case in PROTECTED_FEATURE_REQUESTS],
)
def test_non_loopback_http_is_rejected_when_https_is_required(
    route_name: str,
    method: str,
    path: str,
    request_kwargs: dict[str, Any],
    base_headers: dict[str, str],
) -> None:
    del route_name

    with runtime_client(
        base_url=f"http://{REMOTE_HOST}",
        host=REMOTE_HOST,
        auth_token=REMOTE_TOKEN,
        require_https_remote=True,
    ) as client:
        response = send_case(
            client,
            method,
            path,
            request_kwargs,
            base_headers,
            AUTH_HEADERS,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "remote self-hosted connections require HTTPS"


def test_non_loopback_feature_routes_fail_closed_without_bearer_configuration() -> None:
    with runtime_client(
        base_url=f"https://{REMOTE_HOST}",
        host=REMOTE_HOST,
        auth_token="",
        require_https_remote=True,
    ) as client:
        response = client.get("/v1/timeline")

    assert response.status_code == 401
    assert response.json()["detail"] == (
        "remote self-hosted connections require bearer authentication"
    )


def test_non_loopback_sse_is_rejected_over_http_even_with_bearer_auth() -> None:
    with runtime_client(
        base_url=f"http://{REMOTE_HOST}",
        host=REMOTE_HOST,
        auth_token=REMOTE_TOKEN,
        require_https_remote=True,
    ) as client:
        response = client.get(
            "/v1/events/stream",
            headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "remote self-hosted connections require HTTPS"


@pytest.mark.parametrize(
    "auth_headers",
    [
        pytest.param({}, id="missing-bearer"),
        pytest.param({"Authorization": "Bearer incorrect-token"}, id="incorrect-bearer"),
    ],
)
def test_remote_sse_requires_bearer_auth(auth_headers: dict[str, str]) -> None:
    with runtime_client(
        base_url=f"https://{REMOTE_HOST}",
        host=REMOTE_HOST,
        auth_token=REMOTE_TOKEN,
        require_https_remote=True,
    ) as client:
        response = client.get(
            "/v1/events/stream",
            headers={"Accept": "text/event-stream", **auth_headers},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "bearer authentication required"


@pytest.mark.parametrize(
    "auth_headers",
    [
        pytest.param({}, id="missing-bearer"),
        pytest.param({"Authorization": "Bearer incorrect-token"}, id="incorrect-bearer"),
    ],
)
@pytest.mark.parametrize(
    ("route_name", "method", "path", "request_kwargs", "base_headers"),
    PROTECTED_FEATURE_REQUESTS,
    ids=[case[0] for case in PROTECTED_FEATURE_REQUESTS],
)
def test_remote_feature_routes_require_bearer_auth(
    auth_headers: dict[str, str],
    route_name: str,
    method: str,
    path: str,
    request_kwargs: dict[str, Any],
    base_headers: dict[str, str],
) -> None:
    del route_name

    with runtime_client(
        base_url=f"https://{REMOTE_HOST}",
        host=REMOTE_HOST,
        auth_token=REMOTE_TOKEN,
        require_https_remote=True,
    ) as client:
        response = send_case(
            client,
            method,
            path,
            request_kwargs,
            base_headers,
            auth_headers,
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "bearer authentication required"


def test_correct_bearer_over_https_compatible_scope_succeeds() -> None:
    with runtime_client(
        base_url=f"https://{REMOTE_HOST}",
        host=REMOTE_HOST,
        auth_token=REMOTE_TOKEN,
        require_https_remote=True,
    ) as client:
        artifact_put = client.put(
            f"/v1/artifacts/{ARTIFACT_DIGEST}",
            content=ARTIFACT_BODY,
            headers={**AUTH_HEADERS, "X-Artifact-Sha256": ARTIFACT_DIGEST},
        )
        artifact_get = client.get(
            f"/v1/artifacts/{ARTIFACT_DIGEST}",
            headers=AUTH_HEADERS,
        )
        ingest = client.post(
            "/v1/ingest/batches",
            json=security_envelope(),
            headers=AUTH_HEADERS,
        )
        ingest_alias = client.post(
            "/v1/ingest/batch",
            json=security_envelope(),
            headers=AUTH_HEADERS,
        )
        manual = client.post(
            "/v1/observations",
            json=manual_observation(),
            headers=AUTH_HEADERS,
        )
        query = client.post(
            "/v1/query",
            json={"query": "security acceptance", "mode": "fts", "limit": 5},
            headers=AUTH_HEADERS,
        )
        timeline = client.get("/v1/timeline", headers=AUTH_HEADERS)
        data = client.get("/v1/observations", headers=AUTH_HEADERS)

    assert artifact_put.status_code == 201
    assert artifact_get.status_code == 200
    assert artifact_get.content == ARTIFACT_BODY
    assert ingest.status_code == 202
    assert ingest_alias.status_code == 202
    assert manual.status_code == 200
    assert query.status_code == 200
    assert query.json()["meta"]["result_count"] >= 1
    assert timeline.status_code == 200
    assert data.status_code == 200
    assert data.json()["total_count"] >= 1
