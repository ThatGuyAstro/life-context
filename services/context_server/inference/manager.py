"""Model registry and managed/external client construction."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Mapping as MappingABC
from typing import Any

from .client import HttpTransport, ModelClient, OpenAICompatibleModelClient
from .config import ModelClientConfig
from .probes import CapabilityProbeResult, HealthProbeResult, ModelProbeResult


def build_model_client(
    config: ModelClientConfig | Mapping[str, Any],
    *,
    name: str | None = None,
    transport: HttpTransport | None = None,
) -> OpenAICompatibleModelClient:
    """Construct the same OpenAI-compatible client for managed or external mode."""

    resolved = (
        config
        if isinstance(config, ModelClientConfig)
        else ModelClientConfig.from_mapping(config, name=name)
    )
    return OpenAICompatibleModelClient(resolved, transport=transport)


class ModelManager:
    """Named model clients with explicit default selection."""

    def __init__(
        self,
        clients: Mapping[str, ModelClient] | None = None,
        *,
        default_model: str | None = None,
    ) -> None:
        self._clients: dict[str, ModelClient] = dict(clients or {})
        self._default_model = default_model or (next(iter(self._clients), None))
        if self._default_model is not None and self._default_model not in self._clients:
            raise KeyError(f"default model is not registered: {self._default_model}")

    @classmethod
    def from_configs(
        cls,
        configs: Mapping[str, ModelClientConfig | Mapping[str, Any]],
        *,
        default_model: str | None = None,
        transport: HttpTransport | None = None,
    ) -> ModelManager:
        return cls(
            {
                name: build_model_client(config, name=name, transport=transport)
                for name, config in configs.items()
            },
            default_model=default_model,
        )

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        transport: HttpTransport | None = None,
    ) -> ModelManager:
        if "models" in data:
            models = data["models"]
        elif "base_url" in data or "endpoint" in data or "model" in data or "model_id" in data:
            models = {str(data.get("name", "default")): data}
        else:
            models = data
        if not isinstance(models, MappingABC):
            raise ValueError("models configuration must be a mapping")
        default = data.get("default_model", data.get("default"))
        return cls.from_configs(models, default_model=default, transport=transport)

    def register(
        self, name: str, client_or_config: ModelClient | ModelClientConfig | Mapping[str, Any]
    ) -> None:
        if isinstance(client_or_config, ModelClientConfig):
            client: ModelClient = build_model_client(client_or_config, name=name)
        elif isinstance(client_or_config, MappingABC):
            client = build_model_client(client_or_config, name=name)
        else:
            client = client_or_config
        self._clients[name] = client
        if self._default_model is None:
            self._default_model = name

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._clients)

    @property
    def default_model(self) -> str | None:
        return self._default_model

    def get(self, name: str | None = None) -> ModelClient:
        resolved = name or self._default_model
        if resolved is None:
            raise KeyError("no model clients are registered")
        try:
            return self._clients[resolved]
        except KeyError as error:
            raise KeyError(f"model client is not registered: {resolved}") from error

    def resolve(self, name: str | None = None) -> ModelClient:
        return self.get(name)

    def __getitem__(self, name: str) -> ModelClient:
        return self.get(name)

    def health_probe(self, name: str | None = None) -> HealthProbeResult:
        return self.get(name).health_probe()

    def capability_probe(self, name: str | None = None) -> CapabilityProbeResult:
        return self.get(name).capability_probe()

    def probe(self, name: str | None = None) -> ModelProbeResult:
        client = self.get(name)
        return ModelProbeResult(
            health=client.health_probe(), capabilities=client.capability_probe()
        )

    def health_all(self) -> dict[str, HealthProbeResult]:
        return {name: client.health_probe() for name, client in self._clients.items()}

    def capabilities_all(self) -> dict[str, CapabilityProbeResult]:
        return {name: client.capability_probe() for name, client in self._clients.items()}
