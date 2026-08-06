from __future__ import annotations

from hashlib import sha256

import pytest

from services.collector.capture import (
    CaptureConfig,
    MssPillowCaptureBackend,
    Monitor,
    RawFrame,
    ScreenCaptureService,
)
from services.collector.capture.backends import create_default_backend
from services.collector.capture.images import generate_contact_sheet
from services.collector.capture.windows import WindowsCaptureBackend


class FakeScreenshot:
    def __init__(self, width: int, height: int, rgb: bytes) -> None:
        self.width = width
        self.height = height
        self.rgb = rgb


class FakeMss:
    def __init__(self) -> None:
        self.monitors = [
            {"left": -100, "top": 0, "width": 300, "height": 100},
            {
                "left": 0,
                "top": 0,
                "width": 2,
                "height": 1,
                "name": "DISPLAY1",
            },
            {
                "left": -2,
                "top": 0,
                "width": 2,
                "height": 1,
                "name": "DISPLAY2",
            },
        ]
        self.requests: list[dict[str, int]] = []
        self.closed = False

    def grab(self, request: dict[str, int]) -> FakeScreenshot:
        self.requests.append(request)
        if request["left"] == 0:
            pixels = bytes((255, 0, 0, 0, 255, 0))
        else:
            pixels = bytes((0, 0, 255, 255, 255, 0))
        return FakeScreenshot(request["width"], request["height"], pixels)

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    name = "fake-backend"

    def __init__(self) -> None:
        self.monitors = (
            Monitor("one", "One", 0, 0, 2, 2, True),
            Monitor("two", "Two", 2, 0, 2, 2, False),
        )
        self.calls: list[str] = []
        self.closed = False

    def enumerate_monitors(self):
        return self.monitors

    def capture(self, monitor: Monitor) -> RawFrame:
        self.calls.append(monitor.monitor_id)
        color = (255, 0, 0) if monitor.monitor_id == "one" else (0, 0, 255)
        return RawFrame(
            width=monitor.width,
            height=monitor.height,
            pixel_bytes=bytes(color) * (monitor.width * monitor.height),
            metadata={"fixture": True},
        )

    def close(self) -> None:
        self.closed = True


def test_mss_backend_enumerates_physical_monitors_and_preserves_virtual_coordinates():
    fake_mss = FakeMss()
    backend = MssPillowCaptureBackend(sct_factory=lambda: fake_mss)

    monitors = backend.enumerate_monitors()

    assert [monitor.monitor_id for monitor in monitors] == ["DISPLAY1", "DISPLAY2"]
    assert monitors[0].is_primary is True
    assert monitors[1].left == -2
    assert monitors[1].bounds == (-2, 0, 0, 1)

    frame = backend.capture(monitors[1])
    assert frame.pixel_format == "RGB"
    assert len(frame.pixel_bytes) == 2 * 1 * 3
    assert fake_mss.requests == [
        {"left": -2, "top": 0, "width": 2, "height": 1}
    ]

    backend.close()
    assert fake_mss.closed is True


def test_service_encodes_each_monitor_adds_metadata_hashes_and_contact_sheet():
    backend = FakeBackend()
    service = ScreenCaptureService(
        backend,
        config=CaptureConfig(
            interval_seconds=1,
            jpeg_quality=90,
            contact_sheet_max_tile_size=(20, 20),
            contact_sheet_padding=2,
            contact_sheet_label_height=10,
        ),
        now=lambda: __import__("datetime").datetime(2026, 1, 1),
        sequence_id_factory=lambda: "capture-test-0001",
    )

    result = service.capture_once()

    assert result.sequence_id == "capture-test-0001"
    assert len(result.monitors) == 2
    assert backend.calls == ["one", "two"]
    assert result.contact_sheet is not None
    assert result.contact_sheet_jpeg.startswith(b"\xff\xd8")
    assert result.contact_sheet_sha256 == sha256(result.contact_sheet_jpeg).hexdigest()
    assert result.metadata["monitor_count"] == 2
    assert result.metadata["audio"] is False
    assert result.metadata["video"] is False

    for capture in result.monitors:
        assert capture.sequence_id == result.sequence_id
        assert capture.jpeg_bytes.startswith(b"\xff\xd8")
        assert capture.sha256 == sha256(capture.jpeg_bytes).hexdigest()
        assert capture.metadata["sequence_id"] == result.sequence_id
        assert capture.metadata["monitor_id"] == capture.monitor.monitor_id
        assert capture.metadata["fixture"] is True
        assert capture.metadata["sha256"] == capture.sha256


def test_contact_sheet_respects_columns_and_tile_limits():
    backend = FakeBackend()
    service = ScreenCaptureService(
        backend,
        config=CaptureConfig(include_contact_sheet=False),
        sequence_id_factory=lambda: "one",
    )
    result = service.capture_once()
    images = [(capture.monitor.name, capture.image) for capture in result.monitors]

    sheet = generate_contact_sheet(
        images,
        columns=1,
        max_tile_size=(5, 5),
        padding=1,
        label_height=3,
    )

    assert sheet.width == 4
    assert sheet.height == 2 * (3 + 2 + 1) + 1


def test_run_captures_immediately_then_sleeps_at_configured_interval():
    backend = FakeBackend()
    current = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        current[0] += seconds

    service = ScreenCaptureService(
        backend,
        config=CaptureConfig(interval_seconds=2.5, include_contact_sheet=False),
        monotonic=monotonic,
        sleep=sleep,
        sequence_id_factory=iter(("one", "two", "three")).__next__,
    )

    emitted = service.run(max_sequences=3)

    assert emitted == 3
    assert sleeps == pytest.approx([2.5, 2.5])
    assert current[0] == pytest.approx(5.0)
    assert len(backend.calls) == 6


def test_default_factory_can_be_forced_to_use_python_fallback():
    fake_mss = FakeMss()
    backend = create_default_backend(
        prefer_windows=False,
        sct_factory=lambda: fake_mss,
    )

    assert isinstance(backend, MssPillowCaptureBackend)
    assert backend.name == "mss-pillow-fallback"
    backend.close()


def test_windows_adapter_isolated_from_core_and_returns_raw_rgb(monkeypatch):
    from PIL import Image

    import services.collector.capture.windows as windows_module

    calls: list[dict[str, object]] = []

    class FakeImageGrab:
        @staticmethod
        def grab(**kwargs):
            calls.append(kwargs)
            return Image.new("RGB", (2, 1), (12, 34, 56))

    monkeypatch.setattr(windows_module.sys, "platform", "win32")
    backend = WindowsCaptureBackend(image_grab=FakeImageGrab)
    monitor = Monitor("display-1", "DISPLAY1", 0, 0, 2, 1, True)

    frame = backend.capture(monitor)

    assert frame.pixel_format == "RGB"
    assert frame.width == 2
    assert frame.height == 1
    assert calls == [{"bbox": (0, 0, 2, 1), "all_screens": True}]


def test_context_manager_closes_backend():
    backend = FakeBackend()
    with ScreenCaptureService(
        backend,
        config=CaptureConfig(include_contact_sheet=False),
        sequence_id_factory=lambda: "context",
    ):
        pass
    assert backend.closed is True
