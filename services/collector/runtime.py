"""Five-second Windows collector runtime with durable offline delivery."""

from __future__ import annotations

import argparse
import json
import os
import uuid
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    from contracts import ArtifactRef, CaptureEnvelope, SourceEvent
except ModuleNotFoundError:  # Source checkout without an editable install.
    from packages.contracts.python.contracts import ArtifactRef, CaptureEnvelope, SourceEvent

from .capture import CaptureConfig, ScreenCaptureService
from .sources.browser_clipboard import BrowserSource, ClipboardSource
from .sources.filesystem import discover_git_repositories, file_metadata
from .sources.git_terminal import collect_git_terminal_events
from .sources.window_metadata import get_window_metadata_source
from .transport.config import TransportConfig
from .transport.spool import LocalSpool
from .transport.transport import CollectorTransport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


_SOURCE_ROUTING_DEFAULTS: dict[str, dict[str, object]] = {
    # Clipboard text remains local unless an operator explicitly routes it to a
    # remote endpoint. Screenshots retain the collector's existing remote
    # default and can be made local with an explicit route setting.
    "screenshots": {"enabled": True, "route": "remote"},
    "foreground": {"enabled": True, "route": "remote"},
    "filesystem": {"enabled": True, "route": "remote"},
    "git": {"enabled": True, "route": "remote"},
    "powershell": {"enabled": True, "route": "remote"},
    "process": {"enabled": True, "route": "remote"},
    "browser": {"enabled": True, "route": "remote"},
    "clipboard": {"enabled": True, "route": "local"},
}
_SOURCE_ROUTING_ALIASES = {
    "screen": "screenshots",
    "screenshot": "screenshots",
    "foreground_window": "foreground",
    "file": "filesystem",
    "powershell_process": "powershell",
}
_REMOTE_ROUTE_NAMES = frozenset({"remote", "remote_only", "remote-only"})


