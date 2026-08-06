"""Source-level privacy boundary tests for the collector.

These tests intentionally stay below the collector privacy test directory so
the boundary remains easy to run without changing production behavior or
coupling the privacy contract to an unrelated adapter test.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import CaptureEnvelope
from services.collector.capture import CaptureConfig, Monitor, RawFrame, ScreenCaptureService
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.sources.browser_clipboard import (
    BrowserMetadata,
    BrowserSource,
    ClipboardEvent,
    ClipboardSource,
    NoAudioVideoSource,
    SafeBrowserMetadataFallback,
)
from services.collector.sources.filesystem import normalize_file_event
from services.collector.sources.git_terminal import collect_git_terminal_events
from services.collector.sources.window_metadata import WindowsWindowMetadataSource
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.transport import CollectorTransport


CAPTURED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

_RAW_KEY_EVENT_FIELDS = frozenset(
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
_RAW_KEY_EVENT_MARKERS = (
    "keystroke",
    "key_down",
    "key_up",
    "keyboard_event",
    "raw_keyboard",
    "raw_input",
)


def _assert_no_raw_keystroke_events(value: Any) -> None:
    """Reject raw key-event fields while allowing explicit false guard flags."""

    violations: list[str] = []

    def walk(item: Any, path: str = "$") -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
                child_path = f"{path}.{key}"
                if normalized in _RAW_KEY_EVENT_FIELDS:
                    violations.append(child_path)
                elif normalized == "keylogging" and nested is not False:
                    violations.append(f"{child_path}=not-false")
                walk(nested, child_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                walk(nested, f"{path}[{index}]")
        elif isinstance(item, str):
            normalized = item.casefold().replace("-", "_")
            if any(marker in normalized for marker in _RAW_KEY_EVENT_MARKERS):
                violations.append(path)

    walk(value)
    assert not violations, f"raw keystroke capture found at: {violations}"


class _PrivacyCaptureBackend:
    name = "privacy-test-backend"

    def enumerate_monitors(self) -> tuple[Monitor, ...]:
        return (Monitor("display-privacy", "Privacy display", 0, 0, 2, 2, True),)

    def capture(self, monitor: Monitor) -> RawFrame:
        return RawFrame(
            width=monitor.width,
            height=monitor.height,
            pixel_bytes=bytes((20, 40, 60)) * (monitor.width * monitor.height),
            metadata={"fixture": "privacy-boundary"},
        )

    def close(self) -> None:
        return None


class _PrivacyHTTPClient:
    def __init__(self) -> None:
        self.batch_calls: list[dict[str, Any]] = []

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        return ClientResponse(201, {"sha256": artifact.sha256}, {}, 1)

    def ingest_batch(
        self,
        payload: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        self.batch_calls.append(payload)
        return ClientResponse(202, {"accepted": True, "batch_id": batch_id}, {}, 1)


def _make_runtime(
    tmp_path: Path,
    client: _PrivacyHTTPClient,
    *,
    server_url: str,
    clipboard_enabled: bool,
    remote_clipboard: bool = False,
) -> CollectorRuntime:
    spool_dir = tmp_path / "collector-spool"
    transport = CollectorTransport(
        TransportConfig(
            server_url=server_url,
            spool_dir=spool_dir,
            max_attempts=1,
            backoff_initial_seconds=0,
            backoff_max_seconds=0,
            fsync=False,
        ),
        client=client,
    )
    capture = ScreenCaptureService(
        _PrivacyCaptureBackend(),
        config=CaptureConfig(interval_seconds=5, include_contact_sheet=False),
        now=lambda: CAPTURED_AT,
        sequence_id_factory=lambda: "privacy-sequence",
    )
    return CollectorRuntime(
        CollectorConfig(
            roots=(),
            clipboard_enabled=clipboard_enabled,
            browser_enabled=False,
            include_git_history=False,
            include_processes=False,
            spool_dir=spool_dir,
            remote_clipboard=remote_clipboard,
        ),
        transport=transport,
        capture=capture,
    )


def test_collector_envelopes_have_no_raw_keystrokes_and_no_media_payloads(
    tmp_path: Path,
) -> None:
    client = _PrivacyHTTPClient()
    runtime = _make_runtime(
        tmp_path,
        client,
        server_url="http://127.0.0.1:8000",
        clipboard_enabled=False,
    )

    try:
        runtime.tick()
    finally:
        runtime.close()

    assert len(client.batch_calls) == 1
    envelope = client.batch_calls[0]
    validated = CaptureEnvelope.model_validate(envelope)

    _assert_no_raw_keystroke_events(envelope)
    assert validated.metadata["audio"] is False
    assert validated.metadata["video"] is False
    assert validated.metadata["keylogging"] is False
    assert all(ref["kind"] not in {"audio", "video"} for ref in envelope["artifacts"])
    for event in envelope["events"]:
        assert event["payload"].get("audio", False) is False
        assert event["payload"].get("video", False) is False
        assert event["metadata"].get("keylogging", False) is False


def test_source_adapters_emit_metadata_or_explicit_clipboard_values_only(
    tmp_path: Path,
) -> None:
    class FakeWindowsApi:
        def foreground_window(self) -> int:
            return 101

        def process_id(self, hwnd: int) -> int:
            return 4242

        def process_executable(self, process_id: int) -> str:
            return r"C:\Program Files\Example\example.exe"

        def process_created_at(self, process_id: int) -> float:
            return 1_700_000_000.25

        def window_title(self, hwnd: int) -> str:
            return "Example window"

        def monitor(self, hwnd: int) -> str:
            return r"\\.\DISPLAY1"

    root = tmp_path / "watched"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("metadata-only fixture", encoding="utf-8")
    file_event = normalize_file_event(
        "created",
        target,
        root,
        timestamp=1_800_000_000.0,
        compute_hash=True,
    )
    assert file_event is not None

    history = tmp_path / "ConsoleHost_history.txt"
    history.write_text("Get-Process\nGet-Date\n", encoding="utf-8")
    git_terminal_events = collect_git_terminal_events(
        repositories=(),
        include_history=True,
        history_path=history,
        include_processes=True,
        max_processes=1,
        environment={},
    )

    clipboard = ClipboardSource(
        enabled=True,
        reader=lambda: "explicit clipboard value",
        prefer_listener=False,
    ).poll_once()
    assert clipboard is not None
    assert clipboard.source == "clipboard"
    assert clipboard.routing["source_type"] == "clipboard_text"

    values = [
        WindowsWindowMetadataSource(
            api=FakeWindowsApi(),
            clock=lambda: 1_800_000_000.5,
        ).collect().as_dict(),
        BrowserSource(
            fallback=SafeBrowserMetadataFallback(lambda: "Foreground browser title")
        ).read().as_dict(),
        clipboard.as_dict(),
        file_event.to_dict(),
        git_terminal_events,
    ]
    for value in values:
        _assert_no_raw_keystroke_events(value)

    terminal_payloads = {
        event["payload"]["event_kind"]: event["payload"]
        for event in git_terminal_events
    }
    assert terminal_payloads["terminal_metadata"]["input_collected"] is False
    assert terminal_payloads["process_metadata"]["command_lines_collected"] is False
    assert terminal_payloads["process_metadata"]["environment_collected"] is False
    assert terminal_payloads["process_metadata"]["keylogging"] is False


def test_source_audio_video_apis_return_none_and_capabilities_are_false() -> None:
    sources = (
        NoAudioVideoSource(),
        BrowserSource(enabled=False),
        ClipboardSource(enabled=False),
    )
    for source in sources:
        assert source.supports_audio is False
        assert source.supports_video is False
        assert source.no_audio is True
        assert source.no_video is True
        assert source.read_audio() is None
        assert source.read_video() is None
        assert source.capture_audio() is None
        assert source.capture_video() is None

    browser = BrowserMetadata(title="Metadata title")
    clipboard = ClipboardEvent(text="explicit clipboard text")
    assert browser.audio is None
    assert browser.video is None
    assert browser.as_dict()["audio"] is None
    assert browser.as_dict()["video"] is None
    assert clipboard.audio is None
    assert clipboard.video is None
    assert clipboard.as_dict()["audio"] is None
    assert clipboard.as_dict()["video"] is None


def test_browser_fallback_is_metadata_only() -> None:
    class BrokenMetadataProvider:
        def read_metadata(self) -> object:
            raise RuntimeError("page body must never escape the provider")

    result = BrowserSource(
        metadata_source=BrokenMetadataProvider(),
        fallback=SafeBrowserMetadataFallback(lambda: "Foreground browser title"),
    ).read()
    payload = result.as_dict()

    assert result.used_fallback is True
    assert result.title == "Foreground browser title"
    assert result.url is None
    assert result.browser is None
    assert payload["routing"]["metadata_only"] is True
    assert payload["routing"]["supports_text"] is False
    assert payload["audio"] is None
    assert payload["video"] is None
    assert "page body must never escape" not in repr(payload)
    assert not {"page_body", "dom", "html", "content", "body"} & payload.keys()
    _assert_no_raw_keystroke_events(payload)


def test_clipboard_plaintext_is_explicit_and_local_only_by_default(
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    def reader() -> str:
        calls.append(1)
        return "disabled clipboard must not be read"

    default_source = ClipboardSource(reader=reader)
    assert default_source.enabled is False
    assert default_source.read_current() is None
    assert calls == []

    client = _PrivacyHTTPClient()
    runtime = _make_runtime(
        tmp_path,
        client,
        server_url="https://remote.example.test",
        clipboard_enabled=True,
    )
    raw_text = "  local-only clipboard\r\nwith Unicode: café 🔒  "
    runtime.clipboard_source = ClipboardSource(
        enabled=True,
        reader=lambda: raw_text,
        prefer_listener=False,
    )

    try:
        runtime.tick()
        local_batches = runtime.local_only_spool.list_pending_batches()
    finally:
        runtime.close()

    assert CollectorConfig(roots=(), spool_dir=tmp_path / "defaults").remote_clipboard is False
    assert len(client.batch_calls) == 1
    remote_envelope = client.batch_calls[0]
    assert "clipboard:local" not in {event["source_id"] for event in remote_envelope["events"]}
    assert raw_text not in repr(remote_envelope)

    assert len(local_batches) == 1
    local_event = local_batches[0].payload["events"][0]
    assert local_event["source_id"] == "clipboard:local"
    assert local_event["payload"]["raw_text"] == raw_text
    assert local_event["metadata"]["plaintext"] is True
    assert local_event["metadata"]["remote_route"] == "explicit_opt_in"
    _assert_no_raw_keystroke_events(local_event)
