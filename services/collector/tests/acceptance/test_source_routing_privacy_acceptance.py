"""Focused source-routing and privacy acceptance coverage for the collector.

Every source is represented by an injected fake.  The test therefore checks
the collector boundary where source settings become local-only records or a
remote transport batch without touching Windows APIs, a browser, a clipboard,
the live filesystem, or a GPU.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from contracts import SourceEvent
from services.collector.capture.models import (
    CaptureSequence,
    EncodedImage,
    Monitor,
    MonitorCapture,
)
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.sources.browser_clipboard import BrowserMetadata, ClipboardEvent
from services.collector.sources.filesystem import FileMetadata
from services.collector.sources.window_metadata import WindowMetadata
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.transport import CollectorTransport


CAPTURED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ROOT_NAME = "routing-root"

SOURCE_IDS = {
    "screenshots": "screen:display-routing",
    "foreground": "window:foreground",
    "filesystem": f"filesystem:{ROOT_NAME}",
    "git": "git_terminal:git:routing-fixture",
    "powershell": "git_terminal:powershell:routing-fixture",
    "process": "git_terminal:process:routing-fixture",
    "browser": "browser:foreground",
    "clipboard": "clipboard:local",
}

ALL_SOURCE_KINDS = tuple(SOURCE_IDS)

_RAW_KEY_FIELDS = frozenset(
    {
        "key",
        "keys",
        "key_event",
        "key_events",
        "keyboard",
        "keyboard_event",
        "keyboard_events",
        "key_down",
        "key_up",
        "raw_input",
        "raw_keyboard",
        "raw_keys",
        "scan_code",
        "virtual_key",
    }
)


def _assert_no_capture_fields(value: object) -> None:
    """Allow explicit false capability guards, but reject captured media/input."""

    violations: list[str] = []

    def walk(item: object, path: str = "$") -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
                child_path = f"{path}.{key}"
                if normalized in _RAW_KEY_FIELDS:
                    violations.append(child_path)
                elif normalized == "keylogging" and nested is not False:
                    violations.append(f"{child_path}=not-false")
                elif normalized in {"audio", "video"} and nested not in {False, None}:
                    violations.append(f"{child_path}=captured")
                walk(nested, child_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                walk(nested, f"{path}[{index}]")

    walk(value)
    assert not violations, f"capture field(s) found at: {violations}"


class _FakeCapture:
    def __init__(self) -> None:
        monitor = Monitor("display-routing", "Routing display", 0, 0, 2, 2, True)
        image_bytes = b"injected-routing-jpeg"
        image = EncodedImage(
            jpeg_bytes=image_bytes,
            width=2,
            height=2,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
        )
        self.sequence = CaptureSequence(
            sequence_id="routing-sequence",
            captured_at=CAPTURED_AT,
            monitors=(
                MonitorCapture(
                    sequence_id="routing-sequence",
                    monitor=monitor,
                    captured_at=CAPTURED_AT,
                    image=image,
                ),
            ),
            contact_sheet=None,
            metadata={"backend": "injected-routing-capture", "audio": False, "video": False},
        )
        self.capture_calls = 0
        self.close_calls = 0

    def capture_once(self) -> CaptureSequence:
        self.capture_calls += 1
        return self.sequence

    def close(self) -> None:
        self.close_calls += 1


class _FakeWindowSource:
    def __init__(self) -> None:
        self.collect_calls = 0

    def collect(self) -> WindowMetadata:
        self.collect_calls += 1
        return WindowMetadata(
            timestamp=CAPTURED_AT.timestamp(),
            foreground_process="routing-editor.exe",
            executable=r"C:\Routing\routing-editor.exe",
            window_title="Routing fixture",
            monitor=r"\\.\DISPLAY1",
            process_id=4242,
            process_created_at=CAPTURED_AT.timestamp() - 60,
            available=True,
        )


class _FakeBrowserSource:
    def __init__(self) -> None:
        self.read_calls = 0

    def read(self) -> BrowserMetadata:
        self.read_calls += 1
        return BrowserMetadata(
            title="Routing browser fixture",
            url="https://example.invalid/routing",
            browser="fixture-browser",
            captured_at=CAPTURED_AT,
            available=True,
        )


class _FakeClipboardSource:
    def __init__(self, text: str) -> None:
        self.text = text
        self.poll_calls = 0

    def poll_once(self) -> ClipboardEvent:
        self.poll_calls += 1
        return ClipboardEvent(
            text=self.text,
            sequence_number=7,
            captured_at=CAPTURED_AT,
        )


class _FakeHTTPClient:
    def __init__(self) -> None:
        self.artifact_calls: list[Any] = []
        self.batch_calls: list[dict[str, Any]] = []

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        self.artifact_calls.append(artifact)
        return ClientResponse(201, {"sha256": artifact.sha256}, {}, 1)

    def ingest_batch(
        self,
        payload: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        del batch_id
        self.batch_calls.append(payload)
        return ClientResponse(202, {"accepted": True}, {}, 1)


def _source_event(source_id: str, event_kind: str) -> SourceEvent:
    return SourceEvent(
        event_id=f"event-{event_kind}",
        source_id=source_id,
        source_kind="application",
        event_type="observed",
        occurred_at=CAPTURED_AT,
        captured_at=CAPTURED_AT,
        payload={
            "event_kind": event_kind,
            "fixture": "injected",
            "audio": False,
            "video": False,
            "keylogging": False,
        },
        metadata={"collector": "injected-routing-fake", "keylogging": False},
    )


def _routing(*, enabled: str | None = None, route: str = "remote") -> dict[str, dict[str, object]]:
    result = {
        source_kind: {"enabled": True, "route": route}
        for source_kind in ALL_SOURCE_KINDS
    }
    if enabled is not None:
        result[enabled] = {"enabled": False, "route": route}
    return result


def _install_source_fakes(
    monkeypatch: pytest.MonkeyPatch,
    runtime_module: Any,
    root: Path,
) -> dict[str, Any]:
    window = _FakeWindowSource()
    browser = _FakeBrowserSource()
    clipboard = _FakeClipboardSource("  clipboard routing marker\r\nline  ")
    calls = {"discover": 0, "file_metadata": 0, "git_terminal": 0}

    def discover_git_repositories(*_args: Any, **_kwargs: Any) -> list[Path]:
        calls["discover"] += 1
        return [root / "fixture-repository"]

    def file_metadata(*_args: Any, **_kwargs: Any) -> FileMetadata:
        calls["file_metadata"] += 1
        return FileMetadata(
            path=root,
            root=root,
            is_directory=True,
            size=0,
            mtime_ns=1,
            sha256=None,
        )

    def collect_git_terminal_events(*_args: Any, **_kwargs: Any) -> list[SourceEvent]:
        calls["git_terminal"] += 1
        return [
            _source_event(SOURCE_IDS["git"], "git_status"),
            _source_event(SOURCE_IDS["powershell"], "powershell_history"),
            _source_event(SOURCE_IDS["process"], "process_metadata"),
        ]

    monkeypatch.setattr(runtime_module, "discover_git_repositories", discover_git_repositories)
    monkeypatch.setattr(runtime_module, "file_metadata", file_metadata)
    monkeypatch.setattr(runtime_module, "collect_git_terminal_events", collect_git_terminal_events)
    return {"window": window, "browser": browser, "clipboard": clipboard, "calls": calls}


def _make_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_routing: Mapping[str, Mapping[str, object]],
    remote_clipboard: bool = False,
) -> tuple[CollectorRuntime, _FakeHTTPClient, _FakeCapture, dict[str, Any]]:
    import services.collector.runtime as runtime_module

    root = tmp_path / ROOT_NAME
    root.mkdir(parents=True)
    fakes = _install_source_fakes(monkeypatch, runtime_module, root)
    client = _FakeHTTPClient()
    capture = _FakeCapture()
    spool_dir = tmp_path / "collector-spool"
    transport = CollectorTransport(
        TransportConfig(
            server_url="https://remote.example.invalid",
            spool_dir=spool_dir,
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            fsync=False,
        ),
        client=client,
    )
    runtime = CollectorRuntime(
        CollectorConfig(
            roots=(root,),
            clipboard_enabled=True,
            browser_enabled=True,
            include_git_history=True,
            include_processes=True,
            spool_dir=spool_dir,
            remote_clipboard=remote_clipboard,
            source_routing=source_routing,
        ),
        transport=transport,
        capture=capture,
    )
    runtime.window_source = fakes["window"]
    runtime.browser_source = fakes["browser"]
    runtime.clipboard_source = fakes["clipboard"]
    return runtime, client, capture, fakes


def _all_batches(runtime: CollectorRuntime) -> list[Mapping[str, Any]]:
    return [batch.payload for batch in runtime.local_only_spool.list_pending_batches()]


def _event_ids(batches: list[Mapping[str, Any]]) -> set[str]:
    return {
        str(event["source_id"])
        for batch in batches
        for event in batch.get("events", [])
    }


def _is_routing_filesystem_id(value: object) -> bool:
    # The runtime intentionally namespaces the configured root with its full
    # path. The stable acceptance contract is the source prefix, root basename,
    # and the shared identifier length limit, not pytest's temp-directory path.
    if not isinstance(value, str) or not value.startswith("filesystem:"):
        return False
    path_text = value.removeprefix("filesystem:")
    return Path(path_text).name == ROOT_NAME and len(value) <= 256


def test_source_routes_split_remote_and_local_events_without_artifact_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_routing = {
        "screenshots": {"enabled": True, "route": "local"},
        "foreground": {"enabled": True, "route": "remote"},
        "filesystem": {"enabled": True, "route": "local-only"},
        "git": {"enabled": True, "route": "remote"},
        "powershell": {"enabled": True, "route": "local"},
        "process": {"enabled": True, "route": "remote"},
        "browser": {"enabled": True, "route": "local"},
        "clipboard": {"enabled": True, "route": "local"},
    }
    runtime, client, capture, fakes = _make_runtime(
        tmp_path,
        monkeypatch,
        source_routing=source_routing,
    )

    try:
        report = runtime.tick()
        local_batches = _all_batches(runtime)
    finally:
        runtime.close()

    assert capture.capture_calls == 1
    assert fakes["window"].collect_calls == 1
    assert fakes["browser"].read_calls == 1
    assert fakes["clipboard"].poll_calls == 1
    assert fakes["calls"] == {"discover": 1, "file_metadata": 1, "git_terminal": 1}

    assert report["local_only_events"] == 5
    assert len(client.batch_calls) == 1
    remote_events = client.batch_calls[0]["events"]
    assert {event["source_id"] for event in remote_events} == {
        SOURCE_IDS["foreground"],
        SOURCE_IDS["git"],
        SOURCE_IDS["process"],
    }
    local_ids = _event_ids(local_batches)
    filesystem_ids = {value for value in local_ids if _is_routing_filesystem_id(value)}
    assert len(filesystem_ids) == 1
    assert local_ids - filesystem_ids == {
        SOURCE_IDS["screenshots"],
        SOURCE_IDS["powershell"],
        SOURCE_IDS["browser"],
        SOURCE_IDS["clipboard"],
    }
    assert "clipboard routing marker" not in repr(client.batch_calls[0])
    assert client.artifact_calls == []
    assert len(runtime.local_only_spool.list_pending_artifacts()) == 1
    assert client.batch_calls[0]["artifacts"] == []

    _assert_no_capture_fields(client.batch_calls)
    _assert_no_capture_fields(local_batches)


@pytest.mark.parametrize("disabled_source", ALL_SOURCE_KINDS)
def test_each_source_enabled_setting_prevents_collection(
    disabled_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, client, capture, fakes = _make_runtime(
        tmp_path,
        monkeypatch,
        source_routing=_routing(enabled=disabled_source),
    )

    try:
        runtime.tick()
        local_batches = _all_batches(runtime)
    finally:
        runtime.close()

    emitted_ids = {
        event["source_id"]
        for payload in client.batch_calls
        for event in payload["events"]
    } | _event_ids(local_batches)
    if disabled_source == "filesystem":
        assert not any(_is_routing_filesystem_id(value) for value in emitted_ids)
    else:
        assert SOURCE_IDS[disabled_source] not in emitted_ids

    expected_calls = {
        "discover": int(disabled_source not in {"git"}),
        "file_metadata": int(disabled_source not in {"filesystem"}),
        "git_terminal": 1,
    }
    assert fakes["calls"] == expected_calls
    assert capture.capture_calls == int(disabled_source != "screenshots")
    assert fakes["window"].collect_calls == int(disabled_source != "foreground")
    assert fakes["browser"].read_calls == int(disabled_source != "browser")
    assert fakes["clipboard"].poll_calls == int(disabled_source != "clipboard")
    _assert_no_capture_fields(client.batch_calls)
    _assert_no_capture_fields(local_batches)


def test_clipboard_is_local_only_by_default_and_remote_only_after_explicit_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = CollectorConfig(roots=(), spool_dir=tmp_path / "defaults")
    assert defaults.source_routing["clipboard"]["enabled"] is True
    assert defaults.source_routing["clipboard"]["route"] == "local"
    assert defaults.remote_clipboard is False

    default_runtime, default_client, _capture, _fakes = _make_runtime(
        tmp_path / "default",
        monkeypatch,
        source_routing=_routing(route="remote"),
    )
    try:
        default_runtime.tick()
        default_local = _all_batches(default_runtime)
    finally:
        default_runtime.close()

    assert default_runtime.config.remote_clipboard is False
    default_remote_ids = {
        event["source_id"]
        for event in default_client.batch_calls[0]["events"]
    }
    assert SOURCE_IDS["clipboard"] not in default_remote_ids
    assert SOURCE_IDS["clipboard"] in _event_ids(default_local)

    opt_in_runtime, opt_in_client, _capture, _fakes = _make_runtime(
        tmp_path / "opt-in",
        monkeypatch,
        source_routing=_routing(route="remote"),
        remote_clipboard=True,
    )
    try:
        opt_in_runtime.tick()
    finally:
        opt_in_runtime.close()

    opt_in_remote_ids = {
        event["source_id"]
        for event in opt_in_client.batch_calls[0]["events"]
    }
    assert SOURCE_IDS["clipboard"] in opt_in_remote_ids
    assert _all_batches(opt_in_runtime) == []
    assert "clipboard routing marker" in repr(opt_in_client.batch_calls[0])
    _assert_no_capture_fields(opt_in_client.batch_calls)
