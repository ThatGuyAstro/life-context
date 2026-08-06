from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.context_server.application import create_runtime_app
from services.context_server.config import Settings


def _settings_payload(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"settings", "privacy_warning"}
    assert payload["privacy_warning"]
    return payload["settings"]


@pytest.fixture
def runtime_app(tmp_path: Path) -> Iterator[tuple[TestClient, FastAPI]]:
    app = create_runtime_app(Settings(data_dir=tmp_path, docs_enabled=False))
    with TestClient(app) as client:
        yield client, app


def test_get_config_returns_server_settings_and_supports_key_lookup(
    runtime_app: tuple[TestClient, FastAPI],
) -> None:
    client, _ = runtime_app

    response = client.get("/v1/config")
    settings = _settings_payload(response)

    assert settings["worker_count"] == 1
    assert settings["queue_size"] == 1000
    assert settings["retention"] == {
        "raw_hours": 24.0,
        "keyframe_days": 30.0,
        "derived": "indefinite",
    }
    assert settings["source_routing"] == {
        "clipboard": {"route": "local", "enabled": True},
        "screenshots": {"route": "local", "enabled": True},
    }

    keyed = client.get("/v1/config", params={"key": "queue_size"})
    assert keyed.status_code == 200
    assert keyed.json()["key"] == "queue_size"
    assert keyed.json()["value"] == 1000


def test_post_config_updates_source_routing_and_retention_windows(
    runtime_app: tuple[TestClient, FastAPI],
) -> None:
    client, _ = runtime_app
    source_routing = {
        "clipboard": {"route": "local-only", "enabled": True},
        "screenshots": {"route": "redacted-archive", "enabled": False},
        "browser": {"route": "local", "enabled": True},
    }

    response = client.post(
        "/v1/config",
        json={
            "operation": "update",
            "settings": {
                "source_routing": source_routing,
                "retention": {"raw_hours": 12.5, "keyframe_days": 9.0},
            },
        },
    )
    settings = _settings_payload(response)

    assert settings["source_routing"] == source_routing
    assert settings["retention"]["raw_hours"] == pytest.approx(12.5)
    assert settings["retention"]["keyframe_days"] == pytest.approx(9.0)

    re_read = _settings_payload(client.get("/v1/config"))
    assert re_read["source_routing"] == source_routing
    assert re_read["retention"]["raw_hours"] == pytest.approx(12.5)
    assert re_read["retention"]["keyframe_days"] == pytest.approx(9.0)


def test_post_config_updates_worker_and_queue_settings(
    runtime_app: tuple[TestClient, FastAPI],
) -> None:
    client, _ = runtime_app

    response = client.post(
        "/v1/config",
        json={
            "operation": "update",
            "settings": {
                "capture_interval_seconds": 2.5,
                "worker_count": 4,
                "queue_size": 250,
            },
        },
    )
    settings = _settings_payload(response)

    assert settings["capture_interval_seconds"] == pytest.approx(2.5)
    assert settings["worker_count"] == 4
    assert settings["queue_size"] == 250

    status = client.get("/v1/status")
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["worker_count"] == 4
    assert status_payload["queue_size"] == 250


def test_post_config_updates_model_endpoint_and_model_metadata(
    runtime_app: tuple[TestClient, FastAPI],
) -> None:
    client, _ = runtime_app

    first = client.post(
        "/v1/config",
        json={
            "operation": "update",
            "settings": {
                "model_endpoint": "https://model-a.invalid/v1",
                "model_name": "vision-a",
                "model_revision": "revision-a",
            },
        },
    )
    assert _settings_payload(first)["model_endpoint"] == "https://model-a.invalid/v1"

    second = client.post(
        "/v1/config",
        json={
            "operation": "update",
            "settings": {
                "model_endpoint": "https://model-b.invalid/v1",
                "model_name": "vision-b",
                "model_revision": "revision-b",
            },
        },
    )
    settings = _settings_payload(second)

    assert settings["model_endpoint"] == "https://model-b.invalid/v1"
    assert settings["model_name"] == "vision-b"
    assert settings["model_revision"] == "revision-b"

    model = client.get("/v1/status").json()["model"]
    assert model["endpoint"] == "https://model-b.invalid/v1"
    assert model["name"] == "vision-b"
    assert model["revision"] == "revision-b"
    assert model["configured"] is True


@pytest.mark.parametrize(
    "model_endpoint",
    [
        "http://remote.example/v1",
        "not-a-url",
        "ftp://model.example/v1",
    ],
)
def test_post_config_rejects_unsafe_or_malformed_model_endpoints(
    runtime_app: tuple[TestClient, FastAPI], model_endpoint: str
) -> None:
    client, _ = runtime_app

    response = client.post(
        "/v1/config",
        json={"operation": "update", "settings": {"model_endpoint": model_endpoint}},
    )

    assert response.status_code == 422
    assert "model_endpoint" in response.json()["detail"]
    assert _settings_payload(client.get("/v1/config"))["model_endpoint"] == ""


def test_post_config_redacts_model_url_userinfo_and_query_without_persisting_secrets(
    runtime_app: tuple[TestClient, FastAPI],
) -> None:
    client, _ = runtime_app
    endpoint = "https://user:password@model.example/v1?api_key=query-secret#fragment"

    response = client.post(
        "/v1/config",
        json={"operation": "update", "settings": {"model_endpoint": endpoint}},
    )

    assert response.status_code == 200
    settings = _settings_payload(response)
    assert settings["model_endpoint"] == "https://model.example/v1"
    serialized = response.text
    assert "user" not in serialized
    assert "password" not in serialized
    assert "query-secret" not in serialized


def test_post_config_persists_across_runtime_reload(tmp_path: Path) -> None:
    """A successful API update remains the source of truth after restart."""

    settings = Settings(data_dir=tmp_path, docs_enabled=False)
    update = {
        "source_routing": {
            "clipboard": {"route": "local-only", "enabled": True},
            "screenshots": {"route": "archive", "enabled": False},
        },
        "retention": {"raw_hours": 6.0, "keyframe_days": 3.0},
        "worker_count": 2,
        "queue_size": 64,
        "model_structured_output_mode": "json_object",
    }

    first_app = create_runtime_app(settings)
    with TestClient(first_app) as client:
        updated = client.post(
            "/v1/config",
            json={"operation": "update", "settings": update},
        )
        updated_settings = _settings_payload(updated)
        assert updated_settings["source_routing"] == update["source_routing"]
        assert updated_settings["retention"] == update["retention"]
        assert updated_settings["worker_count"] == update["worker_count"]
        assert updated_settings["queue_size"] == update["queue_size"]
        assert (
            updated_settings["model_structured_output_mode"]
            == update["model_structured_output_mode"]
        )

    reloaded_app = create_runtime_app(settings)
    with TestClient(reloaded_app) as client:
        reloaded = _settings_payload(client.get("/v1/config"))

    assert reloaded["source_routing"] == update["source_routing"]
    assert reloaded["retention"]["raw_hours"] == pytest.approx(6.0)
    assert reloaded["retention"]["keyframe_days"] == pytest.approx(3.0)
    assert reloaded["worker_count"] == 2
    assert reloaded["queue_size"] == 64
    assert reloaded["model_structured_output_mode"] == "json_object"
