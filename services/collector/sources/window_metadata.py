"""Foreground-window metadata collection.

The public source contract is deliberately small and platform neutral.  On
Windows, the implementation uses the Win32 APIs exposed by :mod:`ctypes` so
that collecting metadata does not require a UI automation dependency.  On
other platforms, or when a Windows API cannot be loaded or queried, the
source returns a timestamped record whose optional values are ``None``.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WindowMetadata:
    """A point-in-time snapshot of the current foreground window.

    ``timestamp`` and ``process_created_at`` are Unix timestamps in seconds
    (UTC).  The optional values are ``None`` when the platform or a specific
    permission/API lookup is unavailable.  ``foreground_process`` is the
    process image name, while ``executable`` is its full path when available.
    """

    timestamp: float
    foreground_process: Optional[str] = None
    executable: Optional[str] = None
    window_title: Optional[str] = None
    monitor: Optional[str] = None
    process_id: Optional[int] = None
    process_created_at: Optional[float] = None
    available: bool = False

    @property
    def captured_at(self) -> float:
        """Alias for ``timestamp`` for callers using capture terminology."""

        return self.timestamp

    @property
    def observed_at(self) -> float:
        """Alias for ``timestamp`` for callers using observation terminology."""

        return self.timestamp

    @property
    def monitor_name(self) -> Optional[str]:
        """Alias for the associated monitor device name."""

        return self.monitor

    @property
    def process_name(self) -> Optional[str]:
        """Alias for the foreground process image name."""

        return self.foreground_process

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of the snapshot."""

        return {
            "timestamp": self.timestamp,
            "foreground_process": self.foreground_process,
            "executable": self.executable,
            "window_title": self.window_title,
            "monitor": self.monitor,
            "process_id": self.process_id,
            "process_created_at": self.process_created_at,
            "available": self.available,
        }


@runtime_checkable
class WindowMetadataSource(Protocol):
    """Portable interface implemented by foreground metadata sources."""

    def collect(self) -> WindowMetadata:
        """Collect one best-effort foreground-window snapshot."""

    def read(self) -> WindowMetadata:
        """Read one best-effort foreground-window snapshot."""


Clock = Callable[[], float]


