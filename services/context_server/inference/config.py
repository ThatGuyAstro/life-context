"""Configuration for managed and external OpenAI-compatible endpoints."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError

_VALID_MODES = {"managed", "external"}
_VALID_STRUCTURED_OUTPUT_MODES = {"auto", "json_schema", "json_object", "prompt_only"}


def _coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{field_name} must be a boolean")


def _is_loopback_host(value: str | None) -> bool:
    host = str(value or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_url_policy(value: str, *, field_name: str) -> None:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{field_name} must not include embedded credentials")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ConfigurationError(f"non-loopback HTTP {field_name} is not allowed; use HTTPS")


def _clean_base_url(value: str) -> str:
    value = str(value).strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("base_url must be an absolute http(s) URL")
    _validate_url_policy(value, field_name="base_url")
    # Query strings and fragments are never part of an API base URL.  Dropping
    # them also prevents accidental credential-like data in provenance.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _join_url(base_url: str, path: str) -> str:
    path = str(path)
    if path.startswith("http://") or path.startswith("https://"):
        return path.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base_url.rstrip("/") + path


def _safe_netloc(parsed: Any) -> str:
    """Return host/port without userinfo for provenance URLs."""

    hostname = parsed.hostname or parsed.netloc
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{hostname}:{port}" if port is not None else hostname


@dataclass(frozen=True)
class ModelClientConfig:
    """Provider-neutral settings for one managed or external model.

    ``base_url`` is the OpenAI API root, normally ending in ``/v1``.  The
    health endpoint is derived from the URL root when that suffix is present,
    because llama.cpp commonly exposes ``/health`` beside ``/v1`` rather than
    under it.  Deployments with a different layout can provide an absolute
    ``health_path``.
    """

    name: str
    model: str
    base_url: str
    mode: str = "external"
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_s: float = 30.0
    health_path: str = "/health"
    models_path: str = "/models"
    capabilities_path: str | None = None
    structured_output_mode: str = "json_schema"
    default_temperature: float = 0.0
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    declared_capabilities: Mapping[str, Any] = field(default_factory=dict)
    model_revision: str | None = None
    requires_multimodal: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", str(self.mode).strip().lower())
        object.__setattr__(
            self, "structured_output_mode", str(self.structured_output_mode).strip().lower()
        )
        revision = None if self.model_revision is None else str(self.model_revision).strip()
        object.__setattr__(self, "model_revision", revision or None)
        object.__setattr__(
            self,
            "requires_multimodal",
            _coerce_bool(self.requires_multimodal, field_name="requires_multimodal"),
        )
        if not str(self.name).strip():
            raise ConfigurationError("model client name is required")
        if not str(self.model).strip():
            raise ConfigurationError("model identifier is required")
        if self.mode not in _VALID_MODES:
            raise ConfigurationError(f"mode must be one of {sorted(_VALID_MODES)}")
        if self.timeout_s <= 0:
            raise ConfigurationError("timeout_s must be positive")
        if self.structured_output_mode not in _VALID_STRUCTURED_OUTPUT_MODES:
            raise ConfigurationError(
                f"structured_output_mode must be one of {sorted(_VALID_STRUCTURED_OUTPUT_MODES)}"
            )
        if not 0 <= self.default_temperature <= 2:
            raise ConfigurationError("default_temperature must be between 0 and 2")
        object.__setattr__(self, "base_url", _clean_base_url(self.base_url))
        for field_name in ("health_path", "models_path", "capabilities_path"):
            value = getattr(self, field_name)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                _validate_url_policy(value, field_name=field_name)
        object.__setattr__(self, "extra_headers", dict(self.extra_headers))
        object.__setattr__(self, "extra_body", dict(self.extra_body))
        object.__setattr__(self, "declared_capabilities", dict(self.declared_capabilities))

    @property
    def api_root_url(self) -> str:
        """URL root used for endpoints such as ``/health``."""

        parsed = urlsplit(self.base_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3].rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")

    @property
    def chat_completions_url(self) -> str:
        return _join_url(self.base_url, "/chat/completions")

    @property
    def models_url(self) -> str:
        return _join_url(self.base_url, self.models_path)

    @property
    def health_url(self) -> str:
        if self.health_path.startswith("http://") or self.health_path.startswith("https://"):
            return self.health_path.rstrip("/")
        if urlsplit(self.base_url).path.rstrip("/").endswith("/v1"):
            return _join_url(self.api_root_url, self.health_path)
        return _join_url(self.base_url, self.health_path)

    @property
    def capabilities_url(self) -> str:
        return _join_url(self.base_url, self.capabilities_path or self.models_path)

    @property
    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None

    @property
    def safe_base_url(self) -> str:
        """A URL safe to retain in provenance (never includes query/fragment)."""

        parsed = urlsplit(self.base_url)
        return urlunsplit((parsed.scheme, _safe_netloc(parsed), parsed.path, "", ""))

    def auth_headers(self) -> dict[str, str]:
        key = self.resolved_api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def to_dict(self) -> dict[str, Any]:
        """Serialize non-secret configuration for diagnostics."""

        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.safe_base_url,
            "mode": self.mode,
            "api_key_configured": bool(self.resolved_api_key),
            "api_key_env": self.api_key_env,
            "timeout_s": self.timeout_s,
            "health_url": self.health_url,
            "models_url": self.models_url,
            "capabilities_url": self.capabilities_url,
            "structured_output_mode": self.structured_output_mode,
            "model_revision": self.model_revision,
            "requires_multimodal": self.requires_multimodal,
            "default_temperature": self.default_temperature,
            "declared_capabilities": dict(self.declared_capabilities),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, name: str | None = None) -> ModelClientConfig:
        """Build config from JSON/YAML-like data without requiring a settings library."""

        mode = data.get("mode", data.get("kind", "external"))
        structured_mode = data.get("structured_output_mode", data.get("json_mode", "json_schema"))
        if structured_mode is True:
            structured_mode = "json_object"
        if structured_mode is False:
            structured_mode = "prompt_only"
        return cls(
            name=str(name or data.get("name") or "default"),
            model=str(data.get("model") or data.get("model_id") or ""),
            base_url=str(data.get("base_url") or data.get("endpoint") or ""),
            mode=str(mode),
            api_key=data.get("api_key"),
            api_key_env=data.get("api_key_env"),
            timeout_s=float(data.get("timeout_s", data.get("timeout", 30.0))),
            health_path=str(data.get("health_path", "/health")),
            models_path=str(data.get("models_path", "/models")),
            capabilities_path=data.get("capabilities_path"),
            structured_output_mode=str(structured_mode),
            default_temperature=float(
                data.get("default_temperature", data.get("temperature", 0.0))
            ),
            extra_headers=data.get("extra_headers", {}),
            extra_body=data.get("extra_body", {}),
            declared_capabilities=data.get("declared_capabilities", data.get("capabilities", {})),
            model_revision=data.get("model_revision") or data.get("revision"),
            requires_multimodal=data.get(
                "requires_multimodal", data.get("require_multimodal", True)
            ),
        )

    @classmethod
    def from_env(
        cls, *, prefix: str = "CONTEXT_MODEL_", name: str = "default"
    ) -> ModelClientConfig:
        """Read a single client from environment variables.

        Supported names include ``BASE_URL``, ``MODEL``, ``MODE``,
        ``API_KEY``, ``API_KEY_ENV``, ``TIMEOUT_S`` and
        ``STRUCTURED_OUTPUT_MODE`` and ``MODEL_REVISION`` after the supplied prefix.
        """

        def env(key: str, default: Any = None) -> Any:
            return os.environ.get(prefix + key, default)

        return cls.from_mapping(
            {
                "name": name,
                "base_url": env("BASE_URL", ""),
                "model": env("MODEL", ""),
                "mode": env("MODE", "external"),
                "api_key": env("API_KEY"),
                "api_key_env": env("API_KEY_ENV"),
                "timeout_s": env("TIMEOUT_S", 30),
                "health_path": env("HEALTH_PATH", "/health"),
                "models_path": env("MODELS_PATH", "/models"),
                "structured_output_mode": env("STRUCTURED_OUTPUT_MODE", "json_schema"),
                "model_revision": env("MODEL_REVISION"),
                "requires_multimodal": env("REQUIRES_MULTIMODAL", "true"),
            }
        )


ModelEndpointConfig = ModelClientConfig
