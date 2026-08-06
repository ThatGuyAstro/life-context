from __future__ import annotations

import json

from services.context_server.config import Settings, load_settings


def test_file_values_are_overridden_by_environment(tmp_path) -> None:
    config_path = tmp_path / "context-server.json"
    config_path.write_text(
        json.dumps(
            {
                "context_server": {
                    "service_name": "from-file",
                    "port": 9100,
                    "json_logs": False,
                    "capabilities": ["file-capability"],
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(
        config_path,
        environ={
            "CONTEXT_SERVER_PORT": "9200",
            "CONTEXT_SERVER_CAPABILITIES": "env-a,env-b",
        },
    )

    assert settings.service_name == "from-file"
    assert settings.port == 9200
    assert settings.json_logs is False
    assert settings.capabilities == ("env-a", "env-b")
    assert settings.config_file == config_path


def test_settings_normalise_prefix_and_boolean_environment() -> None:
    settings = load_settings(
        environ={
            "CONTEXT_SERVER_API_PREFIX": "api/v1/",
            "CONTEXT_SERVER_DOCS_ENABLED": "false",
            "CONTEXT_SERVER_RELOAD": "1",
        }
    )

    assert settings.api_prefix == "/api/v1"
    assert settings.docs_enabled is False
    assert settings.reload is True


def test_settings_can_be_constructed_directly() -> None:
    settings = Settings(service_name="test", port=8081)

    assert settings.service_name == "test"
    assert settings.port == 8081
