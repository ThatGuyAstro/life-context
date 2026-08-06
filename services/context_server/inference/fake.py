"""Deterministic fake model client for unit and integration tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .client import ChatMessage, ModelResponse
from .contracts import JsonSchemaContract, PromptBundle, canonical_json
from .probes import CapabilityProbeResult, HealthProbeResult, ModelCapabilities, ProbeStatus


@dataclass(frozen=True)
class FakeCall:
    index: int
    messages: tuple[dict[str, Any], ...]
    contract_name: str | None
    request_id: str | None


FakeOutput = Any


class DeterministicFakeModelClient:
    """A scripted client with stable ids and no network/time randomness.

    Each item in ``outputs`` may be a string, JSON-compatible value, a
    ``ModelResponse``, an exception instance, or a callable accepting the
    normalized messages and contract.  JSON-compatible values are serialized
    canonically, making provenance and test assertions repeatable.
    """

    def __init__(
        self,
        outputs: Sequence[FakeOutput] = (),
        *,
        model_name: str = "fake",
        returned_model: str | None = None,
        mode: str = "managed",
        endpoint: str = "fake://model",
        capabilities: Mapping[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
        model_revision: str | None = None,
    ) -> None:
        self._outputs = list(outputs)
        self._index = 0
        self._clock = clock or (lambda: 0.0)
        self._returned_model = returned_model or model_name
        self._mode = mode
        self._endpoint = endpoint
        self._capabilities = dict(capabilities or {})
        self._calls: list[FakeCall] = []
        self._model_name = model_name
        self._model_revision = model_revision

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def calls(self) -> tuple[FakeCall, ...]:
        return tuple(self._calls)

    @property
    def provenance_metadata(self) -> dict[str, Any]:
        return {
            "client_name": self._model_name,
            "mode": self._mode,
            "configured_model": self._model_name,
            "endpoint": self._endpoint,
            "structured_output_mode": "fake",
            "model_revision": self._model_revision,
        }

    def complete(
        self,
        messages: PromptBundle | Sequence[ChatMessage | Mapping[str, Any]],
        *,
        contract: JsonSchemaContract | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_id: str | None = None,
    ) -> ModelResponse:
        if isinstance(messages, PromptBundle):
            contract = contract or messages.contract
            normalized = messages.messages
        else:
            normalized = messages
        serialized: list[dict[str, Any]] = []
        for message in normalized:
            serialized.append(
                message.to_dict() if isinstance(message, ChatMessage) else dict(message)
            )
        self._calls.append(
            FakeCall(
                index=self._index + 1,
                messages=tuple(serialized),
                contract_name=contract.name if contract else None,
                request_id=request_id,
            )
        )
        if self._index >= len(self._outputs):
            raise AssertionError("deterministic fake has no scripted output remaining")
        output = self._outputs[self._index]
        self._index += 1
        if isinstance(output, BaseException):
            raise output
        if callable(output):
            output = output(tuple(serialized), contract)
            if isinstance(output, BaseException):
                raise output
        if isinstance(output, ModelResponse):
            return output
        text = output if isinstance(output, str) else canonical_json(output)
        return ModelResponse(
            text=text,
            model=self._returned_model,
            response_id=f"fake-response-{self._index:04d}",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            finish_reason="stop",
            created=int(self._clock()),
            raw={"fake": True, "index": self._index},
            request_id=request_id,
        )

    def health_probe(self) -> HealthProbeResult:
        return HealthProbeResult(
            status=ProbeStatus.HEALTHY,
            endpoint=self._endpoint,
            latency_ms=0.0,
            http_status=200,
            detail="deterministic fake",
            checked_at=self._clock(),
            raw={"fake": True},
        )

    def capability_probe(self) -> CapabilityProbeResult:
        caps = ModelCapabilities(
            model=self._returned_model,
            available_models=(self._returned_model,),
            context_window=self._capabilities.get("context_window"),
            supports_chat=self._capabilities.get("supports_chat", True),
            supports_streaming=self._capabilities.get("supports_streaming", False),
            supports_tools=self._capabilities.get("supports_tools", False),
            supports_json_object=self._capabilities.get("supports_json_object", True),
            supports_json_schema=self._capabilities.get("supports_json_schema", True),
            source="fake",
            raw=dict(self._capabilities),
        )
        return CapabilityProbeResult(
            status=ProbeStatus.HEALTHY,
            endpoint=self._endpoint,
            capabilities=caps,
            latency_ms=0.0,
            http_status=200,
            detail="deterministic fake",
            checked_at=self._clock(),
        )
