from __future__ import annotations

import json

import pytest

from services.context_server.inference import (
    ConfigurationError,
    HttpResponse,
    JsonSchemaContract,
    ModelClientConfig,
    ModelHTTPError,
    ModelProtocolError,
    OpenAICompatibleModelClient,
    ProbeStatus,
    build_strict_json_prompt,
)


class ScriptTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers, body, timeout_s):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        if not self.responses:
            raise AssertionError("no scripted response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _config(**overrides) -> ModelClientConfig:
    values = {
        "name": "local",
        "model": "local-model",
        "base_url": "http://127.0.0.1:8080/v1",
        "mode": "managed",
        "api_key": "secret",
    }
    values.update(overrides)
    return ModelClientConfig(**values)


def test_openai_compatible_completion_uses_schema_response_format() -> None:
    contract = JsonSchemaContract(
        name="answer",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    )
    transport = ScriptTransport(
        HttpResponse(
            200,
            {"X-Request-ID": "server-request"},
            json.dumps(
                {
                    "id": "chatcmpl-1",
                    "model": "local-model-q4",
                    "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 7},
                }
            ),
        )
    )
    client = OpenAICompatibleModelClient(_config(), transport=transport)
    prompt = build_strict_json_prompt("Answer the question.", contract)

    response = client.complete(prompt, request_id="job-1")

    assert response.text == '{"ok":true}'
    assert response.model == "local-model-q4"
    assert response.response_id == "chatcmpl-1"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["headers"]["X-Request-ID"] == "job-1"
    assert call["body"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": contract.schema,
        },
    }
    assert call["body"]["stream"] is False


def test_auto_completion_falls_back_to_json_object_when_schema_is_unsupported() -> None:
    contract = JsonSchemaContract(
        name="answer",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    )
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "data": [
                        {
                            "id": "local-model",
                            "supports_json_object": True,
                            "supports_json_schema": False,
                        }
                    ]
                }
            ),
        ),
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "id": "chatcmpl-auto-object",
                    "model": "local-model",
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                }
            ),
        ),
    )
    client = OpenAICompatibleModelClient(
        _config(structured_output_mode="auto"), transport=transport
    )

    client.complete(build_strict_json_prompt("Answer the question.", contract))

    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[1]["method"] == "POST"
    assert transport.calls[1]["body"]["response_format"] == {"type": "json_object"}
    assert client.provenance_metadata["structured_output_mode"] == "json_object"
    assert client.provenance_metadata["configured_structured_output_mode"] == "auto"


def test_auto_completion_keeps_strict_schema_when_endpoint_advertises_it() -> None:
    contract = JsonSchemaContract(
        name="answer",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    )
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "data": [
                        {
                            "id": "local-model",
                            "supports_json_object": True,
                            "supports_json_schema": True,
                        }
                    ]
                }
            ),
        ),
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "id": "chatcmpl-auto-schema",
                    "model": "local-model",
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                }
            ),
        ),
    )
    client = OpenAICompatibleModelClient(
        _config(structured_output_mode="auto"), transport=transport
    )

    client.complete(build_strict_json_prompt("Answer the question.", contract))

    assert transport.calls[1]["body"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": contract.schema,
        },
    }
    assert client.structured_output_mode == "json_schema"


def test_health_and_capability_probes_handle_v1_root_and_missing_model() -> None:
    transport = ScriptTransport(
        HttpResponse(200, {}, '{"status":"ok"}'),
        HttpResponse(200, {}, json.dumps({"data": [{"id": "other-model"}]})),
    )
    client = OpenAICompatibleModelClient(_config(), transport=transport)

    health = client.health_probe()
    capabilities = client.capability_probe()

    assert health.status is ProbeStatus.HEALTHY
    assert transport.calls[0]["url"] == "http://127.0.0.1:8080/health"
    assert capabilities.status is ProbeStatus.DEGRADED
    assert capabilities.capabilities.model_listed is False
    assert transport.calls[1]["url"] == "http://127.0.0.1:8080/v1/models"


def test_health_probe_reports_explicit_provider_unavailability() -> None:
    transport = ScriptTransport(HttpResponse(200, {}, '{"status":"unavailable"}'))
    client = OpenAICompatibleModelClient(_config(), transport=transport)

    result = client.health_probe()

    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.available is False
    assert "unavailable" in result.detail


def test_capability_probe_reports_when_strict_json_schema_is_not_supported() -> None:
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "data": [
                        {
                            "id": "local-model",
                            "supports_json_schema": False,
                        }
                    ]
                }
            ),
        )
    )
    client = OpenAICompatibleModelClient(_config(), transport=transport)

    result = client.capability_probe()

    assert result.status is ProbeStatus.DEGRADED
    assert result.capabilities.supports_json_schema is False
    assert "JSON Schema" in result.detail


def test_capability_probe_reports_when_multimodal_input_is_not_supported() -> None:
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "data": [
                        {
                            "id": "local-model",
                            "supports_vision": False,
                        }
                    ]
                }
            ),
        )
    )
    client = OpenAICompatibleModelClient(_config(), transport=transport)

    result = client.capability_probe()

    assert result.status is ProbeStatus.DEGRADED
    assert result.capabilities.supports_multimodal is False
    assert "multimodal" in result.detail


