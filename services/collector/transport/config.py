"""Configuration for the source-independent collector transport.

The transport deliberately owns only delivery concerns.  Values are plain
configuration and do not import or describe any capture/source implementation.
"""

from __future__ import annotations

import ipaddress
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


def _optional_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    return Path(value).expanduser()


def _env_value(environment: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environment.get(name)
        if value is not None and value != "":
            return value
    return None


def _env_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment.get(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {value!r}")


def _env_int(environment: Mapping[str, str], name: str, default: int) -> int:
    value = environment.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _env_float(environment: Mapping[str, str], name: str, default: float) -> float:
    value = environment.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


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


def _normalise_path(value: str, *, allow_template: bool = False) -> str:
    path = value.strip()
    if not path:
        raise ValueError("HTTP endpoint path cannot be empty")
    if not path.startswith("/"):
        path = "/" + path
    if not allow_template and "{" in path:
        raise ValueError(f"endpoint path contains an unsupported template: {path!r}")
    return path


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Immutable delivery configuration.

    ``server_url`` can point at a local collector (for example
    ``http://127.0.0.1:8080``) or a remote HTTPS collector.  TLS verification
    remains enabled by default; ``insecure_tls`` is an explicit opt-in for
    development endpoints only.
    """

    server_url: str = "http://127.0.0.1:8000"
    spool_dir: Path = Path(".collector-spool")
    ingest_path: str = "/v1/ingest/batch"
    artifact_path: str = "/v1/artifacts/{sha256}"
    bearer_token: str | None = None
    ca_file: Path | None = None
    client_cert: Path | None = None
    client_key: Path | None = None
    insecure_tls: bool = False
    request_timeout_seconds: float = 10.0
    max_attempts: int = 4
    backoff_initial_seconds: float = 0.25
    backoff_max_seconds: float = 5.0
    backoff_multiplier: float = 2.0
    backoff_jitter: float = 0.0
    batch_size: int = 100
    fsync: bool = True

    def __post_init__(self) -> None:
        parsed = urlparse(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("server_url must be an absolute http:// or https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("server_url must not include embedded credentials")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("non-loopback HTTP server_url is not allowed; use HTTPS")
        object.__setattr__(self, "server_url", self.server_url.rstrip("/"))
        object.__setattr__(self, "spool_dir", Path(self.spool_dir).expanduser())
        object.__setattr__(self, "ingest_path", _normalise_path(self.ingest_path))
        object.__setattr__(
            self,
            "artifact_path",
            _normalise_path(self.artifact_path, allow_template=True),
        )
        for field_name in ("ca_file", "client_cert", "client_key"):
            object.__setattr__(self, field_name, _optional_path(getattr(self, field_name)))

        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.backoff_initial_seconds < 0:
            raise ValueError("backoff_initial_seconds cannot be negative")
        if self.backoff_max_seconds < 0:
            raise ValueError("backoff_max_seconds cannot be negative")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")
        if not 0 <= self.backoff_jitter <= 1:
            raise ValueError("backoff_jitter must be between 0 and 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if self.client_key is not None and self.client_cert is None:
            raise ValueError("client_key requires client_cert")

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str] | None = None,
        **overrides: object,
    ) -> "TransportConfig":
        """Build configuration from ``COLLECTOR_*`` environment variables.

        Explicit keyword overrides are applied last, which makes tests and
        embedding applications independent of the process environment.
        ``COLLECTOR_URL`` is accepted as a short alias for the server URL.
        """

        env = os.environ if environment is None else environment
        defaults = cls()
        values: dict[str, object] = {
            "server_url": _env_value(
                env,
                "COLLECTOR_SERVER_URL",
                "COLLECTOR_BASE_URL",
                "COLLECTOR_URL",
            )
            or defaults.server_url,
            "spool_dir": _env_value(env, "COLLECTOR_SPOOL_DIR") or defaults.spool_dir,
            "ingest_path": _env_value(env, "COLLECTOR_INGEST_PATH") or defaults.ingest_path,
            "artifact_path": _env_value(env, "COLLECTOR_ARTIFACT_PATH") or defaults.artifact_path,
            "bearer_token": _env_value(env, "COLLECTOR_BEARER_TOKEN", "COLLECTOR_AUTH_TOKEN"),
            "ca_file": _optional_path(
                _env_value(env, "COLLECTOR_CA_FILE", "COLLECTOR_TLS_CA_FILE")
            ),
            "client_cert": _optional_path(
                _env_value(env, "COLLECTOR_CLIENT_CERT", "COLLECTOR_TLS_CLIENT_CERT")
            ),
            "client_key": _optional_path(
                _env_value(env, "COLLECTOR_CLIENT_KEY", "COLLECTOR_TLS_CLIENT_KEY")
            ),
            "insecure_tls": _env_bool(
                env,
                "COLLECTOR_INSECURE_TLS",
                _env_bool(env, "COLLECTOR_TLS_INSECURE", defaults.insecure_tls),
            ),
            "request_timeout_seconds": _env_float(
                env, "COLLECTOR_REQUEST_TIMEOUT_SECONDS", defaults.request_timeout_seconds
            ),
            "max_attempts": _env_int(env, "COLLECTOR_MAX_ATTEMPTS", defaults.max_attempts),
            "backoff_initial_seconds": _env_float(
                env, "COLLECTOR_BACKOFF_INITIAL_SECONDS", defaults.backoff_initial_seconds
            ),
            "backoff_max_seconds": _env_float(
                env, "COLLECTOR_BACKOFF_MAX_SECONDS", defaults.backoff_max_seconds
            ),
            "backoff_multiplier": _env_float(
                env, "COLLECTOR_BACKOFF_MULTIPLIER", defaults.backoff_multiplier
            ),
            "backoff_jitter": _env_float(
                env, "COLLECTOR_BACKOFF_JITTER", defaults.backoff_jitter
            ),
            "batch_size": _env_int(env, "COLLECTOR_BATCH_SIZE", defaults.batch_size),
            "fsync": _env_bool(env, "COLLECTOR_FSYNC", defaults.fsync),
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @property
    def is_https(self) -> bool:
        return urlparse(self.server_url).scheme == "https"

    def url_for(self, path: str, **template_values: str) -> str:
        """Resolve an endpoint path against the configured server URL."""

        resolved = path.format(**template_values) if template_values else path
        if not resolved.startswith("/"):
            resolved = "/" + resolved
        return f"{self.server_url}{resolved}"

    def ingest_url(self) -> str:
        return self.url_for(self.ingest_path)

    def artifact_url(self, sha256: str) -> str:
        if "{sha256}" in self.artifact_path:
            path = self.artifact_path.format(sha256=sha256)
        else:
            path = f"{self.artifact_path.rstrip('/')}/{sha256}"
        return self.url_for(path)

    def tls_context(self) -> ssl.SSLContext | None:
        """Return the configured HTTPS context, or ``None`` for HTTP."""

        if not self.is_https:
            return None
        if self.insecure_tls:
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context(
                cafile=str(self.ca_file) if self.ca_file is not None else None
            )
        if self.client_cert is not None:
            context.load_cert_chain(
                certfile=str(self.client_cert),
                keyfile=str(self.client_key) if self.client_key is not None else None,
            )
        return context

    from_environment = from_env
