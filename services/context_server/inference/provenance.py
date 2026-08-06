"""Serializable model and prompt provenance records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts import PromptBundle


@dataclass(frozen=True)
class PromptProvenance:
    prompt_version: str
    prompt_hash: str
    system_prompt_hash: str
    user_prompt_hash: str
    schema_hash: str
    contract_name: str
    contract_version: str

    @classmethod
    def from_prompt(cls, prompt: PromptBundle) -> PromptProvenance:
        return cls(
            prompt_version=prompt.prompt_version,
            prompt_hash=prompt.prompt_hash,
            system_prompt_hash=prompt.system_prompt_hash,
            user_prompt_hash=prompt.user_prompt_hash,
            schema_hash=prompt.schema_hash,
            contract_name=prompt.contract.name,
            contract_version=prompt.contract.version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "system_prompt_hash": self.system_prompt_hash,
            "user_prompt_hash": self.user_prompt_hash,
            "schema_hash": self.schema_hash,
            "contract_name": self.contract_name,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class ModelProvenance:
    client_name: str
    mode: str
    configured_model: str
    returned_model: str | None
    endpoint: str
    response_id: str | None
    structured_output_mode: str | None
    request_id: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    model_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_name": self.client_name,
            "mode": self.mode,
            "configured_model": self.configured_model,
            "returned_model": self.returned_model,
            "endpoint": self.endpoint,
            "model_revision": self.model_revision,
            "response_id": self.response_id,
            "structured_output_mode": self.structured_output_mode,
            "request_id": self.request_id,
            "usage": dict(self.usage),
        }


@dataclass(frozen=True)
class InferenceProvenance:
    """The minimum provenance needed to reproduce/interpret a job result."""

    request_id: str
    attempt: int
    prompt: PromptProvenance
    model: ModelProvenance
    started_at: float
    finished_at: float
    latency_ms: float
    client_version: str = "context-server-inference-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt": self.attempt,
            "prompt": self.prompt.to_dict(),
            "model": self.model.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latency_ms": self.latency_ms,
            "client_version": self.client_version,
        }

    @property
    def prompt_hash(self) -> str:
        return self.prompt.prompt_hash

    @property
    def schema_hash(self) -> str:
        return self.prompt.schema_hash
