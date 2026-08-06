"""Runtime-owned inference orchestration for the local context server.

The lower-level inference package is provider-neutral.  This module is the
small composition seam that connects it to SQLite jobs and local artifacts:
jobs carry a strict prompt plus ``artifact://<sha256>`` image references, and
the model client resolves those references to data URLs only when a request is
actually sent.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ..query.stream import Event
from .client import ModelClient, ModelResponse
from .config import ModelClientConfig
from .contracts import JsonSchemaContract, PromptBundle, build_strict_json_prompt
from .jobs import InferenceJobResult
from .manager import build_model_client
from .probes import (
    CapabilityProbeResult,
    HealthProbeResult,
    ModelCapabilities,
    ModelProbeResult,
    ProbeStatus,
)
from .sqlite_job_adapter import SQLitePersistentJobAdapter
from .worker import InferenceWorker, RetryPolicy

OBSERVATION_CONTRACT = JsonSchemaContract(
    name="life_context_observation_v1",
    version="1",
    prompt_version="life-context-observation-v1",
    schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
            "activity": {"type": "string", "minLength": 1, "maxLength": 2000},
            "project": {"type": ["string", "null"], "maxLength": 1000},
            "application": {"type": ["string", "null"], "maxLength": 1000},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "tags": {
                "type": "array",
                "items": {"type": "string", "maxLength": 200},
                "maxItems": 32,
            },
        },
        "required": ["summary", "activity", "project", "application", "confidence", "tags"],
    },
)


def build_observation_prompt(
    *,
    event_id: str,
    source_id: str,
    event_type: str,
    occurred_at: Any,
    payload: Any,
    metadata: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> PromptBundle:
    """Build a deterministic strict-JSON prompt for one source event."""

    context = {
        "event_id": event_id,
        "source_id": source_id,
        "event_type": event_type,
        "occurred_at": _json_value(occurred_at),
        "payload": _json_value(payload),
        "metadata": _json_value(metadata),
    }
    prompt = build_strict_json_prompt(
        (
            "Analyze the supplied desktop context. Describe the visible or "
            "recorded activity as one concise observation. Use null when a "
            "project or application cannot be identified. Return only the "
            "strict JSON object requested by the schema."
        ),
        OBSERVATION_CONTRACT,
        context=context,
    )

    blocks: list[Mapping[str, Any]] = [{"type": "text", "text": prompt.user_prompt}]
    for ref in artifacts:
        kind = str(ref.get("kind", "")).lower()
        media_type = str(ref.get("media_type", "")).lower()
        digest = ref.get("sha256")
        if kind not in {"image", "screenshot", "contact_sheet"} and not media_type.startswith(
            "image/"
        ):
            continue
        if digest:
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"artifact://{digest}", "detail": "auto"},
                }
            )
    return replace(prompt, user_content=tuple(dict(block) for block in blocks))


class ArtifactResolvingModelClient:
    """Wrap an OpenAI-compatible client and resolve local image references."""

    def __init__(self, client: ModelClient, artifact_store: Any) -> None:
        self.client = client
        self.artifact_store = artifact_store

    @property
    def model_name(self) -> str:
        return self.client.model_name

    @property
    def provenance_metadata(self) -> Mapping[str, Any]:
        return getattr(self.client, "provenance_metadata", {})

    def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        if isinstance(messages, PromptBundle) and messages.user_content is not None:
            resolved: list[Mapping[str, Any]] = []
            for block in messages.user_content:
                candidate = dict(block)
                image = candidate.get("image_url")
                if isinstance(image, Mapping):
                    image_data = dict(image)
                    url = image_data.get("url")
                    if isinstance(url, str) and url.startswith("artifact://"):
                        digest = url.removeprefix("artifact://")
                        record = self.artifact_store.storage.get_artifact(digest)
                        if record is None:
                            raise FileNotFoundError(f"inference artifact not found: {digest}")
                        raw = self.artifact_store.read(digest)
                        encoded = base64.b64encode(raw).decode("ascii")
                        image_data["url"] = f"data:{record.media_type};base64,{encoded}"
                    candidate["image_url"] = image_data
                resolved.append(candidate)
            messages = replace(messages, user_content=tuple(resolved))
        return self.client.complete(messages, **kwargs)

    def health_probe(self) -> Any:
        return self.client.health_probe()

    def capability_probe(self) -> Any:
        return self.client.capability_probe()


class InferenceRuntimeController:
    """Own optional model workers without making capture depend on inference."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.client: ArtifactResolvingModelClient | None = None
        self.adapter: SQLitePersistentJobAdapter | None = None
        self._client_config: ModelClientConfig | None = None
        self._probe_lock = threading.Lock()
        self.last_error: str | None = None
        self.started_at: datetime | None = None
        self.processed = 0

    @property
    def configured(self) -> bool:
        return bool(self.runtime._config.get("model_endpoint"))

    def _build_client(self) -> ArtifactResolvingModelClient:
        api_key = os.environ.get("CONTEXT_MODEL_API_KEY")
        structured_output_mode = os.environ.get("CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE")
        if structured_output_mode is None:
            structured_output_mode = self.runtime._config.get(
                "model_structured_output_mode", "auto"
            )
        config = ModelClientConfig(
            name="default",
            model=self.runtime._config.get("model_name") or "default",
            base_url=self.runtime._config.get("model_endpoint") or "",
            mode="external",
            api_key=api_key,
            structured_output_mode=structured_output_mode,
            model_revision=self.runtime._config.get("model_revision"),
            requires_multimodal=os.environ.get("CONTEXT_MODEL_REQUIRES_MULTIMODAL", "true"),
        )
        self._client_config = config
        return ArtifactResolvingModelClient(
            build_model_client(config),
            self.runtime.storage.artifacts,
        )

    def start(self) -> None:
        if not self.configured or self.threads:
            return
        self.stop_event.clear()
        self.last_error = None
        self._client_config = None
        try:
            self.client = self._build_client()
            self.adapter = SQLitePersistentJobAdapter(
                self.runtime.storage,
                worker_id="context-server-inference",
                on_complete=self._on_complete,
            )
        except Exception as error:
            self.client = None
            self.adapter = None
            self._client_config = None
            self.last_error = f"inference configuration: {error}"
            return
        worker_count = max(1, int(self.runtime._config.get("worker_count", 1)))
        for index in range(worker_count):
            worker = InferenceWorker(
                self.adapter,
                self.client,
                default_model="default",
                batch_size=1,
                retry_policy=RetryPolicy(max_attempts=3, base_delay_s=5.0),
                idle_sleep_s=0.5,
            )
            thread = threading.Thread(
                target=self._run_worker,
                args=(worker,),
                name=f"context-inference-{index + 1}",
                daemon=True,
            )
            self.threads.append(thread)
            thread.start()
        self.started_at = datetime.now(UTC)

    def _run_worker(self, worker: InferenceWorker) -> None:
        try:
            worker.run_forever(stop_event=self.stop_event)
        except Exception as error:  # pragma: no cover - exercised by live failures
            self.last_error = f"inference worker: {error}"

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3.0)
        self.threads.clear()
        self.client = None
        self.adapter = None
        self._client_config = None

    def _client_metadata(self) -> dict[str, Any]:
        if self.client is None:
            return {}
        metadata = getattr(self.client, "provenance_metadata", {})
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    def _status_endpoint(self, metadata: Mapping[str, Any]) -> str | None:
        endpoint = metadata.get("endpoint")
        if endpoint:
            return str(endpoint)
        if self._client_config is not None:
            return self._client_config.safe_base_url
        configured = self.runtime._config.get("model_endpoint")
        if not configured:
            return None
        try:
            return ModelClientConfig(
                name="status",
                model="status",
                base_url=str(configured),
            ).safe_base_url
        except Exception:
            return str(configured)

    def _probe_model(self, *, endpoint: str | None, model: str | None) -> ModelProbeResult | None:
        client = self.client
        if client is None:
            return None
        probe_endpoint = endpoint or "unknown"
        probe_model = model or "unknown"
        with self._probe_lock:
            if self.client is not client:
                return None
            try:
                health = client.health_probe()
                if not isinstance(health, HealthProbeResult):
                    raise TypeError("health probe returned an invalid result")
            except Exception as error:
                health = HealthProbeResult(
                    status=ProbeStatus.UNAVAILABLE,
                    endpoint=probe_endpoint,
                    detail=f"health probe failed: {error}",
                    checked_at=time.time(),
                )
            try:
                capabilities = client.capability_probe()
                if not isinstance(capabilities, CapabilityProbeResult):
                    raise TypeError("capability probe returned an invalid result")
            except Exception as error:
                capabilities = CapabilityProbeResult(
                    status=ProbeStatus.UNAVAILABLE,
                    endpoint=probe_endpoint,
                    capabilities=ModelCapabilities(model=probe_model, source="runtime"),
                    detail=f"capability probe failed: {error}",
                    checked_at=time.time(),
                )
            # A capability document can be reachable while the provider is
            # still starting or otherwise not ready to serve inference. Keep
            # the nested diagnostic honest by applying the health result as a
            # floor, without changing the raw capability payload or HTTP code.
            if health.status is ProbeStatus.UNAVAILABLE and capabilities.status not in {
                ProbeStatus.UNAVAILABLE
            }:
                capabilities = replace(
                    capabilities,
                    status=ProbeStatus.UNAVAILABLE,
                    detail=(
                        f"provider health is unavailable; {capabilities.detail}"
                    ).strip(),
                )
            elif (
                health.status is ProbeStatus.DEGRADED
                and capabilities.status is ProbeStatus.HEALTHY
            ):
                capabilities = replace(
                    capabilities,
                    status=ProbeStatus.DEGRADED,
                    detail=(f"provider health is degraded; {capabilities.detail}").strip(),
                )
            return ModelProbeResult(health=health, capabilities=capabilities)

    def status(self) -> dict[str, Any]:
        configured = self.configured
        metadata = self._client_metadata()
        endpoint = self._status_endpoint(metadata)
        configured_model = metadata.get("configured_model") or self.runtime._config.get(
            "model_name"
        )
        model = str(configured_model) if configured_model else None
        revision_value = metadata.get("model_revision") or self.runtime._config.get(
            "model_revision"
        )
        revision = str(revision_value) if revision_value else None
        if not configured:
            worker_state = "not_configured"
        elif self.last_error:
            worker_state = "error"
        elif self.threads:
            worker_state = "running"
        else:
            worker_state = "configured"

        probe = (
            self._probe_model(endpoint=endpoint, model=model)
            if configured and self.client
            else None
        )
        if not configured:
            model_status = "not_configured"
            state = worker_state
        elif self.last_error:
            model_status = "error"
            state = worker_state
        elif probe is None:
            model_status = "unknown"
            state = worker_state
        else:
            model_status = probe.status.value
            state = (
                probe.status.value
                if probe.status in {ProbeStatus.DEGRADED, ProbeStatus.UNAVAILABLE}
                else worker_state
            )
        return {
            "status": state,
            "worker_status": worker_state,
            "model_status": model_status,
            "availability": model_status,
            "configured": configured,
            "endpoint": endpoint,
            "model": model,
            "revision": revision,
            "model_revision": revision,
            "structured_output_mode": metadata.get("structured_output_mode"),
            "configured_structured_output_mode": metadata.get(
                "configured_structured_output_mode"
            ),
            "health": probe.health.to_dict() if probe else None,
            "capabilities": probe.capabilities.to_dict() if probe else None,
            "worker_count": int(self.runtime._config.get("worker_count", 1)),
            "active_workers": len(self.threads),
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "processed": self.processed,
        }

    def _on_complete(self, job: Any, result: InferenceJobResult) -> None:
        payload = job.payload if isinstance(job.payload, Mapping) else {}
        value = (
            result.value if isinstance(result.value, Mapping) else {"summary": str(result.value)}
        )
        summary = str(value.get("summary") or value.get("activity") or result.raw_text)
        source_id = payload.get("source_id")
        observed_at = payload.get("observed_at") or datetime.now(UTC)
        artifacts = payload.get("artifacts")
        artifact_digest = None
        if isinstance(artifacts, list):
            for item in artifacts:
                if isinstance(item, Mapping) and item.get("sha256"):
                    artifact_digest = str(item["sha256"])
                    break
        metadata = {
            "inference": result.provenance.to_dict(),
            "model_revision": self.runtime._config.get("model_revision") or None,
            "prompt_revision": result.provenance.prompt.prompt_version,
            "raw_response": result.raw_text,
            "validation_status": "valid",
            "confidence": value.get("confidence"),
            "source_event_id": payload.get("source_event_id"),
        }
        record = self.runtime.storage.create_observation(
            capture_id=payload.get("capture_id"),
            source_id=str(source_id) if source_id else None,
            kind="inference_observation",
            observed_at=observed_at,
            text_content=summary,
            payload=value,
            metadata=metadata,
            retention_tier="derived",
            observation_id=f"inference:{job.id}",
            idempotency_key=f"inference-observation:{job.id}",
            artifact_digest=artifact_digest,
        )
        self.processed += 1
        self.runtime.events.publish(
            Event(
                "context.inference",
                id=record.id,
                data=self.runtime._observation_dict(record),
            )
        )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    return str(value)


__all__ = [
    "ArtifactResolvingModelClient",
    "InferenceRuntimeController",
    "OBSERVATION_CONTRACT",
    "build_observation_prompt",
]
