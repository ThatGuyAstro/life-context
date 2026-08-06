"""Health and capability probe result types.

Probes are intentionally value objects.  A deployment can expose them through
its own route later without coupling this package to a web framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProbeStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthProbeResult:
    """Outcome of a provider health request."""

    status: ProbeStatus
    endpoint: str
    latency_ms: float | None = None
    http_status: int | None = None
    detail: str = ""
    checked_at: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status in {ProbeStatus.HEALTHY, ProbeStatus.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "endpoint": self.endpoint,
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class ModelCapabilities:
    """Capabilities known from configuration and/or provider metadata."""

    model: str
    available_models: tuple[str, ...] = ()
    context_window: int | None = None
    supports_chat: bool | None = True
    supports_streaming: bool | None = None
    supports_tools: bool | None = None
    supports_multimodal: bool | None = None
    supports_json_object: bool | None = None
    supports_json_schema: bool | None = None
    source: str = "unknown"
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def model_listed(self) -> bool | None:
        if not self.available_models:
            return None
        return self.model in self.available_models

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "available_models": list(self.available_models),
            "context_window": self.context_window,
            "supports_chat": self.supports_chat,
            "supports_streaming": self.supports_streaming,
            "supports_tools": self.supports_tools,
            "supports_multimodal": self.supports_multimodal,
            "supports_json_object": self.supports_json_object,
            "supports_json_schema": self.supports_json_schema,
            "model_listed": self.model_listed,
            "source": self.source,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class CapabilityProbeResult:
    """Outcome of a provider model/capability request."""

    status: ProbeStatus
    endpoint: str
    capabilities: ModelCapabilities
    latency_ms: float | None = None
    http_status: int | None = None
    detail: str = ""
    checked_at: float | None = None

    @property
    def available(self) -> bool:
        return self.status in {ProbeStatus.HEALTHY, ProbeStatus.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "endpoint": self.endpoint,
            "capabilities": self.capabilities.to_dict(),
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class ModelProbeResult:
    """Combined health and capability view for one configured model."""

    health: HealthProbeResult
    capabilities: CapabilityProbeResult

    @property
    def status(self) -> ProbeStatus:
        """Return the worst status across endpoint health and capabilities."""

        statuses = {self.health.status, self.capabilities.status}
        if ProbeStatus.UNAVAILABLE in statuses:
            return ProbeStatus.UNAVAILABLE
        if ProbeStatus.DEGRADED in statuses:
            return ProbeStatus.DEGRADED
        if ProbeStatus.UNKNOWN in statuses:
            return ProbeStatus.UNKNOWN
        return ProbeStatus.HEALTHY

    @property
    def available(self) -> bool:
        return self.status in {ProbeStatus.HEALTHY, ProbeStatus.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        return {"health": self.health.to_dict(), "capabilities": self.capabilities.to_dict()}