class UnavailableWindowMetadataSource:
    """Source used when foreground-window metadata is not available."""

    def __init__(self, *, reason: Optional[str] = None, clock: Clock = time.time) -> None:
        self.reason = reason
        self._clock = clock

    def collect(self) -> WindowMetadata:
        return WindowMetadata(timestamp=_timestamp(self._clock), available=False)

    def read(self) -> WindowMetadata:
        return self.collect()

    def snapshot(self) -> WindowMetadata:
        return self.collect()


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _WindowsApi:
    """Thin, private Win32 wrapper kept separate for deterministic testing."""

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MONITOR_DEFAULTTONEAREST = 2
    ERROR_INSUFFICIENT_BUFFER = 122
    WINDOWS_EPOCH_OFFSET = 11_644_473_600

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        hwnd_type = wintypes.HWND
        handle_type = wintypes.HANDLE
        monitor_type = getattr(wintypes, "HMONITOR", handle_type)

        self._user32.GetForegroundWindow.restype = hwnd_type

        self._user32.GetWindowThreadProcessId.argtypes = [
            hwnd_type,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        self._user32.GetWindowTextLengthW.argtypes = [hwnd_type]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int

        self._user32.GetWindowTextW.argtypes = [
            hwnd_type,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int

        self._user32.MonitorFromWindow.argtypes = [hwnd_type, wintypes.DWORD]
        self._user32.MonitorFromWindow.restype = monitor_type

        self._user32.GetMonitorInfoW.argtypes = [
            monitor_type,
            ctypes.POINTER(_MonitorInfoEx),
        ]
        self._user32.GetMonitorInfoW.restype = wintypes.BOOL

        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = handle_type

        self._kernel32.CloseHandle.argtypes = [handle_type]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            handle_type,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        self._kernel32.GetProcessTimes.argtypes = [
            handle_type,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL

    def foreground_window(self) -> Any:
        hwnd = self._user32.GetForegroundWindow()
        return hwnd or None

    def process_id(self, hwnd: Any) -> Optional[int]:
        process_id = wintypes.DWORD()
        if not self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
            return None
        return int(process_id.value) or None

    def window_title(self, hwnd: Any) -> Optional[str]:
        length = int(self._user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return None

        buffer = ctypes.create_unicode_buffer(length + 1)
        copied = int(self._user32.GetWindowTextW(hwnd, buffer, len(buffer)))
        if copied <= 0:
            return None
        return buffer.value[:copied] or None

    def monitor(self, hwnd: Any) -> Optional[str]:
        monitor = self._user32.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None

        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        return str(info.szDevice).strip() or None

    def _open_process(self, process_id: int) -> Any:
        handle = self._kernel32.OpenProcess(
            self.PROCESS_QUERY_INFORMATION,
            False,
            process_id,
        )
        if handle:
            return handle
        return self._kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        ) or None

    def process_executable(self, process_id: int) -> Optional[str]:
        handle = self._open_process(process_id)
        if not handle:
            return None

        try:
            size = 32_768
            while size <= 1_048_576:
                buffer = ctypes.create_unicode_buffer(size)
                character_count = wintypes.DWORD(size)
                if self._kernel32.QueryFullProcessImageNameW(
                    handle,
                    0,
                    buffer,
                    ctypes.byref(character_count),
                ):
                    return buffer.value[: character_count.value] or None
                if ctypes.get_last_error() != self.ERROR_INSUFFICIENT_BUFFER:
                    return None
                size *= 2
            return None
        finally:
            self._close_handle(handle)

    def process_created_at(self, process_id: int) -> Optional[float]:
        handle = self._open_process(process_id)
        if not handle:
            return None

        try:
            creation = _FileTime()
            exit_time = _FileTime()
            kernel_time = _FileTime()
            user_time = _FileTime()
            if not self._kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None

            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return ticks / 10_000_000 - self.WINDOWS_EPOCH_OFFSET
        finally:
            self._close_handle(handle)

    def _close_handle(self, handle: Any) -> None:
        try:
            self._kernel32.CloseHandle(handle)
        except Exception:
            # Closing a best-effort metadata handle must not make collection fail.
            pass


class WindowsWindowMetadataSource:
    """Best-effort foreground metadata source backed by Win32 APIs.

    ``api`` is an intentionally small injection point for tests or an
    alternative host wrapper.  It can provide ``foreground_window``,
    ``process_id``, ``process_executable``, ``window_title``, ``monitor``, and
    ``process_created_at`` methods.  Each method is optional at runtime and
    failures are treated as unavailable values.
    """

    def __init__(self, api: Any = None, *, clock: Clock = time.time) -> None:
        self._clock = clock
        self._api_error: Optional[str] = None

        if api is not None:
            self._api = api
        elif os.name == "nt":
            try:
                self._api = _WindowsApi()
            except Exception as error:
                self._api = None
                self._api_error = type(error).__name__
        else:
            self._api = None
            self._api_error = "unsupported_platform"

    @property
    def available(self) -> bool:
        """Whether the underlying platform wrapper was initialized."""

        return self._api is not None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """A non-sensitive reason when the platform wrapper is unavailable."""

        return self._api_error

    def collect(self) -> WindowMetadata:
        timestamp = _timestamp(self._clock)
        if self._api is None:
            return WindowMetadata(timestamp=timestamp, available=False)

        hwnd = _safe_call(self._api, "foreground_window")
        if not hwnd:
            return WindowMetadata(timestamp=timestamp, available=False)

        process_id = _as_process_id(_safe_call(self._api, "process_id", hwnd))
        executable = None
        process_created_at = None
        if process_id is not None:
            executable = _as_text(
                _safe_call(self._api, "process_executable", process_id)
            )
            process_created_at = _as_timestamp(
                _safe_call(self._api, "process_created_at", process_id)
            )

        return WindowMetadata(
            timestamp=timestamp,
            foreground_process=_process_name(executable),
            executable=executable,
            window_title=_as_text(_safe_call(self._api, "window_title", hwnd)),
            monitor=_as_text(_safe_call(self._api, "monitor", hwnd)),
            process_id=process_id,
            process_created_at=process_created_at,
            available=True,
        )

    def read(self) -> WindowMetadata:
        return self.collect()

    def snapshot(self) -> WindowMetadata:
        return self.collect()


# A descriptive alias for callers that use "foreground" in their source name.
ForegroundWindowMetadataSource = WindowsWindowMetadataSource


def create_window_metadata_source() -> WindowMetadataSource:
    """Create the native source when supported, otherwise an unavailable one."""

    if os.name == "nt":
        source = WindowsWindowMetadataSource()
        if source.available:
            return source
        return UnavailableWindowMetadataSource(reason=source.unavailable_reason)
    return UnavailableWindowMetadataSource(reason="unsupported_platform")


def get_window_metadata_source() -> WindowMetadataSource:
    """Compatibility alias for :func:`create_window_metadata_source`."""

    return create_window_metadata_source()


def _safe_call(api: Any, method_name: str, *args: Any) -> Any:
    method = getattr(api, method_name, None)
    if not callable(method):
        return None
    try:
        return method(*args)
    except Exception:
        return None


def _timestamp(clock: Clock) -> float:
    try:
        return float(clock())
    except Exception:
        return time.time()


def _as_process_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _as_timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _process_name(executable: Optional[str]) -> Optional[str]:
    if not executable:
        return None
    name = ntpath.basename(executable)
    return name or executable


__all__ = [
    "ForegroundWindowMetadataSource",
    "UnavailableWindowMetadataSource",
    "WindowMetadata",
    "WindowMetadataSource",
    "WindowsWindowMetadataSource",
    "create_window_metadata_source",
    "get_window_metadata_source",
]
