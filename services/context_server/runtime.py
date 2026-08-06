"""Composition root for the local-first Life Context MVP.

The feature agents keep capture, storage, inference, and query code in small
packages.  This module is the intentionally boring seam that wires those
pieces together for a real local server: a durable SQLite store, content
addressed artifacts, FTS-backed retrieval, queue metrics, and lifecycle/config
operations used by the API, CLI, and dashboard.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from contracts import CaptureEnvelope
except ModuleNotFoundError:  # Running from a source checkout without an install.
    from packages.contracts.python.contracts import CaptureEnvelope

from .config import (
    MODEL_STRUCTURED_OUTPUT_MODES,
    Settings,
    load_persisted_config,
    save_persisted_config,
)
from .inference.client import ChatMessage
from .inference.runtime_worker import InferenceRuntimeController, build_observation_prompt
from .query.models import QueryRequest, SearchHit
from .query.stream import Event, EventStreamStub
from .storage import RetentionPolicy, RetentionTier, SQLiteStorage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _event_text(payload: Any) -> str:
    """Choose a human-searchable projection without mutating raw payloads."""

    if isinstance(payload, Mapping):
        for key in ("raw_text", "text", "clipboard_text", "window_title", "title", "url"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return _json_text(payload)


def _row_count(connection: sqlite3.Connection, query: str, params: Sequence[Any] = ()) -> int:
    row = connection.execute(query, tuple(params)).fetchone()
    return int(row[0]) if row else 0


_RUNTIME_UPDATE_KEYS = frozenset(
    {
        "capture_interval_seconds",
        "worker_count",
        "queue_size",
        "configured_roots",
        "model_endpoint",
        "model_name",
        "model_revision",
        "model_structured_output_mode",
        "source_routing",
        "routes",
        "retention",
    }
)
_SENSITIVE_CONFIG_KEY_PARTS = frozenset(
    {
        "api_key",
        "access_key",
        "auth_token",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_DROP_CONFIG_VALUE = object()


def _normalise_config_key(value: Any) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _is_sensitive_config_key(value: Any) -> bool:
    key = _normalise_config_key(value)
    return (
        key in _SENSITIVE_CONFIG_KEY_PARTS
        or any(key.endswith(suffix) for suffix in ("_api_key", "_access_key", "_private_key"))
        or bool(
            set(key.split("_"))
            & {"credential", "credentials", "password", "secret", "token"}
        )
    )


def _safe_json_config_value(value: Any, *, key: Any = None) -> Any:
    """Copy JSON-like configuration while dropping secret-bearing keys."""

    if key is not None and _is_sensitive_config_key(key):
        return _DROP_CONFIG_VALUE
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            safe_key = str(raw_key)
            safe_value = _safe_json_config_value(raw_value, key=safe_key)
            if safe_value is not _DROP_CONFIG_VALUE:
                result[safe_key] = safe_value
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = []
        for item in value:
            safe_value = _safe_json_config_value(item)
            if safe_value is not _DROP_CONFIG_VALUE:
                result.append(safe_value)
        return result
    return str(value)


def _copy_config(value: Any) -> Any:
    """Return an independent JSON-compatible copy of a runtime value."""

    if isinstance(value, Mapping):
        return {str(key): _copy_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_config(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_config(item) for item in value]
    return value


def _parse_config_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, Mapping) else None


def _safe_model_endpoint(value: Any) -> str:
    """Validate model endpoints while removing URL-embedded secrets.

    Model endpoints are either HTTPS or loopback HTTP.  Query strings and
    fragments are dropped because they are not part of the API root and often
    carry accidental credentials.  Userinfo is also removed before the safe
    endpoint is persisted or exposed in diagnostics.
    """

    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("model_endpoint must be an absolute http(s) URL")
    host = parsed.hostname.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if parsed.scheme == "http" and not is_loopback:
        raise ValueError("non-loopback HTTP model_endpoint is not allowed; use HTTPS")
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _number_config_value(value: Any, *, key: str, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"invalid {key}")
    try:
        converted: int | float = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid {key}") from exc
    if not math.isfinite(float(converted)):
        raise ValueError(f"invalid {key}")
    if converted < (1 if key in {"worker_count", "queue_size", "capture_interval_seconds"} else 0):
        raise ValueError(f"invalid {key}")
    return converted


def _normalise_runtime_update(key: str, value: Any) -> Any:
    """Validate, canonicalize, and redact one API-owned setting."""

    if key in {"worker_count", "queue_size"}:
        return _number_config_value(value, key=key, integer=True)
    if key == "capture_interval_seconds":
        return _number_config_value(value, key=key)
    if key == "configured_roots":
        safe = _safe_json_config_value(value, key=key)
        if not isinstance(safe, list):
            raise ValueError("invalid configured_roots")
        return [str(item).strip() for item in safe if str(item).strip()]
    if key == "model_endpoint":
        return _safe_model_endpoint(value)
    if key in {"model_name", "model_revision"}:
        return "" if value is None else str(value)
    if key == "model_structured_output_mode":
        mode = str(value).strip().lower()
        if mode not in MODEL_STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                "model_structured_output_mode must be one of "
                f"{sorted(MODEL_STRUCTURED_OUTPUT_MODES)}"
            )
        return mode
    if key == "retention":
        parsed = _parse_config_mapping(value)
        if parsed is None:
            raise ValueError("invalid retention")
        safe = _safe_json_config_value(parsed, key=key)
        if not isinstance(safe, dict):
            raise ValueError("invalid retention")
        if "raw_hours" in safe:
            safe["raw_hours"] = _number_config_value(safe["raw_hours"], key="raw_hours")
        elif "raw_payload_days" in safe:
            safe["raw_payload_days"] = _number_config_value(
                safe["raw_payload_days"], key="raw_payload_days"
            )
            safe["raw_hours"] = safe["raw_payload_days"] * 24
        if "keyframe_days" in safe:
            safe["keyframe_days"] = _number_config_value(
                safe["keyframe_days"], key="keyframe_days"
            )
        return safe
    if key in {"source_routing", "routes"}:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid {key}") from exc
        safe = _safe_json_config_value(value, key=key)
        if safe is _DROP_CONFIG_VALUE or not isinstance(safe, (dict, list)):
            raise ValueError(f"invalid {key}")
        return safe
    raise ValueError(f"unsupported runtime setting: {key}")


def _normalise_persisted_config(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, value in values.items():
        key = str(raw_key)
        if key not in _RUNTIME_UPDATE_KEYS:
            continue
        try:
            result[key] = _normalise_runtime_update(key, value)
        except (TypeError, ValueError):
            # A hand-edited or interrupted sidecar must not prevent the
            # server from starting.  API writes are validated before saving.
            continue
    return result


class SQLiteFtsBackend:
    """Adapter from the storage records to the query orchestrator contract."""

    def __init__(self, runtime: "ContextRuntime") -> None:
        self.runtime = runtime

    def search(self, request: QueryRequest | Any) -> list[SearchHit]:
        canonical = QueryRequest.from_value(request)
        records = self.runtime._matching_observations(canonical)
        if not records and self.runtime._should_use_recent_context(canonical):
            records = self.runtime._recent_context_records(canonical)
        return [self.runtime._search_hit(record) for record in records[: canonical.limit]]


class RuntimeAnswerer:
    """Evidence-grounded local answerer.

    The configured local model receives only the retrieved evidence and the
    user's question. If it is unavailable, the server still returns a useful
    deterministic evidence summary.
    """

    def __init__(self, runtime: "ContextRuntime") -> None:
        self.runtime = runtime

    @staticmethod
    def _fallback_detail(item: SearchHit) -> str | None:
        """Turn common source payloads into readable, appropriately cautious evidence."""

        text = item.text.strip().replace("\x00", " ")
        if not text:
            return None
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None

        if not isinstance(payload, Mapping):
            compact = " ".join(text.split())
            return compact[:277] + "..." if len(compact) > 280 else compact

        for key in ("summary", "activity", "description", "window_title", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                compact = " ".join(value.split())
                return compact[:277] + "..." if len(compact) > 280 else compact

        commands = payload.get("commands")
        if isinstance(commands, Sequence) and not isinstance(commands, (str, bytes)):
            command_text = [str(command).strip() for command in commands if str(command).strip()]
            projects: list[str] = []
            tools: list[str] = []
            for command in command_text:
                cd_match = re.match(r"(?i)^cd\s+(.+)$", command)
                if cd_match:
                    target = cd_match.group(1).strip(" .'\"\\/").replace("/", "\\")
                    leaf = target.rsplit("\\", 1)[-1]
                    if leaf.casefold() not in {"", "desktop", "dev"} and leaf not in projects:
                        projects.append(leaf)
                lowered = command.casefold()
                for token, label in (
                    ("claude", "Claude"),
                    ("codex", "Codex"),
                    ("git ", "Git"),
                    ("pytest", "pytest"),
                    ("pnpm", "pnpm"),
                    ("npm ", "npm"),
                    ("uv ", "uv"),
                    ("python", "Python"),
                ):
                    if token in lowered and label not in tools:
                        tools.append(label)
            details = []
            if projects:
                details.append("projects " + ", ".join(projects[:5]))
            if tools:
                details.append("tools " + ", ".join(tools[:4]))
            if details:
                return (
                    "A terminal-history snapshot captured in this period referenced "
                    + " and ".join(details)
                    + "; the snapshot alone does not prove every command ran during the period."
                )

        cwd = payload.get("cwd")
        event_kind = str(payload.get("event_kind") or "").replace("_", " ")
        if isinstance(cwd, str) and cwd.strip():
            prefix = f"{event_kind.capitalize()}: " if event_kind else "Context from "
            return f"{prefix}{cwd.strip()}."
        url = payload.get("url")
        if isinstance(url, str) and url.strip():
            return f"Browser context included {url.strip()}."
        return None

    def _fallback_summary(
        self, request: QueryRequest, evidence: Sequence[SearchHit]
    ) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(evidence[:12], start=1):
            detail = self._fallback_detail(item)
            if not detail or detail.casefold() in seen:
                continue
            seen.add(detail.casefold())
            lines.append(f"- {detail} [{index}]")
            if len(lines) == 5:
                break
        if not lines:
            return (
                "I found context in the requested period, but it did not contain enough "
                "readable activity detail to produce a reliable recap."
            )
        window = ""
        if request.start is not None or request.end is not None:
            window = " during the requested time window"
        return (
            f"I found {len(evidence)} relevant context item(s){window}. "
            "Here is the evidence-backed recap:\n" + "\n".join(lines)
        )

    def answer(self, request: QueryRequest, evidence: Sequence[SearchHit]) -> dict[str, Any]:
        if not evidence:
            return {
                "text": "No matching context was found in the local archive.",
                "confidence": 0.0,
                "status": "no_results",
                "metadata": {"model_configured": bool(self.runtime._config.get("model_endpoint"))},
            }
        client = self.runtime.inference.client
        if client is not None:
            evidence_text = []
            for index, item in enumerate(evidence[:12], start=1):
                excerpt = item.text.strip().replace("\x00", " ")
                if len(excerpt) > 600:
                    excerpt = excerpt[:597] + "..."
                evidence_text.append(
                    f"[{index}] source={item.source or item.id} "
                    f"timestamp={item.timestamp or 'unknown'}\n{excerpt}"
                )
            messages = (
                ChatMessage(
                    role="system",
                    content=(
                        "Answer the user's question using only the supplied Life Context "
                        "evidence. Be concise and concrete. Cite supporting evidence with "
                        "bracketed numbers such as [1]. If the evidence is insufficient, "
                        "say what is missing. Do not invent activity or timestamps. Keep "
                        "your internal analysis brief and produce the answer promptly."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=(
                        f"Question: {request.query.strip()}\n\n"
                        "Evidence:\n" + "\n\n".join(evidence_text)
                    ),
                ),
            )
            try:
                response = client.complete(
                    messages,
                    temperature=0.1,
                    # Gemma 4 may spend several hundred tokens in a separate
                    # reasoning_content field. Leave enough room for the
                    # user-facing content instead of accepting an empty answer.
                    max_tokens=1024,
                    request_id=f"query:{uuid.uuid4().hex}",
                )
                text = response.text.strip()
                if text:
                    return {
                        "text": text,
                        "confidence": 0.8,
                        "status": "model_synthesized",
                        "metadata": {
                            "model_configured": True,
                            "model_name": self.runtime._config.get("model_name") or None,
                            "response_id": response.response_id,
                        },
                    }
            except Exception as error:
                model_error = error.__class__.__name__
            else:
                model_error = "empty_response"
        else:
            model_error = None
        return {
            "text": self._fallback_summary(request, evidence),
            "confidence": min(0.75, 0.45 + 0.05 * len(evidence)),
            "status": "fallback_evidence_summary",
            "metadata": {
                "model_configured": bool(self.runtime._config.get("model_endpoint")),
                "model_name": self.runtime._config.get("model_name") or None,
                "model_error": model_error,
            },
        }


class ContextRuntime:
    """Durable application services shared by HTTP, CLI, and dashboard."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data_dir = Path(settings.data_dir).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._base_config = self._build_base_config()
        self._persisted_config = _normalise_persisted_config(
            load_persisted_config(self.data_dir)
        )
        self._config = self._effective_config()
        retention = self._retention_policy()
        self.storage = SQLiteStorage(
            self.data_dir / "context.sqlite3",
            artifact_root=self.data_dir / "artifacts",
            retention_policy=retention,
        )
        self.events = EventStreamStub()
        self.fts = SQLiteFtsBackend(self)
        self.answerer = RuntimeAnswerer(self)
        self.inference = InferenceRuntimeController(self)
        self._running = False
        # Service readiness is distinct from capture state.  A paused capture
        # session still has a healthy HTTP/runtime service, while a runtime
        # that has not entered its lifespan (or has shut down) is not ready.
        self._ready = False
        self._started_at: datetime | None = None
        self._paused_at: datetime | None = None
        self._last_ingest_at: datetime | None = None
        self._processed_events = 0
        self._last_error: str | None = None
        self.storage.recover_jobs()

    def _build_base_config(self) -> dict[str, Any]:
        """Build effective settings before applying API-owned persistence."""

        config: dict[str, Any] = {
            "data_dir": str(self.data_dir),
            "capture_interval_seconds": self.settings.capture_interval_seconds,
            "worker_count": self.settings.worker_count,
            "queue_size": self.settings.queue_size,
            "retention": {
                "raw_hours": self.settings.raw_retention_hours,
                "keyframe_days": self.settings.keyframe_retention_days,
                "derived": "indefinite",
            },
            "configured_roots": list(self.settings.configured_roots),
            "model_endpoint": _safe_model_endpoint(self.settings.model_endpoint),
            "model_name": self.settings.model_name,
            "model_revision": self.settings.model_revision,
            "model_structured_output_mode": self.settings.model_structured_output_mode,
            "source_routing": {
                "clipboard": {"route": "local", "enabled": True},
                "screenshots": {"route": "local", "enabled": True},
            },
            "privacy_warning": (
                "Raw clipboard text and screenshots are stored as plaintext in the configured "
                "archive. Clipboard is local-only by default; configure remote routing explicitly."
            ),
        }
        source_values = getattr(self.settings, "_source_values", {})
        if isinstance(source_values, Mapping):
            for key in ("source_routing", "routes", "retention"):
                if key not in source_values:
                    continue
                try:
                    value = _normalise_runtime_update(key, source_values[key])
                except (TypeError, ValueError):
                    continue
                config[key] = value
            if "raw_hours" in source_values and "retention" not in source_values:
                try:
                    config["retention"]["raw_hours"] = _number_config_value(
                        source_values["raw_hours"], key="raw_hours"
                    )
                except (TypeError, ValueError):
                    pass
            if "keyframe_days" in source_values and "retention" not in source_values:
                try:
                    config["retention"]["keyframe_days"] = _number_config_value(
                        source_values["keyframe_days"], key="keyframe_days"
                    )
                except (TypeError, ValueError):
                    pass
        return config

    def _source_values(self) -> Mapping[str, Any]:
        values = getattr(self.settings, "_source_values", {})
        return values if isinstance(values, Mapping) else {}

    def _retention_source_fields(self) -> tuple[bool, bool]:
        source_values = self._source_values()
        raw_configured = any(
            key in source_values
            for key in ("raw_retention_hours", "raw_hours", "raw_payload_days")
        )
        keyframe_configured = any(
            key in source_values for key in ("keyframe_retention_days", "keyframe_days")
        )
        if "retention" in source_values:
            retention = _parse_config_mapping(source_values["retention"])
            if retention is None:
                raw_configured = True
                keyframe_configured = True
            else:
                raw_configured = raw_configured or any(
                    key in retention
                    for key in ("raw_hours", "raw_payload_days", "raw_retention_hours")
                )
                keyframe_configured = keyframe_configured or any(
                    key in retention for key in ("keyframe_days", "keyframe_retention_days")
                )
        return raw_configured, keyframe_configured

    def _persisted_key_is_blocked(self, key: str) -> bool:
        source_values = self._source_values()
        if key in {"source_routing", "routes"}:
            return bool({"source_routing", "routes"} & set(source_values))
        if key == "retention":
            return False
        return key in source_values

    def _effective_config(self) -> dict[str, Any]:
        effective = _copy_config(self._base_config)
        raw_configured, keyframe_configured = self._retention_source_fields()
        for key, value in self._persisted_config.items():
            if key == "retention":
                retention = _copy_config(value)
                if raw_configured:
                    retention["raw_hours"] = self._base_config["retention"]["raw_hours"]
                if keyframe_configured:
                    retention["keyframe_days"] = self._base_config["retention"]["keyframe_days"]
                effective["retention"] = retention
                continue
            if not self._persisted_key_is_blocked(key):
                effective[key] = _copy_config(value)
        return effective

    def _retention_policy(self) -> RetentionPolicy:
        retention = self._config.get("retention", {})
        raw_hours = retention.get("raw_hours", self.settings.raw_retention_hours)
        keyframe_days = retention.get("keyframe_days", self.settings.keyframe_retention_days)
        return RetentionPolicy(
            raw=timedelta(hours=float(raw_hours)),
            keyframe=timedelta(days=float(keyframe_days)),
            derived=None,
        )

    def _apply_effective_config(self, previous: Mapping[str, Any]) -> None:
        if (
            previous.get("model_endpoint"),
            previous.get("model_name"),
            previous.get("model_revision"),
            previous.get("model_structured_output_mode"),
            previous.get("worker_count"),
        ) != (
            self._config.get("model_endpoint"),
            self._config.get("model_name"),
            self._config.get("model_revision"),
            self._config.get("model_structured_output_mode"),
            self._config.get("worker_count"),
        ):
            self.inference.stop()
            if self._running:
                self.inference.start()
        if previous.get("retention") != self._config.get("retention"):
            self.storage.retention_policy = self._retention_policy()

    def _update_persisted_config(self, updates: Mapping[str, Any]) -> None:
        candidate = dict(self._persisted_config)
        candidate.update(updates)
        candidate = _normalise_persisted_config(candidate)
        if candidate == self._persisted_config:
            return
        save_persisted_config(self.data_dir, candidate)
        previous = self._config
        self._persisted_config = candidate
        self._config = self._effective_config()
        self._apply_effective_config(previous)

    # ------------------------------------------------------------------
    # Lifecycle/config/status
    # ------------------------------------------------------------------
    def startup(self) -> None:
        self._running = True
        self._ready = True
        self._started_at = self._started_at or _utc_now()
        self.inference.start()
        self.events.publish(Event(type="ready", data={"service": "context-server"}))

    def shutdown(self) -> None:
        self._ready = False
        self._running = False
        self._paused_at = _utc_now()
        self.inference.stop()
        self.storage.close()

    def start(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._running = True
        self._started_at = self._started_at or _utc_now()
        self._paused_at = None
        self.inference.start()
        self.events.publish(Event(type="capture.started", data=dict(options or {})))
        return self.status()

    def pause(self) -> dict[str, Any]:
        self._running = False
        self._paused_at = _utc_now()
        self.inference.stop()
        self.events.publish(Event(type="capture.paused", data={}))
        return self.status()

    def _job_metrics(self) -> dict[str, Any]:
        connection = self.storage.connection
        counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute("SELECT status, count(*) AS count FROM jobs GROUP BY status")
        }
        oldest = connection.execute(
            "SELECT min(created_at) FROM jobs WHERE status = 'pending'"
        ).fetchone()
        oldest_pending = oldest[0] if oldest and oldest[0] else None
        age = None
        if oldest_pending:
            try:
                age = max(0.0, (_utc_now() - datetime.fromisoformat(oldest_pending)).total_seconds())
            except ValueError:
                age = None
        return {
            "pending_count": counts.get("pending", 0),
            "processing_count": counts.get("running", 0),
            "completed_count": counts.get("succeeded", 0),
            "failed_count": counts.get("failed", 0) + counts.get("dead", 0),
            "dead_lettered_count": counts.get("dead", 0),
            "oldest_pending_at": oldest_pending,
            "oldest_pending_age_seconds": age,
            "updated_at": _utc_now().isoformat(),
        }

    def status(self) -> dict[str, Any]:
        backlog = self._job_metrics()
        state = "running" if self._running else ("paused" if self._paused_at else "stopped")
        return {
            "status": state,
            "state": state,
            "ready": self._ready,
            "service": self.settings.service_name,
            "version": self.settings.version,
            "started_at": _iso(self._started_at),
            "last_event_at": _iso(self._last_ingest_at),
            "last_error": self._last_error,
            "capture_interval_seconds": self._config["capture_interval_seconds"],
            "worker_count": self._config["worker_count"],
            "queue_size": self._config["queue_size"],
            "event_count": _row_count(self.storage.connection, "SELECT count(*) FROM observations"),
            "artifact_count": _row_count(self.storage.connection, "SELECT count(*) FROM artifacts"),
            "capture_count": _row_count(self.storage.connection, "SELECT count(*) FROM captures"),
            "queue": {
                "pending": backlog["pending_count"],
                "processing": backlog["processing_count"],
                "completed": backlog["completed_count"],
                "failed": backlog["failed_count"],
                "oldest_pending_at": backlog["oldest_pending_at"],
                "oldest_pending_age_seconds": backlog["oldest_pending_age_seconds"],
                "processed_rate": self.inference.processed,
                "captured_events": self._processed_events,
                "updated_at": backlog["updated_at"],
            },
            "backlog": backlog,
            "model": {
                "endpoint": self._config["model_endpoint"] or None,
                "name": self._config["model_name"] or None,
                "revision": self._config["model_revision"] or None,
                "configured": bool(self._config["model_endpoint"]),
                **self.inference.status(),
            },
            "storage": {
                "database": str(self.data_dir / "context.sqlite3"),
                "artifact_root": str(self.data_dir / "artifacts"),
                "schema_version": self.storage.schema_version,
                "fts5": self.storage.fts5_available,
                "sqlite_vec": self.storage.sqlite_vec_status.__dict__
                if hasattr(self.storage.sqlite_vec_status, "__dict__")
                else {
                    "available": self.storage.sqlite_vec_status.available,
                    "state": self.storage.sqlite_vec_status.state,
                    "reason": self.storage.sqlite_vec_status.reason,
                },
            },
        }

    def config_operation(self, operation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(operation or {})
        op = str(payload.get("operation", "get")).lower()
        if op in {"set", "update"}:
            updates = (
                payload.get("settings")
                if isinstance(payload.get("settings"), Mapping)
                else payload
            )
            normalised_updates: dict[str, Any] = {}
            for raw_key, value in dict(updates).items():
                key = str(raw_key)
                if key not in _RUNTIME_UPDATE_KEYS:
                    continue
                normalised_updates[key] = _normalise_runtime_update(key, value)
            payload_key = payload.get("key")
            if isinstance(payload_key, str) and payload_key in _RUNTIME_UPDATE_KEYS:
                key = str(payload_key)
                normalised_updates[key] = _normalise_runtime_update(key, payload.get("value"))
            if normalised_updates:
                self._update_persisted_config(normalised_updates)
        if op == "get" and payload.get("key"):
            key = str(payload["key"])
            return {
                "key": key,
                "value": _copy_config(self._config.get(key)),
                "privacy_warning": self._config["privacy_warning"],
            }
        return {
            "settings": _copy_config(self._config),
            "privacy_warning": self._config["privacy_warning"],
        }

    def config(self, operation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.config_operation(operation)

    # ------------------------------------------------------------------
    # Ingest and artifact persistence
    # ------------------------------------------------------------------
    def _artifact_digest(self, refs: Sequence[Any]) -> str | None:
        for ref in refs:
            digest = getattr(ref, "sha256", None) or (ref.get("sha256") if isinstance(ref, Mapping) else None)
            if digest and self.storage.get_artifact(str(digest)) is not None:
                return str(digest)
        return None

    def _capture_envelope(self, raw: Mapping[str, Any], *, idempotency_key: str | None = None) -> tuple[str, int]:
        envelope = CaptureEnvelope.model_validate(raw)
        capture = self.storage.create_capture(
            capture_id=envelope.envelope_id,
            external_id=envelope.envelope_id,
            kind="capture_envelope",
            captured_at=envelope.captured_at,
            metadata={"producer": envelope.producer, **dict(envelope.metadata)},
            idempotency_key=idempotency_key or envelope.envelope_id,
        )
        for ref in envelope.artifacts:
            if self.storage.get_artifact(ref.sha256 or "") is not None and ref.sha256:
                self.storage.attach_artifact("capture", capture.id, ref.sha256, role="envelope")

        accepted = 0
        for event in envelope.events:
            source = self.storage.create_source(
                kind=event.source_kind.value,
                source_id=event.source_id,
                external_id=event.source_id,
                name=event.metadata.get("name") if isinstance(event.metadata, Mapping) else None,
                metadata={"routing": event.metadata.get("routing", {})}
                if isinstance(event.metadata, Mapping)
                else {},
            )
            artifact_digest = self._artifact_digest(event.artifacts)
            metadata = {
                **dict(event.metadata),
                "event_id": event.event_id,
                "source_kind": event.source_kind.value,
                "event_type": event.event_type.value,
                "envelope_id": envelope.envelope_id,
                "captured_at": _iso(event.captured_at),
            }
            if artifact_digest:
                metadata["artifact_digest"] = artifact_digest
            observation = self.storage.create_observation(
                capture_id=capture.id,
                source_id=source.id,
                kind=f"source_event:{event.event_type.value}",
                observed_at=event.occurred_at,
                text_content=_event_text(event.payload),
                payload=event.payload,
                metadata=metadata,
                retention_tier=RetentionTier.RAW,
                observation_id=f"observation:{event.event_id}",
                idempotency_key=event.event_id,
                artifact_digest=artifact_digest,
            )
            for ref in event.artifacts:
                if ref.sha256 and self.storage.get_artifact(ref.sha256) is not None:
                    self.storage.attach_artifact("capture", capture.id, ref.sha256, role="event")
            artifact_payload = [ref.model_dump(mode="json") for ref in event.artifacts]
            prompt = build_observation_prompt(
                event_id=event.event_id,
                source_id=source.id,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at,
                payload=event.payload,
                metadata=metadata,
                artifacts=artifact_payload,
            )
            self.storage.enqueue_job(
                "inference",
                {
                    "observation_id": observation.id,
                    "capture_id": capture.id,
                    "source_event_id": event.event_id,
                    "source_id": source.id,
                    "observed_at": event.occurred_at.isoformat(),
                    "artifacts": artifact_payload,
                    "model": "default" if self._config.get("model_endpoint") else None,
                    "prompt": prompt.to_payload(),
                    "metadata": metadata,
                },
                idempotency_key=f"inference:{observation.id}",
                max_attempts=3,
            )
            accepted += 1
            self._processed_events += 1
            self.events.publish(
                Event(
                    type="context.observation",
                    id=observation.id,
                    data=self._observation_dict(observation),
                )
            )
        self._last_ingest_at = _utc_now()
        return capture.id, accepted

    def ingest_batch(self, payload: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        raw = dict(payload)
        records = raw.get("records")
        if records is None:
            records = [raw]
        if not isinstance(records, list):
            raise ValueError("ingest payload records must be a list")
        captures: list[str] = []
        accepted = 0
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ValueError(f"record {index} must be an object")
            if "events" in record and "envelope_id" in record:
                capture_id, count = self._capture_envelope(
                    record,
                    idempotency_key=idempotency_key if len(records) == 1 else None,
                )
                captures.append(capture_id)
                accepted += count
                continue
            if "event_id" in record and "source_id" in record:
                envelope_id = f"envelope:{record['event_id']}"
                envelope = {
                    "envelope_id": envelope_id,
                    "captured_at": record.get("captured_at") or record.get("occurred_at"),
                    "source_kind": record.get("source_kind", "other"),
                    "events": [dict(record)],
                    "metadata": {"legacy_record": True},
                }
                capture_id, count = self._capture_envelope(envelope)
                captures.append(capture_id)
                accepted += count
                continue
            observation = self.observe(record)
            captures.append(str(observation["id"]))
            accepted += 1
        return {
            "accepted": accepted,
            "records": len(records),
            "captures": captures,
            "idempotency_key": idempotency_key,
            "queue": self._job_metrics(),
        }

    def put_artifact(
        self,
        digest: str,
        body: bytes,
        *,
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, Any] | None = None,
        retention_tier: str = "raw",
    ) -> dict[str, Any]:
        record = self.storage.artifacts.put_bytes(
            body,
            media_type=media_type,
            metadata=dict(metadata or {}),
            retention_tier=retention_tier,
            expected_digest=digest,
        )
        return {
            "artifact_id": record.digest,
            "sha256": record.digest,
            "uri": f"/v1/artifacts/{record.digest}",
            "media_type": record.media_type,
            "size_bytes": record.size_bytes,
            "relative_path": record.relative_path,
        }

    def artifact_path(self, digest: str) -> Path:
        return self.storage.artifacts.path_for(digest)

    # ------------------------------------------------------------------
    # Query/timeline/retention adapters
    # ------------------------------------------------------------------
    def _all_observations(self) -> list[Any]:
        rows = self.storage.connection.execute(
            "SELECT id FROM observations ORDER BY observed_at DESC, id DESC"
        ).fetchall()
        return [record for row in rows if (record := self.storage.get_observation(str(row["id"]))) is not None]

    def _matching_observations(self, request: QueryRequest) -> list[Any]:
        records = self._all_observations()
        query = request.query.strip().casefold()
        filters = dict(request.filters)
        source_ids = filters.get("source_ids") or filters.get("source_id")
        if isinstance(source_ids, str):
            source_ids = [source_ids]
        kinds = filters.get("kinds") or filters.get("kind")
        if isinstance(kinds, str):
            kinds = [kinds]
        start = request.start
        end = request.end
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        filtered = []
        for record in records:
            text = record.text_content.casefold()
            payload = _json_text(record.payload).casefold()
            if query and query not in text and query not in payload:
                continue
            if source_ids and record.source_id not in {str(value) for value in source_ids}:
                continue
            if kinds and record.kind not in {str(value) for value in kinds}:
                continue
            observed = record.observed_at
            if start is not None and observed < start:
                continue
            if end is not None and observed > end:
                continue
            filtered.append(record)
        if request.start is not None or request.end is not None:
            filtered.sort(key=lambda item: item.observed_at, reverse=False)
        return filtered

    @staticmethod
    def _should_use_recent_context(request: QueryRequest) -> bool:
        """Use recent evidence when a conversational hybrid query has no literal hit."""

        if request.mode != "hybrid" or not request.query.strip():
            return False
        # An explicit time window already expresses retrieval intent. Do not
        # require the wording to begin with a fixed question verb (for example,
        # "review the last 20 minutes of activity").
        if request.start is not None or request.end is not None:
            return True
        normalized = request.query.strip().casefold()
        question_prefixes = (
            "what ",
            "where ",
            "when ",
            "which ",
            "who ",
            "why ",
            "how ",
            "summarize ",
            "recap ",
            "review ",
            "show me ",
            "tell me ",
        )
        return normalized.endswith("?") or normalized.startswith(question_prefixes)

    def _recent_context_records(self, request: QueryRequest) -> list[Any]:
        """Return a small, diverse recent evidence set for conversational questions."""

        unfiltered = QueryRequest.from_value({**request.to_dict(), "query": ""})
        records = self._matching_observations(unfiltered)
        inference_records: list[Any] = []
        source_records: list[Any] = []
        seen_captures: set[str] = set()
        seen_sources: set[str] = set()

        for record in records[:500]:
            source_id = str(record.source_id or "")
            if record.kind == "inference_observation":
                capture_key = str(record.capture_id or record.id)
                if capture_key not in seen_captures and len(inference_records) < 4:
                    inference_records.append(record)
                    seen_captures.add(capture_key)
                continue
            payload = record.payload if isinstance(record.payload, Mapping) else {}
            event_kind = str(payload.get("event_kind", "")).casefold()
            if "process" in source_id.casefold() or event_kind == "process_metadata":
                continue
            source_key = source_id or record.kind
            if source_key in seen_sources:
                continue
            source_records.append(record)
            seen_sources.add(source_key)
            if len(source_records) >= 8:
                break

        selected = inference_records + source_records
        selected.sort(key=lambda item: item.observed_at, reverse=True)
        return selected[: max(1, request.limit)]

    def _primary_artifact_digest(self, record: Any) -> str | None:
        """Read the persisted primary artifact digest for REST projection."""

        digest = getattr(record, "primary_artifact_digest", None)
        if digest is None and isinstance(record, Mapping):
            digest = record.get("primary_artifact_digest")
        if digest:
            return str(digest)

        observation_id = getattr(record, "id", None)
        if observation_id is None and isinstance(record, Mapping):
            observation_id = record.get("id") or record.get("observation_id")
        connection = getattr(self.storage, "connection", None)
        if observation_id is None or connection is None:
            return None
        try:
            row = connection.execute(
                "SELECT primary_artifact_digest FROM observations WHERE id = ?",
                (str(observation_id),),
            ).fetchone()
        except (AttributeError, sqlite3.Error):
            return None
        if row is None:
            return None
        try:
            digest = row["primary_artifact_digest"]
        except (IndexError, KeyError, TypeError):
            digest = row[0]
        return str(digest) if digest else None

    def _observation_dict(self, record: Any) -> dict[str, Any]:
        metadata = dict(record.metadata)
        primary_digest = self._primary_artifact_digest(record)
        if primary_digest:
            metadata["artifact_digest"] = primary_digest
        digest = primary_digest or metadata.get("artifact_digest")
        if digest:
            metadata["artifact_url"] = f"/v1/artifacts/{digest}"
        return {
            "id": record.id,
            "observation_id": record.id,
            "capture_id": record.capture_id,
            "source_id": record.source_id,
            "kind": record.kind,
            "observed_at": record.observed_at.isoformat(),
            "occurred_at": record.observed_at.isoformat(),
            "captured_at": metadata.get("captured_at") or record.observed_at.isoformat(),
            "text": record.text_content,
            "content": record.text_content,
            "payload": record.payload,
            "metadata": metadata,
        }

    def _search_hit(self, record: Any) -> SearchHit:
        item = self._observation_dict(record)
        metadata = dict(item["metadata"])
        metadata.update(
            {
                "capture_id": record.capture_id,
                "source_id": record.source_id,
                "kind": record.kind,
                "payload": record.payload,
            }
        )
        return SearchHit(
            id=record.id,
            text=record.text_content,
            score=1.0,
            source=record.source_id or record.kind,
            timestamp=record.observed_at.isoformat(),
            metadata=metadata,
        )

    def search(self, request: QueryRequest | Mapping[str, Any] | str) -> Any:
        from .query.orchestrator import QueryOrchestrator

        canonical = QueryRequest.from_value(request)
        canonical = self._with_relative_time_window(canonical)
        return QueryOrchestrator(fts=self.fts, answerer=self.answerer).query(canonical)

    @staticmethod
    def _with_relative_time_window(request: QueryRequest) -> QueryRequest:
        """Translate conversational windows such as 'past 20 minutes' into filters."""

        if request.start is not None or request.end is not None:
            return request
        match = re.search(
            r"\b(?:past|last)\s+(\d+)\s*(minute|hour|day)s?\b",
            request.query,
            flags=re.IGNORECASE,
        )
        if match is None:
            return request
        amount = max(1, int(match.group(1)))
        unit = match.group(2).casefold()
        delta = {
            "minute": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "day": timedelta(days=amount),
        }[unit]
        end = _utc_now()
        return QueryRequest.from_value(
            {**request.to_dict(), "start": end - delta, "end": end}
        )

    async def aquery(self, request: QueryRequest | Mapping[str, Any] | str) -> Any:
        """Run retrieval/model synthesis off the API event loop."""

        return await asyncio.to_thread(self.search, request)

    def timeline(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        options = dict(options or {})
        request = QueryRequest.from_value(
            {
                "query": options.get("query", ""),
                "limit": int(options.get("limit") or 50),
                "filters": {
                    key: options[key]
                    for key in ("source_id", "kind")
                    if options.get(key) is not None
                },
                "start": options.get("start"),
                "end": options.get("end"),
            }
        )
        items = [self._observation_dict(record) for record in self._matching_observations(request)[: request.limit]]
        return {"items": items, "total_count": len(items), "generated_at": _utc_now().isoformat()}

    def observations(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.timeline(options)

    def observe(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(observation)
        event_id = str(raw.get("source_event_id") or raw.get("event_id") or uuid.uuid4().hex)
        source_id = str(raw.get("source_id") or f"manual:{event_id}")
        source = self.storage.create_source(kind="manual", source_id=source_id, external_id=source_id)
        observed_at = raw.get("observed_at") or raw.get("occurred_at") or _utc_now()
        payload = raw.get("payload", raw.get("value", raw))
        metadata = dict(raw.get("metadata") or {})
        raw_artifacts = raw.get("artifacts") or []
        artifact_payload = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in raw_artifacts
            if isinstance(item, Mapping) or hasattr(item, "model_dump")
        ]
        record = self.storage.create_observation(
            source_id=source.id,
            kind=str(raw.get("kind") or raw.get("observation_kind") or "manual"),
            observed_at=observed_at,
            text_content=str(raw.get("text") or raw.get("text_content") or _event_text(payload)),
            payload=payload,
            metadata=metadata,
            observation_id=str(raw.get("observation_id") or f"observation:{event_id}"),
            idempotency_key=event_id,
            retention_tier=RetentionTier.DERIVED,
        )
        prompt = build_observation_prompt(
            event_id=event_id,
            source_id=source.id,
            event_type="observed",
            occurred_at=observed_at,
            payload=payload,
            metadata=metadata,
            artifacts=artifact_payload,
        )
        self.storage.enqueue_job(
            "inference",
            {
                "observation_id": record.id,
                "source_event_id": event_id,
                "source_id": source.id,
                "observed_at": _iso(observed_at),
                "artifacts": artifact_payload,
                "model": "default" if self._config.get("model_endpoint") else None,
                "prompt": prompt.to_payload(),
                "metadata": metadata,
            },
            idempotency_key=f"inference:{record.id}",
        )
        self._last_ingest_at = _utc_now()
        return self._observation_dict(record)

    def purge(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(options or {})
        dry_run = bool(payload.get("dry_run", False))
        before = payload.get("before")
        if before is None:
            before = payload.get("date")
        source = payload.get("source")
        if source is None:
            source = payload.get("source_ids", payload.get("source_id"))
        project = payload.get("project")
        if project is None:
            project = payload.get("project_ids", payload.get("project_id"))
        retention_tiers = payload.get("retention_tiers")
        if retention_tiers is None:
            retention_tiers = payload.get("retention_tier", payload.get("tiers"))

        result = self.storage.purge_expired(
            as_of=_utc_now(),
            before=before,
            source=source,
            project=project,
            retention_tiers=retention_tiers,
            dry_run=dry_run,
        )
        response = {
            "dry_run": result.dry_run,
            "eligible": result.eligible_observations,
            "deleted": result.deleted_observations,
            "eligible_observations": result.eligible_observations,
            "eligible_artifacts": result.eligible_artifacts,
            "expired_observations": result.expired_observations,
            "deleted_observations": result.deleted_observations,
            "expired_artifacts": result.expired_artifacts,
            "deleted_artifacts": result.deleted_artifacts,
            "skipped_referenced_artifacts": result.skipped_referenced_artifacts,
            "missing_artifact_files": result.missing_artifact_files,
            "file_errors": list(result.file_errors),
        }
        if before is not None:
            parsed_before = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
            response["before"] = parsed_before.isoformat()
        if payload.get("date") is not None:
            response["date"] = payload["date"]
        if source is not None:
            response["source"] = source
        if project is not None:
            response["project"] = project
        if retention_tiers is not None:
            response["retention_tiers"] = retention_tiers
        return response

    def status_snapshot(self) -> dict[str, Any]:
        return self.status()


__all__ = ["ContextRuntime", "RuntimeAnswerer", "SQLiteFtsBackend"]