def test_model_config_persists_multimodal_requirement_and_env_override(monkeypatch) -> None:
    config = ModelClientConfig.from_mapping(
        {
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "local-model",
            "requires_multimodal": False,
        }
    )
    assert config.requires_multimodal is False
    assert config.to_dict()["requires_multimodal"] is False

    monkeypatch.setenv("CONTEXT_MODEL_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("CONTEXT_MODEL_MODEL", "local-model")
    monkeypatch.setenv("CONTEXT_MODEL_REQUIRES_MULTIMODAL", "false")
    env_config = ModelClientConfig.from_env()
    assert env_config.requires_multimodal is False


def test_model_revision_and_endpoint_are_safe_diagnostic_metadata() -> None:
    config = _config(
        base_url="http://127.0.0.1:8080/v1?token=secret",
        model_revision="  q4_k_m-2026-08  ",
    )
    client = OpenAICompatibleModelClient(config, transport=ScriptTransport())

    assert config.to_dict()["model_revision"] == "q4_k_m-2026-08"
    assert client.provenance_metadata["model_revision"] == "q4_k_m-2026-08"
    assert client.provenance_metadata["endpoint"] == "http://127.0.0.1:8080/v1"


@pytest.mark.parametrize(
    "endpoint_field",
    ("base_url", "health_path", "models_path", "capabilities_path"),
)
def test_model_client_rejects_non_loopback_http_in_endpoint_urls(endpoint_field: str) -> None:
    with pytest.raises(ConfigurationError, match="non-loopback HTTP"):
        _config(**{endpoint_field: "http://model.example.test/v1"})


@pytest.mark.parametrize(
    "endpoint_field",
    ("base_url", "health_path", "models_path", "capabilities_path"),
)
def test_model_client_rejects_embedded_credentials_in_endpoint_urls(endpoint_field: str) -> None:
    with pytest.raises(ConfigurationError, match="embedded credentials"):
        _config(**{endpoint_field: "https://user:password@model.example.test/v1"})


@pytest.mark.parametrize(
    "base_url",
    ("http://localhost:8080/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"),
)
def test_model_client_keeps_loopback_http_endpoints_usable(base_url: str) -> None:
    config = _config(base_url=base_url)

    assert config.base_url == base_url
    assert config.chat_completions_url.endswith("/v1/chat/completions")


def test_http_error_classification_exposes_retry_after() -> None:
    transport = ScriptTransport(HttpResponse(429, {"Retry-After": "3"}, "busy"))
    client = OpenAICompatibleModelClient(_config(), transport=transport)
    with pytest.raises(ModelHTTPError) as raised:
        client.complete([{"role": "user", "content": "hello"}])
    assert raised.value.retryable is True
    assert raised.value.retry_after_s == 3


def test_completion_retries_without_reasoning_when_reasoning_consumes_budget() -> None:
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "id": "reasoning-only",
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": "private analysis that must not be returned",
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {
                        "completion_tokens": 128,
                        "completion_tokens_details": {"reasoning_tokens": 128},
                    },
                }
            ),
        ),
        HttpResponse(
            200,
            {"X-Request-ID": "retry-request"},
            json.dumps(
                {
                    "id": "final-answer",
                    "choices": [
                        {
                            "message": {"content": "A concise, useful answer."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"completion_tokens": 9},
                }
            ),
        ),
    )
    client = OpenAICompatibleModelClient(_config(), transport=transport)

    response = client.complete(
        [{"role": "user", "content": "What happened?"}],
        max_tokens=128,
        request_id="query-1",
    )

    assert response.text == "A concise, useful answer."
    assert response.diagnostics == {
        "reasoning_fallback_used": True,
        "initial_response": {
            "empty_content": True,
            "reasoning_content_chars": 42,
            "reasoning_tokens": 128,
            "completion_tokens": 128,
            "finish_reason": "length",
        },
    }
    assert len(transport.calls) == 2
    assert transport.calls[1]["body"]["reasoning_effort"] == "none"
    assert transport.calls[1]["body"]["chat_template_kwargs"] == {
        "enable_thinking": False
    }
    assert transport.calls[1]["headers"]["X-Request-ID"] == "query-1:no-reasoning"


def test_completion_does_not_expose_reasoning_as_answer() -> None:
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "secret reasoning",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"completion_tokens": 12},
                }
            ),
        )
    )
    client = OpenAICompatibleModelClient(_config(), transport=transport)

    with pytest.raises(ModelProtocolError, match="reasoning_content_chars=16"):
        client.complete([{"role": "user", "content": "hello"}])

    assert len(transport.calls) == 1


def test_completion_reports_exhaustion_when_reasoning_was_already_disabled() -> None:
    transport = ScriptTransport(
        HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "reasoning_content": "analysis",
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {
                        "completion_tokens": 64,
                        "completion_tokens_details": {"reasoning_tokens": 64},
                    },
                }
            ),
        )
    )
    client = OpenAICompatibleModelClient(
        _config(extra_body={"reasoning_effort": "none"}), transport=transport
    )

    with pytest.raises(
        ModelProtocolError,
        match=(
            "finish_reason='length'.*completion_tokens=64.*reasoning_tokens=64.*"
            "reasoning budget consumed"
        ),
    ):
        client.complete([{"role": "user", "content": "hello"}])

    assert len(transport.calls) == 1
