"""Acceptance coverage for the Windows-first configuration contract.

These tests exercise settings loading and diagnostic projection only.  They do
not write the runtime-owned ``config.json`` sidecar used by the config API.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from services.collector.transport.config import TransportConfig
from services.context_server.config import Settings, load_settings
from services.context_server.runtime import ContextRuntime


def _temporary_runtime(settings: Settings, data_dir: Path) -> ContextRuntime:
    """Use temporary storage while preserving the loaded source values."""

    temporary_settings = Settings.from_mapping({"data_dir": data_dir}, base=settings)
    return ContextRuntime(temporary_settings)


def test_windows_defaults_and_retention_tiers_are_explicit(tmp_path: Path) -> None:
    settings = Settings()

    assert settings.data_dir == Path(r"E:\LifeContext\data")
    assert settings.capture_interval_seconds == pytest.approx(5.0)
    assert settings.worker_count == 1
    assert settings.queue_size == 1000
    assert settings.model_structured_output_mode == "auto"
    assert settings.raw_retention_hours == pytest.approx(24.0)
    assert settings.keyframe_retention_days == pytest.approx(30.0)

    runtime = _temporary_runtime(settings, tmp_path / "server-data")
    try:
        effective = runtime.config_operation()["settings"]

        assert effective["capture_interval_seconds"] == pytest.approx(5.0)
        assert effective["worker_count"] == 1
        assert effective["queue_size"] == 1000
        assert effective["retention"] == {
            "raw_hours": 24.0,
            "keyframe_days": 30.0,
            "derived": "indefinite",
        }
        assert runtime.storage.retention_policy.raw == timedelta(hours=24)
        assert runtime.storage.retention_policy.keyframe == timedelta(days=30)
        assert runtime.storage.retention_policy.derived is None
    finally:
        runtime.shutdown()


def test_environment_overrides_file_values_and_normalises_source_routing(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "context-server.json"
    config_file.write_text(
        json.dumps(
            {
                "context-server": {
                    "data-dir": r"C:\LifeContext\file-data",
                    "capture-interval-seconds": "9",
                    "worker-count": "2",
                    "queue-size": "250",
                    "raw-retention-hours": "48",
                    "keyframe-retention-days": "90",
                    "model-endpoint": "https://file-model.example/v1",
                    "source-routing": {
                        "clipboard": {"route": "remote", "enabled": False},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    environment_routing = {
        "clipboard": {"route": "local", "enabled": True},
        "screenshots": {"route": "remote", "enabled": False},
    }

    settings = load_settings(
        config_file,
        environ={
            "CONTEXT_SERVER_DATA_DIR": r"E:\LifeContext\env-data",
            "CONTEXT_SERVER_CAPTURE_INTERVAL_SECONDS": "5",
            "CONTEXT_SERVER_WORKER_COUNT": "1",
            "CONTEXT_SERVER_QUEUE_SIZE": "1000",
            "CONTEXT_SERVER_SOURCE_ROUTING": json.dumps(environment_routing),
            "CONTEXT_SERVER_MODEL_ENDPOINT": "https://env-model.example/v1",
            "CONTEXT_SERVER_MODEL_STRUCTURED_OUTPUT_MODE": "json_object",
        },
    )

    assert settings.config_file == config_file
    assert settings.data_dir == Path(r"E:\LifeContext\env-data")
    assert settings.capture_interval_seconds == pytest.approx(5.0)
    assert settings.worker_count == 1
    assert settings.queue_size == 1000
    assert settings.raw_retention_hours == pytest.approx(48.0)
    assert settings.keyframe_retention_days == pytest.approx(90.0)

    runtime = _temporary_runtime(settings, tmp_path / "server-data")
    try:
        effective = runtime.config_operation()["settings"]
        assert effective["model_endpoint"] == "https://env-model.example/v1"
        assert effective["model_structured_output_mode"] == "json_object"
        assert effective["source_routing"] == environment_routing
    finally:
        runtime.shutdown()


def test_transport_environment_selects_local_or_remote_endpoint_with_precedence() -> None:
    local = TransportConfig.from_env({})
    assert local.server_url == "http://127.0.0.1:8000"
    assert local.is_https is False
    assert local.ingest_url() == "http://127.0.0.1:8000/v1/ingest/batch"

    remote = TransportConfig.from_env(
        {
            "COLLECTOR_SERVER_URL": "https://remote.example.test/",
            "COLLECTOR_BASE_URL": "https://base.example.test/",
            "COLLECTOR_URL": "https://alias.example.test/",
        }
    )
    assert remote.server_url == "https://remote.example.test"
    assert remote.is_https is True

    explicit_local = TransportConfig.from_env(
        {"COLLECTOR_SERVER_URL": "https://remote.example.test"},
        server_url="http://127.0.0.1:9000",
    )
    assert explicit_local.server_url == "http://127.0.0.1:9000"
    assert explicit_local.is_https is False


def test_runtime_diagnostics_redact_credentials_and_url_secrets(tmp_path: Path) -> None:
    routing = {
        "clipboard": {
            "route": "local",
            "enabled": True,
            "api-key": "nested-api-key",
            "password": "nested-password",
        }
    }
    settings = load_settings(
        environ={
            "CONTEXT_SERVER_AUTH_TOKEN": "server-auth-token",
            "CONTEXT_SERVER_MODEL_ENDPOINT": (
                "https://user:url-password@model.example.test/v1"
                "?api_key=url-api-key#fragment"
            ),
            "CONTEXT_SERVER_SOURCE_ROUTING": json.dumps(routing),
        }
    )

    runtime = _temporary_runtime(settings, tmp_path / "server-data")
    try:
        diagnostics = runtime.config_operation()["settings"]
        serialized = json.dumps(diagnostics, sort_keys=True)

        assert diagnostics["model_endpoint"] == "https://model.example.test/v1"
        assert diagnostics["source_routing"] == {
            "clipboard": {"route": "local", "enabled": True}
        }
        for secret in (
            "server-auth-token",
            "url-password",
            "url-api-key",
            "nested-api-key",
            "nested-password",
        ):
            assert secret not in serialized
    finally:
        runtime.shutdown()


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("CONTEXT_SERVER_PORT", "not-an-integer"),
        ("CONTEXT_SERVER_CAPTURE_INTERVAL_SECONDS", "not-a-number"),
        ("CONTEXT_SERVER_CAPTURE_INTERVAL_SECONDS", "nan"),
        ("CONTEXT_SERVER_CAPTURE_INTERVAL_SECONDS", "inf"),
        ("CONTEXT_SERVER_WORKER_COUNT", "0"),
        ("CONTEXT_SERVER_QUEUE_SIZE", "-1"),
        ("CONTEXT_SERVER_DOCS_ENABLED", "sometimes"),
        ("CONTEXT_SERVER_RAW_RETENTION_HOURS", "-1"),
        ("CONTEXT_SERVER_MODEL_STRUCTURED_OUTPUT_MODE", "unsupported"),
    ],
)
def test_malformed_environment_values_are_rejected(
    environment_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        load_settings(environ={environment_name: value})
