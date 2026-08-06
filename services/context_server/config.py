"""Configuration loading for the context server.

Configuration is merged in this order:

1. built-in defaults;
2. the optional JSON, TOML, or YAML config file;
3. ``CONTEXT_SERVER_*`` environment variables.

API-owned runtime updates are stored separately by the context runtime and
remain below the explicit file and environment sources when they are read
back during a later runtime construction.

The implementation uses a small dataclass rather than tying the foundation
to a settings-specific dependency.  This makes the package usable from a
minimal uvicorn installation while leaving room for richer settings in the
feature modules.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

_ENV_PREFIX = "CONTEXT_SERVER_"
_CONFIG_FILE_ENV_NAMES = ("CONTEXT_SERVER_CONFIG_FILE", "CONTEXT_SERVER_CONFIG")
_RUNTIME_CONFIG_KEYS = frozenset({"retention", "source_routing", "routes"})
_PERSISTED_CONFIG_FILENAME = "config.json"
MODEL_STRUCTURED_OUTPUT_MODES = frozenset(
    {"auto", "json_schema", "json_object", "prompt_only"}
)


def _normalise_key(key: str) -> str:
    """Return a config key in the form used by :class:`Settings`."""

    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for the foundation application."""

    service_name: str = "context-server"
    version: str = "0.1.0"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    json_logs: bool = True
    config_file: Path | None = None
    api_prefix: str = ""
    docs_enabled: bool = True
    reload: bool = False
    capabilities: tuple[str, ...] = ()
    # Life Context runtime defaults.  The server remains localhost-only unless
    # an operator explicitly changes ``host`` and configures remote security.
    data_dir: Path = Path(r"E:\LifeContext\data")
    capture_interval_seconds: float = 5.0
    worker_count: int = 1
    queue_size: int = 1000
    raw_retention_hours: float = 24.0
    keyframe_retention_days: float = 30.0
    configured_roots: tuple[str, ...] = ()
    model_endpoint: str = ""
    model_name: str = ""
    model_revision: str = ""
    model_structured_output_mode: str = "auto"
    auth_token: str = ""
    require_https_remote: bool = True
    # ``load_settings`` keeps source keys separately so runtime-owned API
    # updates can remain below explicit file/environment values.  It is
    # intentionally not part of the constructor or public serialization.
    _source_values: dict[str, Any] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Validate and normalise values at the boundary."""

        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")

        object.__setattr__(self, "log_level", self.log_level.upper())
        object.__setattr__(self, "api_prefix", _normalise_prefix(self.api_prefix))
        object.__setattr__(self, "capabilities", _coerce_string_tuple(self.capabilities))
        object.__setattr__(self, "configured_roots", _coerce_string_tuple(self.configured_roots))
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser())
        structured_output_mode = str(self.model_structured_output_mode).strip().lower()
        if structured_output_mode not in MODEL_STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                "model_structured_output_mode must be one of "
                f"{sorted(MODEL_STRUCTURED_OUTPUT_MODES)}"
            )
        object.__setattr__(self, "model_structured_output_mode", structured_output_mode)

        if self.capture_interval_seconds <= 0:
            raise ValueError("capture_interval_seconds must be positive")
        if not math.isfinite(float(self.capture_interval_seconds)):
            raise ValueError("capture_interval_seconds must be finite")
        if self.worker_count < 1:
            raise ValueError("worker_count must be positive")
        if self.queue_size < 1:
            raise ValueError("queue_size must be positive")
        if self.raw_retention_hours < 0 or self.keyframe_retention_days < 0:
            raise ValueError("retention windows cannot be negative")
        if not math.isfinite(float(self.raw_retention_hours)):
            raise ValueError("raw_retention_hours must be finite")
        if not math.isfinite(float(self.keyframe_retention_days)):
            raise ValueError("keyframe_retention_days must be finite")

        if self.config_file is not None and not isinstance(self.config_file, Path):
            object.__setattr__(self, "config_file", Path(self.config_file))

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        *,
        base: Settings | None = None,
    ) -> Settings:
        """Build settings from a mapping, ignoring unknown keys.

        ``base`` is useful when callers want to apply one source at a time.
        Normal application loading is handled by :func:`load_settings`.
        """

        source = {_normalise_key(key): value for key, value in values.items()}
        aliases = {
            "name": "service_name",
            "service": "service_name",
            "log_json": "json_logs",
            "json_logging": "json_logs",
            "config": "config_file",
        }
        source = {aliases.get(key, key): value for key, value in source.items()}
        known = {field.name for field in fields(cls) if field.init}
        current = {
            field.name: getattr(base, field.name) if base is not None else field.default
            for field in fields(cls)
            if field.init
        }

        for key, value in source.items():
            if key not in known or value is None:
                continue
            current[key] = _coerce_field(key, value)

        result = cls(**current)
        source_values = dict(getattr(base, "_source_values", {})) if base is not None else {}
        source_values.update({key: value for key, value in source.items() if value is not None})
        object.__setattr__(result, "_source_values", source_values)
        return result

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        config_file: str | Path | None = None,
    ) -> Settings:
        """Load settings from an environment mapping and optional file."""

        return load_settings(config_file=config_file, environ=environ)


# Populated after Settings exists.  Keeping this private constant makes it
# easy to inspect the supported field names without duplicating them.
_KNOWN_FIELDS = {field.name for field in fields(Settings) if field.init}


def _normalise_prefix(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


def _coerce_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                pass
        else:
            value = text.split(",")
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _coerce_field(key: str, value: Any) -> Any:
    if key in {"port", "worker_count", "queue_size"}:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid integer value for {key}: {value!r}") from exc
    if key in {
        "capture_interval_seconds",
        "raw_retention_hours",
        "keyframe_retention_days",
    }:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid number value for {key}: {value!r}") from exc
    if key in {"json_logs", "docs_enabled", "reload", "require_https_remote"}:
        return _coerce_bool(value)
    if key in {"capabilities", "configured_roots"}:
        return _coerce_string_tuple(value)
    if key in {"config_file", "data_dir"}:
        return Path(value) if value else None
    if key == "log_level":
        return str(value).upper()
    return str(value) if value is not None else value


def _unwrap_file_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """Accept both flat config files and a conventional nested root."""

    result: Mapping[str, Any] = values
    for root_key in ("context_server", "context-server", "server", "app"):
        nested = result.get(root_key)
        if isinstance(nested, Mapping):
            result = nested
            break
    return dict(result)


def _load_config_file(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"context-server config file does not exist: {path}")

    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            parsed = json.loads(text)
        elif suffix == ".toml":
            parsed = tomllib.loads(text)
        elif suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "YAML config files require PyYAML; use JSON or TOML instead"
                ) from exc
            parsed = yaml.safe_load(text)
        else:
            raise ValueError(
                f"unsupported config file extension {suffix!r}; use .json, .toml, or .yaml"
            )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid context-server config file: {path}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, Mapping):
        raise ValueError(f"context-server config file must contain an object: {path}")
    return _unwrap_file_mapping(parsed)


def _environment_values(environ: Mapping[str, str]) -> dict[str, str]:
    aliases = {
        "LOG_JSON": "json_logs",
        "JSON_LOGS": "json_logs",
        "CONFIG": "config_file",
        "CONFIG_FILE": "config_file",
    }
    values: dict[str, str] = {}
    for env_key, value in environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        suffix = env_key[len(_ENV_PREFIX):]
        key = aliases.get(suffix, _normalise_key(suffix))
        if key in _KNOWN_FIELDS or key in _RUNTIME_CONFIG_KEYS:
            values[key] = value
    return values


def runtime_config_path(data_dir: str | Path) -> Path:
    """Return the sidecar path used for API-owned runtime configuration."""

    return Path(data_dir).expanduser() / _PERSISTED_CONFIG_FILENAME


def load_persisted_config(data_dir: str | Path) -> dict[str, Any]:
    """Load API-owned configuration from the data directory if present.

    A missing or malformed sidecar is treated as an empty override.  The
    regular settings file and environment loader remain authoritative for
    their own values and are not modified by this read.
    """

    path = runtime_config_path(data_dir)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    return dict(parsed)


def save_persisted_config(data_dir: str | Path, values: Mapping[str, Any]) -> Path:
    """Atomically save API-owned configuration without changing source files."""

    path = runtime_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def load_settings(
    config_file: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings with file values overridden by environment values."""

    env = os.environ if environ is None else environ
    configured_path: str | Path | None = config_file
    if configured_path is None:
        for env_name in _CONFIG_FILE_ENV_NAMES:
            if env.get(env_name):
                configured_path = env[env_name]
                break

    file_values: Mapping[str, Any] = {}
    if configured_path:
        file_path = Path(configured_path)
        file_values = _load_config_file(file_path)

    settings = Settings.from_mapping(file_values)
    environment_values = _environment_values(env)
    if configured_path:
        environment_values["config_file"] = str(configured_path)
    return Settings.from_mapping(environment_values, base=settings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process settings, cached for stable app construction.

    Tests or embedding applications that change environment variables can call
    ``get_settings.cache_clear()`` and then request a fresh instance.
    """

    return load_settings()


__all__ = [
    "MODEL_STRUCTURED_OUTPUT_MODES",
    "Settings",
    "get_settings",
    "load_persisted_config",
    "load_settings",
    "runtime_config_path",
    "save_persisted_config",
]
