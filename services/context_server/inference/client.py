"""OpenAI-compatible model client and transport-neutral probes."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import ModelClientConfig
from .contracts import JsonSchemaContract, PromptBundle
from .errors import ModelHTTPError, ModelProtocolError, ModelTransportError
from .probes import CapabilityProbeResult, HealthProbeResult, ModelCapabilities, ProbeStatus


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout_s: float,
    ) -> HttpResponse:
        """Perform one HTTP request and return the raw response."""


class UrllibHttpTransport:
    """Small stdlib transport so the package has no provider SDK dependency."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout_s: float,
    ) -> HttpResponse:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=dict(headers), method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return HttpResponse(
                    status_code=int(response.status),
                    headers={str(k): str(v) for k, v in response.headers.items()},
                    body=response.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as error:
            body_text = error.read().decode("utf-8", errors="replace")
            return HttpResponse(
                status_code=int(error.code),
                headers={str(k): str(v) for k, v in error.headers.items()},
                body=body_text,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ModelTransportError(f"request to model endpoint failed: {error}") from error


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str | None = None
    response_id: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    created: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None


class ModelClient(Protocol):
    model_name: str

    def complete(
        self,
        messages: PromptBundle | Sequence[ChatMessage | Mapping[str, Any]],
        *,
        contract: JsonSchemaContract | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_id: str | None = None,
    ) -> ModelResponse:
        """Generate one non-streaming chat completion."""

    def health_probe(self) -> HealthProbeResult:
        """Check endpoint availability."""

    def capability_probe(self) -> CapabilityProbeResult:
        """Discover available models and declared response capabilities."""


def _header(headers: Mapping[str, str], name: str) -> str | None:
    needle = name.lower()
    for key, value in headers.items():
        if key.lower() == needle:
            return value
    return None


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    value = _header(headers, "Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for block in content:
            if isinstance(block, str):
                pieces.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                pieces.append(block["text"])
        if pieces:
            return "".join(pieces)
    raise ModelProtocolError("completion choice message has no text content")


def _text_or_none(content: Any) -> str | None:
    """Return provider text while allowing an absent final answer to be diagnosed."""

    try:
        return _block_text(content)
    except ModelProtocolError:
        return None


def _reasoning_length(message: Mapping[str, Any]) -> int:
    """Count reasoning characters without retaining or exposing chain-of-thought."""

    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        return len(reasoning)
    if isinstance(reasoning, list):
        total = 0
        for block in reasoning:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                total += len(block["text"])
        return total
    return 0


def _reasoning_diagnostics(
    message: Mapping[str, Any],
    choice: Mapping[str, Any],
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    details = usage.get("completion_tokens_details")
    reasoning_tokens = (
        details.get("reasoning_tokens") if isinstance(details, Mapping) else None
    )
    return {
        "empty_content": not bool((_text_or_none(message.get("content")) or "").strip()),
        "reasoning_content_chars": _reasoning_length(message),
        "reasoning_tokens": reasoning_tokens if isinstance(reasoning_tokens, int) else None,
        "completion_tokens": (
            usage.get("completion_tokens")
            if isinstance(usage.get("completion_tokens"), int)
            else None
        ),
        "finish_reason": (
            str(choice["finish_reason"])
            if choice.get("finish_reason") is not None
            else None
        ),
    }


def _reasoning_is_disabled(body: Mapping[str, Any]) -> bool:
    effort = body.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip().lower() in {"none", "off"}:
        return True
    template_kwargs = body.get("chat_template_kwargs")
    return (
        isinstance(template_kwargs, Mapping)
        and template_kwargs.get("enable_thinking") is False
    )


def _without_reasoning(body: Mapping[str, Any]) -> dict[str, Any]:
    """Build a llama.cpp-compatible retry body that requests a final answer directly."""

    retry_body = dict(body)
    retry_body["reasoning_effort"] = "none"
    template_kwargs = retry_body.get("chat_template_kwargs")
    merged_template_kwargs = (
        dict(template_kwargs) if isinstance(template_kwargs, Mapping) else {}
    )
    merged_template_kwargs["enable_thinking"] = False
    retry_body["chat_template_kwargs"] = merged_template_kwargs
    return retry_body


def _reported_health_status(raw: Mapping[str, Any]) -> tuple[ProbeStatus, str]:
    """Interpret explicit provider health fields without requiring one schema."""

    reported = raw.get("status")
    if isinstance(reported, str):
        normalized = reported.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in {"unavailable", "unhealthy", "error", "failed", "not_ready"}:
            return ProbeStatus.UNAVAILABLE, f"endpoint reported status {reported!r}"
        if normalized in {"degraded", "loading", "starting", "initializing", "busy"}:
            return ProbeStatus.DEGRADED, f"endpoint reported status {reported!r}"
    if raw.get("healthy") is False:
        return ProbeStatus.UNAVAILABLE, "endpoint reported healthy=false"
    if raw.get("ready") is False:
        return ProbeStatus.DEGRADED, "endpoint reported ready=false"
    return ProbeStatus.HEALTHY, "endpoint responded successfully"


def _select_structured_output_mode(
    configured_mode: str, capabilities: ModelCapabilities
) -> str:
    """Resolve the wire format while keeping explicit modes operator-controlled."""

    if configured_mode == "auto":
        # JSON Schema is the stronger contract, but only use it when the
        # endpoint explicitly advertises support.  OpenAI-compatible servers
        # commonly implement json_object without implementing json_schema.
        return (
            "json_schema"
            if capabilities.supports_json_schema is True
            else "json_object"
        )
    return configured_mode


class OpenAICompatibleModelClient:
    """Configurable client for managed or external OpenAI-compatible servers.

    Managed and external endpoints use the same wire contract.  ``mode`` is a
    provenance and policy distinction; it does not silently change URLs or
    authentication.  This makes local servers, gateways, and hosted providers
    testable through one injected transport.
    """

    def __init__(
        self, config: ModelClientConfig, *, transport: HttpTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport or UrllibHttpTransport()
        self._capability_lock = threading.RLock()
        self._capabilities_probed = False
        self._effective_structured_output_mode = config.structured_output_mode

    @property
    def model_name(self) -> str:
        return self.config.name

    @property
    def provenance_metadata(self) -> dict[str, Any]:
        with self._capability_lock:
            structured_output_mode = self._effective_structured_output_mode
        return {
            "client_name": self.config.name,
            "mode": self.config.mode,
            "configured_model": self.config.model,
            "model_revision": self.config.model_revision,
            "requires_multimodal": self.config.requires_multimodal,
            "endpoint": self.config.safe_base_url,
            "structured_output_mode": structured_output_mode,
            "configured_structured_output_mode": self.config.structured_output_mode,
        }

    @property
    def structured_output_mode(self) -> str:
        """Return the effective response format mode used on the wire."""

        with self._capability_lock:
            return self._effective_structured_output_mode

    def _remember_capabilities(self, capabilities: ModelCapabilities) -> None:
        with self._capability_lock:
            self._effective_structured_output_mode = _select_structured_output_mode(
                self.config.structured_output_mode, capabilities
            )
            self._capabilities_probed = True

    def _resolved_structured_output_mode(self) -> str:
        """Resolve ``auto`` once before the first structured request."""

        if self.config.structured_output_mode != "auto":
            return self.config.structured_output_mode
        with self._capability_lock:
            if not self._capabilities_probed:
                # The probe is deliberately best-effort.  Its fallback
                # capabilities select json_object for auto when the endpoint
                # has no usable capability document.
                self.capability_probe()
            return self._effective_structured_output_mode

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        headers.update(self.config.auth_headers())
        headers.update({str(k): str(v) for k, v in self.config.extra_headers.items()})
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> HttpResponse:
        try:
            response = self.transport.request(
                method,
                url,
                headers=self._headers(request_id),
                body=body,
                timeout_s=self.config.timeout_s,
            )
        except ModelTransportError:
            raise
        except Exception as error:
            raise ModelTransportError(
                f"transport raised {error.__class__.__name__}: {error}"
            ) from error
        if not 200 <= response.status_code < 300:
            raise ModelHTTPError(
                response.status_code,
                body=response.body,
                retry_after_s=_parse_retry_after(response.headers),
            )
        return response

    @staticmethod
    def _json(response: HttpResponse) -> Mapping[str, Any]:
        try:
            value = json.loads(response.body) if response.body else {}
        except json.JSONDecodeError as error:
            raise ModelProtocolError(f"model endpoint returned invalid JSON: {error}") from error
        if not isinstance(value, Mapping):
            raise ModelProtocolError("model endpoint JSON response must be an object")
        return value

    @staticmethod
    def _normalize_messages(
        messages: PromptBundle | Sequence[ChatMessage | Mapping[str, Any]],
    ) -> tuple[Sequence[ChatMessage | Mapping[str, Any]], JsonSchemaContract | None]:
        if isinstance(messages, PromptBundle):
            return messages.messages, messages.contract
        return messages, None

    def complete(
        self,
        messages: PromptBundle | Sequence[ChatMessage | Mapping[str, Any]],
        *,
        contract: JsonSchemaContract | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_id: str | None = None,
    ) -> ModelResponse:
        normalized, embedded_contract = self._normalize_messages(messages)
        contract = contract or embedded_contract
        serialized_messages: list[dict[str, Any]] = []
        for message in normalized:
            if isinstance(message, ChatMessage):
                item = message.to_dict()
            elif isinstance(message, Mapping):
                item = dict(message)
            else:
                raise ModelProtocolError("chat messages must be ChatMessage or mapping values")
            if not isinstance(item.get("role"), str) or "content" not in item:
                raise ModelProtocolError("each chat message needs a role and content")
            serialized_messages.append(item)

        body: dict[str, Any] = dict(self.config.extra_body)
        body.update(
            {
                "model": self.config.model,
                "messages": serialized_messages,
                "temperature": self.config.default_temperature
                if temperature is None
                else temperature,
                "stream": False,
            }
        )
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if contract is not None:
            response_format = contract.response_format(self._resolved_structured_output_mode())
            if response_format is not None:
                body["response_format"] = response_format

        response = self._request(
            "POST",
            self.config.chat_completions_url,
            body=body,
            request_id=request_id,
        )
        data = self._json(response)
        result, empty_diagnostics = self._parse_completion(
            data, response=response, request_id=request_id
        )
        if result is not None:
            return result

        assert empty_diagnostics is not None
        reasoning_exhausted_budget = (
            empty_diagnostics["reasoning_content_chars"] > 0
            and empty_diagnostics["finish_reason"] == "length"
        )
        if reasoning_exhausted_budget and not _reasoning_is_disabled(body):
            retry_response = self._request(
                "POST",
                self.config.chat_completions_url,
                body=_without_reasoning(body),
                request_id=f"{request_id}:no-reasoning" if request_id else None,
            )
            retry_data = self._json(retry_response)
            retry_result, retry_diagnostics = self._parse_completion(
                retry_data,
                response=retry_response,
                request_id=request_id,
                diagnostics={
                    "reasoning_fallback_used": True,
                    "initial_response": empty_diagnostics,
                },
            )
            if retry_result is not None:
                return retry_result
            assert retry_diagnostics is not None
            raise ModelProtocolError(
                self._empty_content_error(
                    retry_diagnostics,
                    prefix="reasoning-disabled retry also returned no final content",
                )
            )

        raise ModelProtocolError(self._empty_content_error(empty_diagnostics))

    def _parse_completion(
        self,
        data: Mapping[str, Any],
        *,
        response: HttpResponse,
        request_id: str | None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> tuple[ModelResponse | None, dict[str, Any] | None]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ModelProtocolError("completion response has no choices[0]")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ModelProtocolError("completion response has no message object")
        usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
        text = _text_or_none(message.get("content"))
        if text is None or not text.strip():
            return None, _reasoning_diagnostics(message, choice, usage)
        return ModelResponse(
            text=text,
            model=str(data["model"]) if data.get("model") is not None else self.config.model,
            response_id=str(data["id"]) if data.get("id") is not None else None,
            usage=dict(usage),
            diagnostics=dict(diagnostics or {}),
            finish_reason=str(choice["finish_reason"])
            if choice.get("finish_reason") is not None
            else None,
            created=int(data["created"]) if isinstance(data.get("created"), (int, float)) else None,
            raw=dict(data),
            request_id=_header(response.headers, "X-Request-ID") or request_id,
        ), None

    @staticmethod
    def _empty_content_error(
        diagnostics: Mapping[str, Any], *, prefix: str = "completion returned no final content"
    ) -> str:
        reasoning_chars = diagnostics.get("reasoning_content_chars", 0)
        finish_reason = diagnostics.get("finish_reason")
        completion_tokens = diagnostics.get("completion_tokens")
        reasoning_tokens = diagnostics.get("reasoning_tokens")
        detail = (
            f"{prefix}; finish_reason={finish_reason!r}, "
            f"reasoning_content_chars={reasoning_chars}"
        )
        if completion_tokens is not None:
            detail += f", completion_tokens={completion_tokens}"
        if reasoning_tokens is not None:
            detail += f", reasoning_tokens={reasoning_tokens}"
        if reasoning_chars and finish_reason == "length":
            detail += (
                "; the reasoning budget consumed the completion limit before an answer "
                "was emitted"
            )
        return detail

    def health_probe(self) -> HealthProbeResult:
        checked_at = time.time()
        started = time.perf_counter()
        try:
            response = self._request("GET", self.config.health_url)
            raw: Mapping[str, Any] = {}
            if response.body:
                try:
                    parsed = json.loads(response.body)
                    if isinstance(parsed, Mapping):
                        raw = parsed
                except json.JSONDecodeError:
                    raw = {}
            status, detail = _reported_health_status(raw)
            return HealthProbeResult(
                status=status,
                endpoint=self.config.health_url,
                latency_ms=(time.perf_counter() - started) * 1000,
                http_status=response.status_code,
                detail=detail,
                checked_at=checked_at,
                raw=raw,
            )
        except ModelHTTPError as error:
            return HealthProbeResult(
                status=ProbeStatus.DEGRADED if error.retryable else ProbeStatus.UNAVAILABLE,
                endpoint=self.config.health_url,
                latency_ms=(time.perf_counter() - started) * 1000,
                http_status=error.status_code,
                detail=str(error),
                checked_at=checked_at,
            )
        except ModelTransportError as error:
            return HealthProbeResult(
                status=ProbeStatus.UNAVAILABLE,
                endpoint=self.config.health_url,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=str(error),
                checked_at=checked_at,
            )

    def capability_probe(self) -> CapabilityProbeResult:
        checked_at = time.time()
        started = time.perf_counter()
        declared = self.config.declared_capabilities
        configured_json_object = self.config.structured_output_mode in {
            "auto",
            "json_object",
            "json_schema",
        }
        configured_json_schema = self.config.structured_output_mode == "json_schema"
        default_capabilities = ModelCapabilities(
            model=self.config.model,
            context_window=_optional_int(declared.get("context_window")),
            supports_chat=_optional_bool(declared.get("supports_chat"), default=True),
            supports_streaming=_optional_bool(declared.get("supports_streaming")),
            supports_tools=_optional_bool(declared.get("supports_tools")),
            supports_multimodal=_capability_bool(declared, "supports_multimodal"),
            supports_json_object=_optional_bool(
                declared.get("supports_json_object"), default=configured_json_object
            ),
            supports_json_schema=_optional_bool(
                declared.get("supports_json_schema"), default=configured_json_schema
            ),
            source="configuration",
            raw=dict(declared),
        )
        try:
            response = self._request("GET", self.config.capabilities_url)
            data = self._json(response)
            entries = data.get("data", data.get("models", []))
            if not isinstance(entries, list):
                entries = []
            model_ids: list[str] = []
            selected: Mapping[str, Any] = {}
            for entry in entries:
                if isinstance(entry, str):
                    model_ids.append(entry)
                    if entry == self.config.model:
                        selected = {"id": entry}
                elif isinstance(entry, Mapping) and entry.get("id") is not None:
                    model_id = str(entry["id"])
                    model_ids.append(model_id)
                    if model_id == self.config.model:
                        selected = entry
            metadata: dict[str, Any] = {
                key: selected[key]
                for key in (
                    "context_window",
                    "supports_chat",
                    "supports_streaming",
                    "supports_tools",
                    "supports_multimodal",
                    "supports_vision",
                    "multimodal",
                    "vision",
                    "supports_json_object",
                    "supports_json_schema",
                )
                if key in selected
            }
            nested_metadata = selected.get("capabilities", selected.get("metadata", {}))
            if isinstance(nested_metadata, Mapping):
                metadata.update(dict(nested_metadata))
            merged = dict(declared)
            merged.update(metadata)
            capabilities = ModelCapabilities(
                model=self.config.model,
                available_models=tuple(model_ids),
                context_window=_optional_int(merged.get("context_window")),
                supports_chat=_optional_bool(merged.get("supports_chat"), default=True),
                supports_streaming=_optional_bool(merged.get("supports_streaming")),
                supports_tools=_optional_bool(merged.get("supports_tools")),
                supports_multimodal=_capability_bool(merged, "supports_multimodal"),
                supports_json_object=_optional_bool(
                    merged.get("supports_json_object"), default=configured_json_object
                ),
                supports_json_schema=_optional_bool(
                    merged.get("supports_json_schema"), default=configured_json_schema
                ),
                source="endpoint",
                raw=dict(data),
            )
            status = ProbeStatus.HEALTHY
            detail = "capabilities endpoint responded successfully"
            if capabilities.model_listed is False:
                status = ProbeStatus.DEGRADED
                detail = "configured model was not listed by the endpoint"
            elif self.config.requires_multimodal and capabilities.supports_multimodal is False:
                status = ProbeStatus.DEGRADED
                detail = "configured model does not advertise multimodal/vision input support"
            elif (
                self.config.structured_output_mode == "json_schema"
                and capabilities.supports_json_schema is False
            ):
                status = ProbeStatus.DEGRADED
                detail = "configured model does not advertise JSON Schema output support"
            elif (
                self.config.structured_output_mode == "json_object"
                and capabilities.supports_json_object is False
            ):
                status = ProbeStatus.DEGRADED
                detail = "configured model does not advertise JSON object output support"
            result = CapabilityProbeResult(
                status=status,
                endpoint=self.config.capabilities_url,
                capabilities=capabilities,
                latency_ms=(time.perf_counter() - started) * 1000,
                http_status=response.status_code,
                detail=detail,
                checked_at=checked_at,
            )
            self._remember_capabilities(result.capabilities)
            return result
        except ModelHTTPError as error:
            result = CapabilityProbeResult(
                status=ProbeStatus.DEGRADED if error.retryable else ProbeStatus.UNAVAILABLE,
                endpoint=self.config.capabilities_url,
                capabilities=default_capabilities,
                latency_ms=(time.perf_counter() - started) * 1000,
                http_status=error.status_code,
                detail=str(error),
                checked_at=checked_at,
            )
            self._remember_capabilities(result.capabilities)
            return result
        except (ModelTransportError, ModelProtocolError) as error:
            result = CapabilityProbeResult(
                status=ProbeStatus.UNAVAILABLE,
                endpoint=self.config.capabilities_url,
                capabilities=default_capabilities,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=str(error),
                checked_at=checked_at,
            )
            self._remember_capabilities(result.capabilities)
            return result


def _optional_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return bool(value)


def _capability_bool(values: Mapping[str, Any], name: str) -> bool | None:
    """Read provider capability aliases while preserving unknown as ``None``."""

    aliases = {
        "supports_multimodal": ("supports_multimodal", "supports_vision", "multimodal", "vision")
    }
    for key in aliases.get(name, (name,)):
        if key in values:
            return _optional_bool(values.get(key))
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