def _coerce_bool_value(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def _normalise_source_routing(
    value: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    result = {key: dict(setting) for key, setting in _SOURCE_ROUTING_DEFAULTS.items()}
    if value is None:
        return result
    if not isinstance(value, Mapping):
        raise ValueError("source_routing must be a mapping")

    for raw_key, raw_setting in value.items():
        key = str(raw_key).strip().lower().replace("-", "_").replace(" ", "_")
        key = _SOURCE_ROUTING_ALIASES.get(key, key)
        if key not in result:
            continue
        if isinstance(raw_setting, Mapping):
            setting = dict(result[key])
            if "enabled" in raw_setting:
                setting["enabled"] = _coerce_bool_value(
                    raw_setting["enabled"],
                    field_name=f"source_routing.{key}.enabled",
                )
            if "route" in raw_setting:
                route = str(raw_setting["route"]).strip().lower()
                if not route:
                    raise ValueError(f"source_routing.{key}.route must not be empty")
                setting["route"] = route
            result[key] = setting
        else:
            result[key]["enabled"] = _coerce_bool_value(
                raw_setting,
                field_name=f"source_routing.{key}.enabled",
            )
    return result


def _source_routing_from_environment() -> Mapping[str, object] | None:
    raw = os.environ.get("COLLECTOR_SOURCE_ROUTING") or os.environ.get("COLLECTOR_ROUTES")
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("COLLECTOR_SOURCE_ROUTING must contain JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("COLLECTOR_SOURCE_ROUTING must contain a JSON object")
    return value


def _env_roots() -> tuple[Path, ...]:
    raw = os.environ.get("COLLECTOR_ROOTS", "")
    if not raw.strip():
        return (Path.cwd(),)
    return tuple(Path(item.strip()).expanduser() for item in raw.split(";" if ";" in raw else ",") if item.strip())


def _event(
    *,
    source_id: str,
    source_kind: str,
    payload: Mapping[str, object],
    event_type: str = "observed",
    metadata: Mapping[str, object] | None = None,
    occurred_at: datetime | None = None,
    artifacts: Iterable[ArtifactRef] = (),
) -> SourceEvent:
    timestamp = occurred_at or _now()
    return SourceEvent(
        event_id=uuid.uuid4().hex,
        source_id=source_id,
        source_kind=source_kind,
        event_type=event_type,
        occurred_at=timestamp,
        captured_at=timestamp,
        payload=dict(payload),
        artifacts=list(artifacts),
        metadata=dict(metadata or {}),
    )


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    interval_seconds: float = 5.0
    jpeg_quality: int = 85
    roots: tuple[Path, ...] = (Path.cwd(),)
    clipboard_enabled: bool = True
    browser_enabled: bool = True
    include_git_history: bool = True
    include_processes: bool = True
    max_processes: int = 64
    spool_dir: Path = Path(r"E:\LifeContext\spool")
    remote_clipboard: bool = False
    source_routing: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: _normalise_source_routing(None)
    )

    @classmethod
    def from_environment(cls) -> "CollectorConfig":
        # Slotted dataclasses expose a member descriptor at the class level;
        # use the documented concrete default rather than ``str(cls.spool_dir)``.
        default_spool_dir = Path(r"E:\LifeContext\spool")
        spool = Path(os.environ.get("COLLECTOR_SPOOL_DIR", str(default_spool_dir))).expanduser()
        return cls(
            interval_seconds=_env_float("COLLECTOR_CAPTURE_INTERVAL_SECONDS", 5.0),
            jpeg_quality=int(os.environ.get("COLLECTOR_JPEG_QUALITY", "85")),
            roots=_env_roots(),
            clipboard_enabled=_env_bool("COLLECTOR_CLIPBOARD_ENABLED", True),
            browser_enabled=_env_bool("COLLECTOR_BROWSER_ENABLED", True),
            include_git_history=_env_bool("COLLECTOR_INCLUDE_POWERSHELL_HISTORY", True),
            include_processes=_env_bool("COLLECTOR_INCLUDE_PROCESSES", True),
            max_processes=int(os.environ.get("COLLECTOR_MAX_PROCESSES", "64")),
            spool_dir=spool,
            remote_clipboard=_env_bool("COLLECTOR_ALLOW_REMOTE_CLIPBOARD", False),
            source_routing=_source_routing_from_environment(),
        )

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("collector interval must be positive")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG quality must be between 1 and 100")
        if self.max_processes < 0:
            raise ValueError("max_processes cannot be negative")
        object.__setattr__(self, "source_routing", _normalise_source_routing(self.source_routing))


class CollectorRuntime:
    """Collect one complete context tick and durably deliver it."""

    def __init__(
        self,
        config: CollectorConfig | None = None,
        *,
        transport: CollectorTransport | None = None,
        capture: ScreenCaptureService | None = None,
    ) -> None:
        self.config = config or CollectorConfig.from_environment()
        transport_config = TransportConfig.from_env(spool_dir=self.config.spool_dir)
        self.transport = transport or CollectorTransport(transport_config)
        self.local_only_spool = LocalSpool(self.config.spool_dir / "local-only")
        self.capture = capture or ScreenCaptureService(
            config=CaptureConfig(
                interval_seconds=self.config.interval_seconds,
                jpeg_quality=self.config.jpeg_quality,
            )
        )
        self.window_source = get_window_metadata_source()
        self.browser_source = BrowserSource(enabled=self._source_enabled("browser", self.config.browser_enabled))
        self.clipboard_source = ClipboardSource(
            enabled=self._source_enabled("clipboard", self.config.clipboard_enabled)
        )
        self.stop_requested = False
        self.ticks = 0
        self.last_error: str | None = None
        self.last_report: dict[str, object] = {}
        self.local_only_events = 0

    @property
    def server_is_remote(self) -> bool:
        host = (urlparse(self.transport.config.server_url).hostname or "").lower()
        return host not in {"127.0.0.1", "localhost", "::1"}

    def _source_setting(self, source_kind: str) -> Mapping[str, object]:
        return self.config.source_routing.get(
            source_kind,
            _SOURCE_ROUTING_DEFAULTS.get(
                source_kind,
                {"enabled": True, "route": "remote"},
            ),
        )

    def _source_enabled(self, source_kind: str, legacy_enabled: bool = True) -> bool:
        return bool(self._source_setting(source_kind).get("enabled", True)) and legacy_enabled

    def _source_route(self, source_kind: str) -> str:
        route = str(self._source_setting(source_kind).get("route", "remote")).strip().lower()
        # Clipboard remote delivery has two independent gates: its route must
        # be explicitly remote and the legacy opt-in flag must be true.
        if source_kind == "clipboard" and not self.config.remote_clipboard:
            return "local"
        return route or "local"

    @staticmethod
    def _is_remote_route(route: str) -> bool:
        return route.strip().lower() in _REMOTE_ROUTE_NAMES

    def _event_source_kind(self, event: SourceEvent) -> str:
        source_id = event.source_id.casefold()
        if source_id.startswith("screen:"):
            return "screenshots"
        if source_id == "window:foreground" or source_id.startswith("window:"):
            return "foreground"
        if source_id.startswith("filesystem:"):
            return "filesystem"
        if source_id.startswith("browser:"):
            return "browser"
        if source_id.startswith("clipboard:"):
            return "clipboard"
        if source_id.startswith("git_terminal:"):
            payload = event.payload
            event_kind = payload.get("event_kind") if isinstance(payload, Mapping) else None
            if event_kind == "powershell_history":
                return "powershell"
            if event_kind in {"process_metadata", "terminal_metadata"}:
                return "process"
            return "git"
        return "remote"

    def _artifact_ref(
        self,
        encoded: object,
        *,
        name: str,
        monitor_id: str | None = None,
        retention_tier: str = "raw",
        local_only: bool = False,
    ) -> ArtifactRef:
        artifact = getattr(encoded, "jpeg_bytes", getattr(encoded, "data", b""))
        metadata = {
            "content_type": "image/jpeg",
            "retention_tier": retention_tier,
            "name": name,
            "monitor_id": monitor_id,
        }
        stored = (
            self.local_only_spool.put_artifact(artifact, metadata=metadata)
            if local_only
            else self.transport.enqueue_artifact(artifact, metadata=metadata)
        )
        return ArtifactRef(
            artifact_id=stored.sha256,
            kind="image",
            uri=f"/v1/artifacts/{stored.sha256}",
            name=name,
            media_type="image/jpeg",
            size_bytes=stored.size,
            sha256=stored.sha256,
            metadata={
                key: value
                for key, value in {
                    "monitor_id": monitor_id,
                    "retention_tier": retention_tier,
                }.items()
                if value is not None
            },
        )

    def _source_events(self) -> list[SourceEvent]:
        events: list[SourceEvent] = []
        now = _now()
        if self._source_enabled("foreground"):
            try:
                window = self.window_source.collect()
                events.append(
                    _event(
                        source_id="window:foreground",
                        source_kind="application",
                        payload=window.as_dict(),
                        metadata={"adapter": "foreground-window", "keylogging": False},
                        occurred_at=datetime.fromtimestamp(window.timestamp, timezone.utc),
                    )
                )
            except Exception as error:
                self.last_error = f"window metadata: {error}"

        if self._source_enabled("browser", self.config.browser_enabled):
            try:
                browser = self.browser_source.read()
                events.append(
                    _event(
                        source_id="browser:foreground",
                        source_kind="web",
                        payload=browser.as_dict(),
                        metadata={"adapter": "browser-metadata", "page_body_collected": False},
                        occurred_at=browser.captured_at,
                    )
                )
            except Exception as error:
                self.last_error = f"browser metadata: {error}"

        if self._source_enabled("clipboard", self.config.clipboard_enabled):
            try:
                clipboard = self.clipboard_source.poll_once()
                if clipboard is not None:
                    events.append(
                        _event(
                            source_id="clipboard:local",
                            source_kind="manual",
                            payload={
                                "raw_text": clipboard.text,
                                "sequence_number": clipboard.sequence_number,
                                "routing": dict(clipboard.routing or {}),
                            },
                            metadata={
                                "adapter": "clipboard",
                                "plaintext": True,
                                "remote_route": "explicit_opt_in",
                            },
                            occurred_at=clipboard.captured_at,
                        )
                    )
            except Exception as error:
                self.last_error = f"clipboard: {error}"

        if self._source_enabled("filesystem"):
            try:
                for root in self.config.roots:
                    details = file_metadata(root, root=root, compute_hash=False)
                    events.append(
                        _event(
                            source_id=f"filesystem:{root}",
                            source_kind="file",
                            payload={
                                "root": str(root),
                                "metadata": details.as_dict() if details else None,
                            },
                            metadata={"adapter": "filesystem-root", "content_capture": False},
                            occurred_at=now,
                        )
                    )
            except Exception as error:
                self.last_error = f"filesystem metadata: {error}"

        git_enabled = self._source_enabled("git")
        powershell_enabled = self._source_enabled(
            "powershell", self.config.include_git_history
        )
        process_enabled = self._source_enabled("process", self.config.include_processes)
        if git_enabled or powershell_enabled or process_enabled:
            try:
                repositories = (
                    discover_git_repositories(
                        self.config.roots,
                        max_depth=4,
                        max_repositories=64,
                    )
                    if git_enabled
                    else ()
                )
                raw_events = collect_git_terminal_events(
                    repositories=repositories,
                    include_history=powershell_enabled,
                    include_processes=process_enabled,
                    max_processes=self.config.max_processes,
                )
                for raw in raw_events:
                    event = SourceEvent.model_validate(raw)
                    source_kind = self._event_source_kind(event)
                    if source_kind == "git" and not git_enabled:
                        continue
                    if source_kind == "powershell" and not powershell_enabled:
                        continue
                    if source_kind == "process" and not process_enabled:
                        continue
                    events.append(event)
            except Exception as error:
                self.last_error = f"git/terminal metadata: {error}"

        return events

    def _remote_safe_events(self, events: list[SourceEvent], envelope_id: str) -> list[SourceEvent]:
        if not self.server_is_remote:
            return events

        allowed: list[SourceEvent] = []
        for event in events:
            source_kind = self._event_source_kind(event)
            if self._is_remote_route(self._source_route(source_kind)):
                allowed.append(event)
                continue
            self.local_only_spool.enqueue_batch(
                {"envelope_id": envelope_id, "events": [event.model_dump(mode="json")]},
                batch_id=f"local-only:{event.event_id}",
            )
            self.local_only_events += 1
        return allowed

    def tick(self) -> dict[str, object]:
        sequence = self.capture.capture_once() if self._source_enabled("screenshots") else None
        captured_at = sequence.captured_at if sequence is not None else _now()
        envelope_id = sequence.sequence_id if sequence is not None else uuid.uuid4().hex
        artifacts: list[ArtifactRef] = []
        monitor_events: list[SourceEvent] = []
        if sequence is not None:
            local_only_screenshots = self.server_is_remote and not self._is_remote_route(
                self._source_route("screenshots")
            )
            for monitor_capture in sequence.monitors:
                ref = self._artifact_ref(
                    monitor_capture.image,
                    name=f"{monitor_capture.monitor.monitor_id}.jpg",
                    monitor_id=monitor_capture.monitor.monitor_id,
                    local_only=local_only_screenshots,
                )
                artifacts.append(ref)
                monitor_events.append(
                    _event(
                        source_id=f"screen:{monitor_capture.monitor.monitor_id}",
                        source_kind="application",
                        payload={
                            "sequence_id": sequence.sequence_id,
                            "monitor": monitor_capture.monitor.as_metadata(),
                            "sha256": monitor_capture.sha256,
                            "audio": False,
                            "video": False,
                        },
                        metadata={
                            "adapter": sequence.metadata.get("backend"),
                            "capture_kind": "monitor",
                        },
                        occurred_at=sequence.captured_at,
                        artifacts=[ref],
                    )
                )
            if sequence.contact_sheet is not None:
                contact_ref = self._artifact_ref(
                    sequence.contact_sheet,
                    name="contact-sheet.jpg",
                    retention_tier="keyframe",
                    local_only=local_only_screenshots,
                )
                artifacts.append(contact_ref)
                monitor_events.append(
                    _event(
                        source_id=f"screen:{sequence.sequence_id}:contact-sheet",
                        source_kind="application",
                        payload={"sequence_id": sequence.sequence_id, "contact_sheet": True},
                        metadata={
                            "capture_kind": "contact_sheet",
                            "audio": False,
                            "video": False,
                        },
                        occurred_at=sequence.captured_at,
                        artifacts=[contact_ref],
                    )
                )

        events = monitor_events + self._source_events()
        events = self._remote_safe_events(events, envelope_id)
        if self.server_is_remote:
            allowed_artifacts = {
                ref.artifact_id
                for event in events
                for ref in event.artifacts
            }
            artifacts = [ref for ref in artifacts if ref.artifact_id in allowed_artifacts]
        if not events:
            self.last_report = {
                "sequence_id": envelope_id,
                "monitor_count": len(sequence.monitors) if sequence is not None else 0,
                "accepted": 0,
                "local_only_events": self.local_only_events,
            }
            return self.last_report
        envelope = CaptureEnvelope(
            envelope_id=envelope_id,
            captured_at=captured_at,
            source_kind="application",
            events=events,
            artifacts=artifacts,
            producer="windows-collector",
            metadata={
                "monitor_count": len(sequence.monitors) if sequence is not None else 0,
                "audio": False,
                "video": False,
                "keylogging": False,
                "clipboard_local_only": not (
                    self.config.remote_clipboard
                    and self._is_remote_route(self._source_route("clipboard"))
                ),
            },
        )
        self.transport.enqueue_batch(envelope.model_dump(mode="json"), batch_id=envelope.envelope_id)
        report = self.transport.replay()
        self.ticks += 1
        self.last_report = {
            "sequence_id": envelope_id,
            "monitor_count": len(sequence.monitors) if sequence is not None else 0,
            "event_count": len(events),
            "accepted": len(events),
            "transport": report.as_dict(),
            "spool": self.transport.status(),
            "local_only_events": self.local_only_events,
        }
        return self.last_report

    def run(self, *, max_ticks: int | None = None) -> int:
        emitted = 0
        try:
            while not self.stop_requested and (max_ticks is None or emitted < max_ticks):
                report = self.tick()
                emitted += 1
                print(report, flush=True)
                if self.stop_requested or (max_ticks is not None and emitted >= max_ticks):
                    break
                time.sleep(self.config.interval_seconds)
        except KeyboardInterrupt:
            self.stop_requested = True
        finally:
            self.close()
        return emitted

    def close(self) -> None:
        self.capture.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="life-context-collector")
    parser.add_argument("--once", action="store_true", help="capture one tick and exit")
    parser.add_argument("--max-ticks", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = CollectorRuntime()
    if args.once:
        try:
            print(runtime.tick(), flush=True)
        finally:
            runtime.close()
        return 0
    return runtime.run(max_ticks=args.max_ticks)


__all__ = ["CollectorConfig", "CollectorRuntime", "build_parser", "main"]
