from __future__ import annotations

from types import SimpleNamespace

from services.context_server.inference import runtime_worker as runtime_worker_module
from services.context_server.inference.probes import (
    CapabilityProbeResult,
    HealthProbeResult,
    ModelCapabilities,
    ProbeStatus,
)
from services.context_server.inference.runtime_worker import InferenceRuntimeController


class _ProbeClient:
    model_name = "default"

    def __init__(self, health: HealthProbeResult, capabilities: CapabilityProbeResult) -> None:
        self._health = health
        self._capabilities = capabilities
        self.health_calls = 0
        self.capability_calls = 0

    @property
    def provenance_metadata(self) -> dict[str, object]:
        return {
            "client_name": "default",
            "mode": "external",
            "configured_model": "local-vlm",
            "model_revision": "revision-2026-08",
            "endpoint": "http://127.0.0.1:8080/v1",
            "structured_output_mode": "json_schema",
        }

    def health_probe(self) -> HealthProbeResult:
        self.health_calls += 1
        return self._health

    def capability_probe(self) -> CapabilityProbeResult:
        self.capability_calls += 1
        return self._capabilities


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(),
        _config={
            "model_endpoint": "http://127.0.0.1:8080/v1",
            "model_name": "local-vlm",
            "model_revision": "revision-2026-08",
            "worker_count": 1,
        },
    )


def _capabilities(status: ProbeStatus) -> CapabilityProbeResult:
    return CapabilityProbeResult(
        status=status,
        endpoint="http://127.0.0.1:8080/v1/models",
        capabilities=ModelCapabilities(
            model="local-vlm",
            available_models=("local-vlm",),
            supports_json_schema=True,
            source="endpoint",
        ),
        http_status=200 if status is not ProbeStatus.UNAVAILABLE else None,
        detail="scripted capability probe",
        checked_at=1.0,
    )


def test_controller_builds_model_client_from_runtime_structured_output_setting(monkeypatch) -> None:
    runtime = _runtime()
    runtime._config["model_structured_output_mode"] = "auto"
    runtime.storage = SimpleNamespace(artifacts=object())
    captured: dict[str, object] = {}

    def build_model_client(config):
        captured["config"] = config
        return object()

    monkeypatch.setattr(runtime_worker_module, "build_model_client", build_model_client)

    controller = InferenceRuntimeController(runtime)
    controller._build_client()

    config = captured["config"]
    assert config.structured_output_mode == "auto"


def test_legacy_inference_environment_mode_still_overrides_runtime_auto(monkeypatch) -> None:
    runtime = _runtime()
    runtime._config["model_structured_output_mode"] = "auto"
    runtime.storage = SimpleNamespace(artifacts=object())
    captured: dict[str, object] = {}

    monkeypatch.setenv("CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE", "json_object")
    monkeypatch.setattr(
        runtime_worker_module,
        "build_model_client",
        lambda config: captured.setdefault("config", config) or object(),
    )

    InferenceRuntimeController(runtime)._build_client()

    assert captured["config"].structured_output_mode == "json_object"


def test_status_reports_live_model_health_capabilities_and_revision() -> None:
    controller = InferenceRuntimeController(_runtime())
    controller.client = _ProbeClient(
        HealthProbeResult(
            status=ProbeStatus.HEALTHY,
            endpoint="http://127.0.0.1:8080/health",
            http_status=200,
            detail="ready",
            checked_at=1.0,
        ),
        _capabilities(ProbeStatus.HEALTHY),
    )
    controller.threads = [object()]  # type: ignore[list-item]

    status = controller.status()

    assert status["status"] == "running"
    assert status["worker_status"] == "running"
    assert status["model_status"] == "healthy"
    assert status["availability"] == "healthy"
    assert status["endpoint"] == "http://127.0.0.1:8080/v1"
    assert status["model"] == "local-vlm"
    assert status["revision"] == "revision-2026-08"
    assert status["model_revision"] == "revision-2026-08"
    assert status["health"]["status"] == "healthy"
    assert status["capabilities"]["capabilities"]["supports_json_schema"] is True
    assert controller.client.health_calls == 1
    assert controller.client.capability_calls == 1


def test_unavailable_model_is_reported_without_stopping_workers() -> None:
    controller = InferenceRuntimeController(_runtime())
    controller.client = _ProbeClient(
        HealthProbeResult(
            status=ProbeStatus.UNAVAILABLE,
            endpoint="http://127.0.0.1:8080/health",
            detail="connection refused",
            checked_at=1.0,
        ),
        _capabilities(ProbeStatus.UNAVAILABLE),
    )
    controller.threads = [object()]  # type: ignore[list-item]

    status = controller.status()

    assert status["status"] == "unavailable"
    assert status["worker_status"] == "running"
    assert status["model_status"] == "unavailable"
    assert status["availability"] == "unavailable"
    assert status["active_workers"] == 1
    assert status["configured"] is True
    assert status["health"]["detail"] == "connection refused"
    assert status["capabilities"]["status"] == "unavailable"
