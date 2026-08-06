"""Deterministic Windows capture/privacy acceptance coverage.

The Win32 monitor registry and Pillow ImageGrab path are both replaced with
safe local fakes.  This keeps the acceptance check useful on CI or a locked
desktop without requiring a live display, VLM, camera, microphone, or GPU.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from contracts import CaptureEnvelope
from services.collector.capture import CaptureConfig, ScreenCaptureService, WindowsCaptureBackend
from services.collector.capture import windows as windows_module
from services.collector.runtime import CollectorConfig, CollectorRuntime
from services.collector.transport.client import ClientResponse
from services.collector.transport.config import TransportConfig
from services.collector.transport.transport import CollectorTransport


CAPTURED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

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


@dataclass(frozen=True)
class _MonitorSpec:
    handle: int
    device: str
    left: int
    top: int
    right: int
    bottom: int
    primary: bool = False


_MONITORS = (
    _MonitorSpec(101, r"\\.\DISPLAY2", -4, 0, 0, 3),
    _MonitorSpec(202, r"\\.\DISPLAY1", 0, 0, 4, 3, primary=True),
    _MonitorSpec(303, r"\\.\DISPLAY3", 4, 0, 8, 3),
)


class _FakeUser32:
    def __init__(self, monitors: tuple[_MonitorSpec, ...]) -> None:
        self.monitors = {monitor.handle: monitor for monitor in monitors}
        self.enumeration_calls = 0

    def EnumDisplayMonitors(self, _hdc, _clip, callback, _data) -> int:
        self.enumeration_calls += 1
        for monitor in self.monitors.values():
            callback(monitor.handle, None, None, None)
        return 1

    def GetMonitorInfoW(self, handle: int, info_pointer: object) -> int:
        monitor = self.monitors[handle]
        info = ctypes.cast(
            info_pointer,
            ctypes.POINTER(windows_module._MonitorInfoEx),
        ).contents
        info.rcMonitor.left = monitor.left
        info.rcMonitor.top = monitor.top
        info.rcMonitor.right = monitor.right
        info.rcMonitor.bottom = monitor.bottom
        info.dwFlags = windows_module.MONITORINFOF_PRIMARY if monitor.primary else 0
        info.szDevice = monitor.device
        return 1


class _FakeImageGrab:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def grab(self, **kwargs: object) -> Image.Image:
        self.calls.append(kwargs)
        left, top, right, bottom = kwargs["bbox"]  # type: ignore[misc]
        width = int(right) - int(left)
        height = int(bottom) - int(top)
        colors = {-4: (220, 40, 40), 0: (40, 180, 80), 4: (40, 80, 220)}
        return Image.new("RGB", (width, height), colors[int(left)])


class _AcceptingHTTPClient:
    def __init__(self) -> None:
        self.artifact_calls: list[Any] = []
        self.batch_calls: list[tuple[dict[str, Any], str | None]] = []

    def upload_artifact(self, artifact: Any) -> ClientResponse:
        self.artifact_calls.append(artifact)
        return ClientResponse(201, {"sha256": artifact.sha256}, {}, 1)

    def ingest_batch(
        self,
        payload: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        self.batch_calls.append((payload, batch_id))
        return ClientResponse(202, {"accepted": True}, {}, 1)


def _install_fake_windows_capture(monkeypatch):
    fake_user32 = _FakeUser32(_MONITORS)
    fake_image_grab = _FakeImageGrab()
    monkeypatch.setattr(windows_module.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_module.ctypes,
        "windll",
        type("_FakeWindll", (), {"user32": fake_user32})(),
        raising=False,
    )
    if not hasattr(windows_module.ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(
            windows_module.ctypes,
            "WINFUNCTYPE",
            lambda *_args: lambda callback: callback,
        )
    return fake_user32, fake_image_grab


def _assert_no_raw_keystroke_capture(value: object) -> None:
    violations: list[str] = []

    def walk(item: object, path: str = "$") -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
                child_path = f"{path}.{key}"
                if normalized in _RAW_KEY_FIELDS:
                    violations.append(child_path)
                elif normalized == "keylogging" and nested is not False:
                    violations.append(f"{child_path}=not-false")
                walk(nested, child_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                walk(nested, f"{path}[{index}]")

    walk(value)
    assert not violations, f"raw keystroke capture found at: {violations}"


def test_windows_capture_privacy_acceptance_all_monitors_tiers_and_boundaries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Exercise the complete screenshot-only Windows collector contract."""

    fake_user32, fake_image_grab = _install_fake_windows_capture(monkeypatch)
    backend = WindowsCaptureBackend(image_grab=fake_image_grab)
    capture = ScreenCaptureService(
        backend,
        config=CaptureConfig(
            interval_seconds=5,
            jpeg_quality=80,
            contact_sheet_max_tile_size=(8, 6),
            contact_sheet_padding=1,
            contact_sheet_label_height=4,
        ),
        now=lambda: CAPTURED_AT,
        sequence_id_factory=lambda: "windows-acceptance-001",
    )
    client = _AcceptingHTTPClient()
    spool_dir = tmp_path / "spool"
    transport = CollectorTransport(
        TransportConfig(
            server_url="http://127.0.0.1:8000",
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
            interval_seconds=5,
            roots=(),
            clipboard_enabled=False,
            browser_enabled=False,
            include_git_history=False,
            include_processes=False,
            spool_dir=spool_dir,
        ),
        transport=transport,
        capture=capture,
    )
    runtime._source_events = lambda: []  # type: ignore[method-assign]

    try:
        report = runtime.tick()
    finally:
        runtime.close()

    expected_ids = [r"\\.\DISPLAY1", r"\\.\DISPLAY2", r"\\.\DISPLAY3"]
    expected_bounds = [
        (0, 0, 4, 3),
        (-4, 0, 0, 3),
        (4, 0, 8, 3),
    ]
    assert fake_user32.enumeration_calls == 1
    assert [call["bbox"] for call in fake_image_grab.calls] == expected_bounds
    assert all(call["all_screens"] is True for call in fake_image_grab.calls)
    assert report["sequence_id"] == "windows-acceptance-001"
    assert report["monitor_count"] == len(expected_ids)
    assert report["event_count"] == len(expected_ids) + 1

    assert len(client.artifact_calls) == len(expected_ids) + 1
    stored_by_name = {artifact.metadata["name"]: artifact for artifact in client.artifact_calls}
    assert set(stored_by_name) == {
        *(f"{monitor_id}.jpg" for monitor_id in expected_ids),
        "contact-sheet.jpg",
    }
    for monitor_id in expected_ids:
        stored = stored_by_name[f"{monitor_id}.jpg"]
        assert stored.metadata["retention_tier"] == "raw"
        assert stored.metadata["monitor_id"] == monitor_id
        assert stored.size > 0
        assert stored.path.is_file()
    assert stored_by_name["contact-sheet.jpg"].metadata["retention_tier"] == "keyframe"
    assert stored_by_name["contact-sheet.jpg"].metadata["monitor_id"] is None

    assert len(client.batch_calls) == 1
    envelope, batch_id = client.batch_calls[0]
    assert batch_id == "windows-acceptance-001"
    validated = CaptureEnvelope.model_validate(envelope)
    assert validated.metadata["monitor_count"] == len(expected_ids)
    assert validated.metadata["audio"] is False
    assert validated.metadata["video"] is False
    assert validated.metadata["keylogging"] is False

    refs_by_name = {ref["name"]: ref for ref in envelope["artifacts"]}
    for monitor_id in expected_ids:
        ref = refs_by_name[f"{monitor_id}.jpg"]
        assert ref["kind"] == "image"
        assert ref["media_type"] == "image/jpeg"
        assert ref["metadata"] == {"monitor_id": monitor_id, "retention_tier": "raw"}
    contact_ref = refs_by_name["contact-sheet.jpg"]
    assert contact_ref["kind"] == "image"
    assert contact_ref["media_type"] == "image/jpeg"
    assert contact_ref["metadata"] == {"retention_tier": "keyframe"}
    assert all(ref["kind"] not in {"audio", "video"} for ref in envelope["artifacts"])

    event_ids = {event["source_id"] for event in envelope["events"]}
    assert {f"screen:{monitor_id}" for monitor_id in expected_ids} <= event_ids
    assert "screen:windows-acceptance-001:contact-sheet" in event_ids
    for event in envelope["events"]:
        assert event["payload"].get("audio", False) is False
        assert event["payload"].get("video", False) is False
        assert event["metadata"].get("audio", False) is False
        assert event["metadata"].get("video", False) is False
    _assert_no_raw_keystroke_capture(envelope)
