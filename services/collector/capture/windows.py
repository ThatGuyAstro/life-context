"""Windows-only monitor enumeration and Pillow ``ImageGrab`` adapter.

This module is intentionally isolated from the portable service.  It uses the
Win32 monitor APIs to retain negative virtual-desktop coordinates and Pillow's
Windows screen grabber for the actual pixels.  Importing this module is safe on
other platforms; constructing the backend is not.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Sequence
from typing import Any

from .backends import CaptureBackendError, CaptureUnavailableError
from .models import Monitor, RawFrame


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


MONITORINFOF_PRIMARY = 1


def enumerate_windows_monitors() -> tuple[Monitor, ...]:
    """Enumerate physical Windows monitors through ``EnumDisplayMonitors``."""

    if sys.platform != "win32":
        raise CaptureUnavailableError("Windows monitor enumeration requires Windows")

    try:
        user32 = ctypes.windll.user32
    except AttributeError as exc:  # pragma: no cover - only non-Windows runtimes
        raise CaptureUnavailableError("Win32 user32.dll is unavailable") from exc

    monitors: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_Rect),
        ctypes.c_void_p,
    )

    def callback(_handle, _hdc, _rect, _data) -> int:
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if not user32.GetMonitorInfoW(_handle, ctypes.byref(info)):
            return 1
        rect = info.rcMonitor
        name = info.szDevice.rstrip("\x00") or f"Monitor {len(monitors) + 1}"
        monitors.append(
            Monitor(
                monitor_id=name,
                name=name,
                left=rect.left,
                top=rect.top,
                width=rect.right - rect.left,
                height=rect.bottom - rect.top,
                is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
            )
        )
        return 1

    callback_ref = callback_type(callback)
    if not user32.EnumDisplayMonitors(None, None, callback_ref, 0):
        raise CaptureBackendError("EnumDisplayMonitors failed")
    monitors.sort(key=lambda monitor: (not monitor.is_primary, monitor.monitor_id))
    return tuple(monitors)


class WindowsCaptureBackend:
    """Windows screen capture using Pillow's ``ImageGrab`` implementation."""

    def __init__(self, *, image_grab: Any | None = None) -> None:
        if sys.platform != "win32":
            raise CaptureUnavailableError("WindowsCaptureBackend requires Windows")
        if image_grab is None:
            try:
                from PIL import ImageGrab
            except ImportError as exc:  # pragma: no cover - environment-specific
                raise CaptureUnavailableError(
                    "Pillow is required for the Windows capture adapter"
                ) from exc
            image_grab = ImageGrab
        if not hasattr(image_grab, "grab"):
            raise CaptureUnavailableError("ImageGrab adapter has no grab method")
        self._image_grab = image_grab

    @property
    def name(self) -> str:
        return "windows-imagegrab"

    def enumerate_monitors(self) -> Sequence[Monitor]:
        return enumerate_windows_monitors()

    def capture(self, monitor: Monitor) -> RawFrame:
        try:
            try:
                image = self._image_grab.grab(bbox=monitor.bounds, all_screens=True)
            except TypeError:
                # Older Pillow builds may not expose all_screens.  The bbox is
                # still correct for the primary display on those builds.
                image = self._image_grab.grab(bbox=monitor.bounds)
            image = image.convert("RGB")
            width, height = image.size
            return RawFrame(
                width=int(width),
                height=int(height),
                pixel_bytes=image.tobytes(),
                pixel_format="RGB",
                metadata={"backend": self.name, "monitor_id": monitor.monitor_id},
            )
        except Exception as exc:
            raise CaptureBackendError(
                f"Windows screen capture failed for monitor {monitor.monitor_id}"
            ) from exc

    def close(self) -> None:
        """The Pillow adapter owns no persistent native handle."""


# Explicit name for dependency injection and discovery.
WindowsScreenCaptureBackend = WindowsCaptureBackend
