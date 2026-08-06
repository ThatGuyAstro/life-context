from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient

from services.context_server.app import create_app
from services.context_server.config import Settings


def test_factory_exposes_foundation_routes_and_lifecycle() -> None:
    app = create_app(
        Settings(service_name="test-context", version="9.1.0"),
    )

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/health/ready")
        capabilities = client.get("/capabilities")
        status = client.get("/status")

        assert health.status_code == 200
        assert health.json()["service"] == "test-context"
        assert ready.status_code == 200
        assert capabilities.json()["capabilities"] == []
        assert status.json()["status"] == "running"

    assert app.state.lifecycle.state.status == "stopped"


def test_feature_router_can_be_mounted_without_registry_changes() -> None:
    feature_router = APIRouter(prefix="/feature", tags=["feature"])

    @feature_router.get("/placeholder")
    async def placeholder() -> dict[str, str]:
        return {"status": "ok"}

    app = create_app(
        Settings(api_prefix="/api"),
        routers=[feature_router],
    )

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/feature/placeholder").json() == {"status": "ok"}
