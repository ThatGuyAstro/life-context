"""Browser metadata and opt-in clipboard sources.

This module deliberately has a small, dependency-free surface.  A browser
source can be backed by a browser extension, accessibility adapter, or any
other caller-owned provider.  The built-in fallback only reads the title of a
known foreground browser window on Windows; it never reads page contents and
cannot manufacture a URL.

Clipboard capture is opt-in.  On Windows the source first attempts the native
``AddClipboardFormatListener`` notification API and falls back to polling when
that API is unavailable.  Clipboard text is read from ``CF_UNICODETEXT`` and
is returned without stripping, newline conversion, whitespace normalization,
or decoding/re-encoding.  The terminating Win32 NUL is a format terminator,
not part of the clipboard text, and is the only character removed.

Neither source captures audio or video.  The explicit ``read_audio`` and
``read_video`` methods always return ``None`` so callers do not accidentally
route these sources into a media pipeline.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

from ctypes import wintypes


SOURCE_BROWSER = "browser"
SOURCE_CLIPBOARD = "clipboard"
ROUTE_BROWSER = "collector.sources.browser"
ROUTE_CLIPBOARD = "collector.sources.clipboard"

CF_UNICODETEXT = 13
WM_CLIPBOARDUPDATE = 0x031D
WM_QUIT = 0x0012
HWND_MESSAGE = -3

_UNSET = object()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _routing_metadata(
    source: str,
    *,
    enabled: bool,
    available: bool,
    mode: str,
) -> dict[str, object]:
    """Return serializable routing/capability metadata for a source."""

    is_browser = source == SOURCE_BROWSER
    route = ROUTE_BROWSER if is_browser else ROUTE_CLIPBOARD
    kind = "browser_metadata" if is_browser else "clipboard_text"
    return {
        "source": source,
        "source_id": source,
        "source_type": kind,
        "route": route,
        "kind": kind,
        "enabled": bool(enabled),
        "available": bool(available),
        "mode": mode,
        "metadata_only": is_browser,
        "supports_title": is_browser,
        "supports_url": is_browser,
        "supports_text": not is_browser,
        "supports_audio": False,
        "supports_video": False,
        "audio": False,
        "video": False,
    }


# Public templates are useful to a collector router before a source instance
# exists.  Instance events carry a copy with current enabled/available/mode
# values.
SOURCE_ROUTING_METADATA: Mapping[str, Mapping[str, object]] = {
    SOURCE_BROWSER: _routing_metadata(
        SOURCE_BROWSER, enabled=True, available=False, mode="metadata_fallback"
    ),
    SOURCE_CLIPBOARD: _routing_metadata(
        SOURCE_CLIPBOARD, enabled=False, available=False, mode="opt_in"
    ),
}
BROWSER_ROUTING_METADATA = SOURCE_ROUTING_METADATA[SOURCE_BROWSER]
CLIPBOARD_ROUTING_METADATA = SOURCE_ROUTING_METADATA[SOURCE_CLIPBOARD]


def get_source_routing_metadata(
    source: str,
    *,
    enabled: bool,
    available: bool,
    mode: str,
) -> dict[str, object]:
    """Build current routing metadata without exposing mutable templates."""

    if source not in (SOURCE_BROWSER, SOURCE_CLIPBOARD):
        raise ValueError(f"unknown source: {source}")
    return _routing_metadata(
        source, enabled=enabled, available=available, mode=mode
    )


class NoAudioVideoSource:
    """Mixin/API contract shared by these non-media sources."""

    supports_audio = False
    supports_video = False
    no_audio = True
    no_video = True

    def read_audio(self) -> None:
        return None

    def read_video(self) -> None:
        return None

    # These aliases make the behavior explicit for routers that use capture
    # terminology instead of read terminology.
    capture_audio = read_audio
    capture_video = read_video


@dataclass(frozen=True)
class BrowserMetadata:
    """Safe browser metadata; no page body, DOM, audio, or video is stored."""

    title: Optional[str] = None
    url: Optional[str] = None
    browser: Optional[str] = None
    captured_at: datetime = field(default_factory=_utc_now)
    available: bool = False
    used_fallback: bool = False
    fallback_reason: Optional[str] = None
    source: str = SOURCE_BROWSER
    routing: Optional[Mapping[str, object]] = None

    def __post_init__(self) -> None:
        if self.routing is None:
            object.__setattr__(
                self,
                "routing",
                get_source_routing_metadata(
                    SOURCE_BROWSER,
                    enabled=True,
                    available=self.available,
                    mode=("metadata_fallback" if self.used_fallback else "provider"),
                ),
            )

    @property
    def audio(self) -> None:
        return None

    @property
    def video(self) -> None:
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "browser": self.browser,
            "captured_at": self.captured_at.isoformat(),
            "available": self.available,
            "used_fallback": self.used_fallback,
            "fallback_reason": self.fallback_reason,
            "routing": dict(self.routing or {}),
            "metadata": dict(self.routing or {}),
            "audio": None,
            "video": None,
        }

    to_dict = as_dict

    @property
    def metadata(self) -> Mapping[str, object]:
        """Alias for routing metadata used by source-event consumers."""

        return self.routing or {}

    @classmethod
    def from_value(cls, value: object) -> Optional["BrowserMetadata"]:
        """Coerce common provider return values without executing page data."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            title = value.get("title")
            url = value.get("url", value.get("URL"))
            browser = value.get("browser", value.get("browser_name"))
            if not all(item is None or isinstance(item, str) for item in (title, url, browser)):
                return None
            return cls(
                title=title,
                url=url,
                browser=browser,
                available=title is not None or url is not None,
            )
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            title, url = value[0], value[1]
            if not all(item is None or isinstance(item, str) for item in (title, url)):
                return None
            return cls(
                title=title,
                url=url,
                available=title is not None or url is not None,
            )
        return None


@runtime_checkable
class BrowserTitleSource(Protocol):
    """Interface for a provider that can return the active tab/window title."""

    def read_title(self) -> Optional[str]:
        ...


@runtime_checkable
class BrowserUrlSource(Protocol):
    """Interface for a provider that can return the active tab URL."""

    def read_url(self) -> Optional[str]:
        ...


# Both spellings are common in integrations; retain one canonical protocol.
BrowserURLSource = BrowserUrlSource


@runtime_checkable
class BrowserMetadataSource(Protocol):
    """Interface for a provider that returns title and URL metadata together."""

    def read_metadata(self) -> object:
        ...


def _optional_text(value: object) -> Optional[str]:
    # Do not stringify arbitrary provider values: doing so can leak object
    # representations or turn a provider bug into fabricated metadata.
    return value if isinstance(value, str) else None


def _call_provider(provider: object, method_names: tuple[str, ...]) -> object:
    for method_name in method_names:
        method = getattr(provider, method_name, None)
        if callable(method):
            return method()
    if callable(provider):
        return provider()
    return None


_BROWSER_WINDOW_CLASSES = {
    # Chromium-based browsers share this class; the fallback reports the
    # family rather than pretending to identify a particular browser.
    "Chrome_WidgetWin_1": "chromium",
    "MozillaWindowClass": "firefox",
}


def read_foreground_browser_metadata() -> Optional[BrowserMetadata]:
    """Read only a known browser window title on Windows.

    A universal, permission-free way to read the active browser tab URL does
    not exist.  This function intentionally returns ``url=None`` and returns
    no metadata for unknown foreground windows.
    """

    if os.name != "nt":
        return None

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        class_buffer = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, class_buffer, len(class_buffer)) <= 0:
            return None
        browser_family = _BROWSER_WINDOW_CLASSES.get(class_buffer.value)
        if browser_family is None:
            return None

        title_length = max(0, user32.GetWindowTextLengthW(hwnd))
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        title = title_buffer.value
        return BrowserMetadata(
            title=title,
            url=None,
            browser=browser_family,
            available=bool(title),
            used_fallback=True,
            fallback_reason="url_provider_unavailable",
        )
    except Exception:
        # Metadata fallback must never make collection fail because a window
        # API is unavailable, blocked, or behaves differently under Wine.
        return None


def read_foreground_browser_title() -> Optional[str]:
    metadata = read_foreground_browser_metadata()
    return metadata.title if metadata is not None else None


class SafeBrowserMetadataFallback:
    """Conservative fallback that returns metadata only and never page data."""

    def __init__(self, title_reader: Optional[Callable[[], Optional[str]]] = None):
        self._title_reader = title_reader or read_foreground_browser_title

    def read_metadata(self) -> BrowserMetadata:
        try:
            title = _optional_text(self._title_reader())
        except Exception:
            title = None
        return BrowserMetadata(
            title=title,
            url=None,
            available=title is not None,
            used_fallback=True,
            fallback_reason="provider_unavailable",
        )

    read = read_metadata


class BrowserSource(NoAudioVideoSource):
    """Composable browser title/URL source with a safe fallback.

    ``metadata_source`` is preferred when supplied.  Otherwise independent
    ``title_source`` and ``url_source`` interfaces are queried.  Providers are
    caller-owned, which keeps browser-extension/accessibility dependencies out
    of the collector core.
    """

    source = SOURCE_BROWSER

    def __init__(
        self,
        *,
        metadata_source: Optional[object] = None,
        title_source: Optional[object] = None,
        url_source: Optional[object] = None,
        fallback: Optional[object] = None,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self._metadata_source = metadata_source
        self._title_source = title_source
        self._url_source = url_source
        self._fallback = fallback or SafeBrowserMetadataFallback()

    @property
    def routing_metadata(self) -> Mapping[str, object]:
        mode = (
            "metadata_fallback"
            if self._metadata_source is None
            and self._title_source is None
            and self._url_source is None
            else "provider"
        )
        return get_source_routing_metadata(
            SOURCE_BROWSER,
            enabled=self.enabled,
            available=self.available,
            mode=mode,
        )

    source_metadata = routing_metadata

    @property
    def available(self) -> bool:
        return self.enabled and (
            self._metadata_source is not None
            or self._title_source is not None
            or self._url_source is not None
            or os.name == "nt"
        )

    def _fallback_metadata(self, reason: str) -> BrowserMetadata:
        if not self.enabled:
            return BrowserMetadata(
                available=False,
                used_fallback=True,
                fallback_reason="disabled",
                routing=get_source_routing_metadata(
                    SOURCE_BROWSER,
                    enabled=False,
                    available=False,
                    mode="disabled",
                ),
            )

        try:
            value = _call_provider(self._fallback, ("read_metadata", "read", "get_metadata"))
        except Exception:
            value = None
        metadata = BrowserMetadata.from_value(value)
        if metadata is None:
            metadata = BrowserMetadata()
        return replace(
            metadata,
            source=SOURCE_BROWSER,
            available=bool(metadata.title is not None or metadata.url is not None),
            used_fallback=True,
            fallback_reason=reason,
            routing=get_source_routing_metadata(
                SOURCE_BROWSER,
                enabled=True,
                available=bool(metadata.title is not None or metadata.url is not None),
                mode="metadata_fallback",
            ),
        )

    def read(self) -> BrowserMetadata:
        if not self.enabled:
            return self._fallback_metadata("disabled")

        if self._metadata_source is not None:
            try:
                value = _call_provider(
                    self._metadata_source,
                    ("read_metadata", "get_metadata", "read", "get"),
                )
            except Exception:
                return self._fallback_metadata("metadata_provider_error")
            metadata = BrowserMetadata.from_value(value)
            if metadata is not None:
                return replace(
                    metadata,
                    source=SOURCE_BROWSER,
                    available=bool(metadata.title is not None or metadata.url is not None),
                    routing=get_source_routing_metadata(
                        SOURCE_BROWSER,
                        enabled=True,
                        available=bool(metadata.title is not None or metadata.url is not None),
                        mode="provider",
                    ),
                )
            return self._fallback_metadata("metadata_provider_empty")

        title: Optional[str] = None
        url: Optional[str] = None
        title_failed = False
        url_failed = False

        if self._title_source is not None:
            try:
                title = _optional_text(
                    _call_provider(self._title_source, ("read_title", "get_title", "read", "get"))
                )
            except Exception:
                title_failed = True
        if self._url_source is not None:
            try:
                url = _optional_text(
                    _call_provider(self._url_source, ("read_url", "get_url", "read", "get"))
                )
            except Exception:
                url_failed = True

        if title is None and url is None:
            reason = "title_url_provider_error" if title_failed or url_failed else "provider_empty"
            return self._fallback_metadata(reason)

        return BrowserMetadata(
            title=title,
            url=url,
            available=True,
            routing=get_source_routing_metadata(
                SOURCE_BROWSER,
                enabled=True,
                available=True,
                mode="provider",
            ),
        )

    read_metadata = read
    get_metadata = read
    collect = read
    snapshot = read

    def read_title(self) -> Optional[str]:
        return self.read().title

    get_title = read_title

    def read_url(self) -> Optional[str]:
        return self.read().url

    get_url = read_url


@dataclass(frozen=True)
class ClipboardSnapshot:
    """A raw text snapshot and optional Win32 sequence number."""

    text: str
    sequence_number: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("clipboard text must be a str; bytes are not decoded implicitly")

    @property
    def raw_text(self) -> str:
        return self.text


@dataclass(frozen=True)
class ClipboardEvent:
    """A changed clipboard text event; ``text`` is byte-for-character raw."""

    text: str
    captured_at: datetime = field(default_factory=_utc_now)
    sequence_number: Optional[int] = None
    source: str = SOURCE_CLIPBOARD
    routing: Optional[Mapping[str, object]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("clipboard event text must be a str")
        if self.routing is None:
            object.__setattr__(
                self,
                "routing",
                get_source_routing_metadata(
                    SOURCE_CLIPBOARD,
                    enabled=True,
                    available=True,
                    mode="polling",
                ),
            )

    @property
    def raw_text(self) -> str:
        return self.text

    @property
    def audio(self) -> None:
        return None

    @property
    def video(self) -> None:
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "text": self.text,
            "raw_text": self.text,
            "captured_at": self.captured_at.isoformat(),
            "sequence_number": self.sequence_number,
            "routing": dict(self.routing or {}),
            "metadata": dict(self.routing or {}),
            "audio": None,
            "video": None,
        }

    to_dict = as_dict

    @property
    def metadata(self) -> Mapping[str, object]:
        """Alias for routing metadata used by source-event consumers."""

        return self.routing or {}


def read_windows_clipboard_text() -> Optional[str]:
    """Read CF_UNICODETEXT exactly on Windows, or return ``None`` elsewhere."""

    if os.name != "nt":
        return None

    user32 = None
    opened = False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        if not user32.OpenClipboard(None):
            return None
        opened = True
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            # wstring_at stops at the format's terminating NUL.  It does not
            # strip whitespace or translate CRLF/LF.
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        if opened and user32 is not None:
            try:
                user32.CloseClipboard()
            except Exception:
                pass


def read_windows_clipboard_sequence_number() -> Optional[int]:
    """Return Win32's sequence number when available."""

    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        value = int(user32.GetClipboardSequenceNumber())
        return value or None
    except Exception:
        return None


class WindowsClipboardReader:
    """Dependency-free CF_UNICODETEXT reader used by ``ClipboardSource``."""

    def read_text(self) -> Optional[str]:
        return read_windows_clipboard_text()

    def read_snapshot(self) -> Optional[ClipboardSnapshot]:
        text = self.read_text()
        if text is None:
            return None
        return ClipboardSnapshot(
            text=text,
            sequence_number=read_windows_clipboard_sequence_number(),
        )

    read = read_text


def _coerce_clipboard_snapshot(value: object) -> Optional[ClipboardSnapshot]:
    if value is None:
        return None
    if isinstance(value, ClipboardSnapshot):
        return value
    if isinstance(value, ClipboardEvent):
        return ClipboardSnapshot(value.text, value.sequence_number)
    if isinstance(value, str):
        return ClipboardSnapshot(value)
    if isinstance(value, Mapping):
        text = value.get("text", value.get("raw_text"))
        sequence = value.get("sequence_number", value.get("sequence"))
        if not isinstance(text, str):
            return None
        if sequence is not None and not isinstance(sequence, int):
            sequence = None
        return ClipboardSnapshot(text, sequence)
    if isinstance(value, (tuple, list)) and value:
        text = value[0]
        sequence = value[1] if len(value) > 1 else None
        if not isinstance(text, str):
            return None
        if sequence is not None and not isinstance(sequence, int):
            sequence = None
        return ClipboardSnapshot(text, sequence)
    return None


class _Win32ClipboardListener:
    """Small message-only-window wrapper for AddClipboardFormatListener."""

    def __init__(self, on_update: Callable[[], None]) -> None:
        self._on_update = on_update
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hwnd: Any = None
        self._error: Optional[BaseException] = None

    def start(self, timeout: float = 2.0) -> None:
        if os.name != "nt":
            raise OSError("Win32 clipboard listener is only available on Windows")
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="clipboard-listener",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise TimeoutError("timed out starting Win32 clipboard listener")
        if self._error is not None:
            error = self._error
            self.stop()
            raise OSError("could not start Win32 clipboard listener") from error

    def _run(self) -> None:
        user32 = None
        kernel32 = None
        class_name = f"CodexClipboardListener_{id(self):x}"
        hinstance = None
        registered = False
        hwnd = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._thread_id = int(kernel32.GetCurrentThreadId())

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.RegisterClassW.argtypes = [ctypes.c_void_p]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
            user32.AddClipboardFormatListener.restype = wintypes.BOOL
            user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
            user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = [
                ctypes.c_void_p,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int
            user32.TranslateMessage.argtypes = [ctypes.c_void_p]
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
            user32.DispatchMessageW.restype = ctypes.c_ssize_t
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
            user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            user32.UnregisterClassW.restype = wintypes.BOOL

            @WNDPROC
            def window_proc(window, message, wparam, lparam):
                if message == WM_CLIPBOARDUPDATE:
                    try:
                        self._on_update()
                    except Exception:
                        # A consumer callback must not terminate the Win32
                        # message pump; the source remains usable.
                        pass
                return user32.DefWindowProcW(window, message, wparam, lparam)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", getattr(wintypes, "HCURSOR", wintypes.HANDLE)),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            hinstance = kernel32.GetModuleHandleW(None)
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = window_proc
            window_class.hInstance = hinstance
            window_class.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True

            hwnd = user32.CreateWindowExW(
                0,
                class_name,
                class_name,
                0,
                0,
                0,
                0,
                0,
                ctypes.c_void_p(HWND_MESSAGE),
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd
            if not user32.AddClipboardFormatListener(hwnd):
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()

            message = ctypes.create_string_buffer(ctypes.sizeof(wintypes.MSG))
            while not self._stop_requested.is_set():
                result = user32.GetMessageW(message, None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(message)
                user32.DispatchMessageW(message)
        except BaseException as error:
            self._error = error
            self._ready.set()
        finally:
            if user32 is not None and hwnd:
                try:
                    user32.RemoveClipboardFormatListener(hwnd)
                except Exception:
                    pass
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            if registered and user32 is not None:
                try:
                    user32.UnregisterClassW(class_name, hinstance)
                except Exception:
                    pass
            self._hwnd = None

    def stop(self) -> None:
        self._stop_requested.set()
        if self._thread_id is not None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(
                    wintypes.DWORD(self._thread_id), WM_QUIT, 0, 0
                )
            except Exception:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None


class ClipboardSource(NoAudioVideoSource):
    """Opt-in Windows clipboard source with listener and polling modes.

    ``reader`` may be a callable or an object exposing ``read_snapshot``,
    ``read_text``, or ``read``.  Injection is intentional: it permits tests
    and platform adapters to supply a raw string without changing this module.
    A reader returning ``bytes`` is rejected rather than decoded implicitly,
    because implicit decoding cannot guarantee exact text preservation.
    """

    source = SOURCE_CLIPBOARD

    def __init__(
        self,
        *,
        enabled: bool = False,
        reader: Optional[object] = None,
        poll_interval: float = 0.25,
        prefer_listener: bool = True,
        platform_name: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        self.enabled = bool(enabled)
        self._reader_was_injected = reader is not None
        self._reader = reader or WindowsClipboardReader()
        self._poll_interval = float(poll_interval)
        self._prefer_listener = bool(prefer_listener)
        self._is_windows = (
            platform_name.lower() in {"nt", "windows"}
            if platform_name is not None
            else os.name == "nt"
        )
        self._clock = clock or _utc_now
        self._last_key: object = _UNSET
        self._lock = threading.Lock()
        self._stop_event: Optional[threading.Event] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._listener: Optional[_Win32ClipboardListener] = None
        self._callback: Optional[Callable[[ClipboardEvent], None]] = None
        self._mode = "disabled" if not self.enabled else "stopped"

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def available(self) -> bool:
        return self.enabled and (self._is_windows or self._reader_was_injected)

    @property
    def routing_metadata(self) -> Mapping[str, object]:
        return get_source_routing_metadata(
            SOURCE_CLIPBOARD,
            enabled=self.enabled,
            available=self.available,
            mode=self._mode,
        )

    source_metadata = routing_metadata

    def _read_snapshot(self) -> Optional[ClipboardSnapshot]:
        try:
            reader = self._reader
            if callable(getattr(reader, "read_snapshot", None)):
                value = reader.read_snapshot()  # type: ignore[attr-defined]
            elif callable(getattr(reader, "read_text", None)):
                value = reader.read_text()  # type: ignore[attr-defined]
            elif callable(getattr(reader, "read", None)):
                value = reader.read()  # type: ignore[attr-defined]
            elif callable(reader):
                value = reader()
            else:
                return None
        except Exception:
            return None
        return _coerce_clipboard_snapshot(value)

    def read_current(self) -> Optional[ClipboardEvent]:
        """Read the current raw text, including an empty string if present."""

        if not self.enabled:
            return None
        snapshot = self._read_snapshot()
        if snapshot is None:
            return None
        return ClipboardEvent(
            text=snapshot.text,
            captured_at=self._clock(),
            sequence_number=snapshot.sequence_number,
            routing=get_source_routing_metadata(
                SOURCE_CLIPBOARD,
                enabled=True,
                available=self.available,
                mode=self._mode if self._mode != "stopped" else "polling",
            ),
        )

    read = read_current

    def poll_once(self) -> Optional[ClipboardEvent]:
        """Return one event only when the clipboard changed since the last poll."""

        event = self.read_current()
        if event is None:
            return None
        if event.sequence_number is None:
            key: object = ("text", event.text)
        else:
            # A sequence number lets us report copying the same raw text again.
            key = ("sequence", event.sequence_number, event.text)
        with self._lock:
            if self._last_key == key:
                return None
            self._last_key = key
        return event

    poll = poll_once
    collect = poll_once
    snapshot = read_current

    def reset(self) -> None:
        with self._lock:
            self._last_key = _UNSET

    def _deliver(self, callback: Optional[Callable[[ClipboardEvent], None]]) -> None:
        event = self.poll_once()
        if event is not None and callback is not None:
            try:
                callback(event)
            except Exception:
                # A consumer callback should not kill the fallback worker.
                pass

    def _poll_loop(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        self._deliver(self._callback)
        while not stop_event.wait(self._poll_interval):
            self._deliver(self._callback)

    def start(self, callback: Optional[Callable[[ClipboardEvent], None]] = None) -> str:
        """Start listener mode, or polling mode when listener setup fails.

        Returns ``"listener"``, ``"polling"``, ``"unsupported"``, or
        ``"disabled"``.  The method is idempotent while running.
        """

        self._callback = callback
        if not self.enabled:
            self._mode = "disabled"
            return self._mode
        if self._mode in {"listener", "polling"}:
            return self._mode

        if self._prefer_listener and self._is_windows:
            listener = _Win32ClipboardListener(lambda: self._deliver(self._callback))
            try:
                listener.start()
            except Exception:
                listener.stop()
            else:
                self._listener = listener
                self._mode = "listener"
                return self._mode

        if not self.available:
            self._mode = "unsupported"
            return self._mode

        self._stop_event = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="clipboard-poller",
            daemon=True,
        )
        self._poll_thread.start()
        self._mode = "polling"
        return self._mode

    start_listener = start

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()

        stop_event, self._stop_event = self._stop_event, None
        if stop_event is not None:
            stop_event.set()
        thread, self._poll_thread = self._poll_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._poll_interval * 4))
        self._mode = "disabled" if not self.enabled else "stopped"

    close = stop

    def events(
        self,
        *,
        stop_event: Optional[threading.Event] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[ClipboardEvent]:
        """Yield changed events using polling until stopped or timed out."""

        if not self.enabled or not self.available:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        external_stop = stop_event or threading.Event()
        while not external_stop.is_set():
            event = self.poll_once()
            if event is not None:
                yield event
            if deadline is not None and time.monotonic() >= deadline:
                return
            external_stop.wait(self._poll_interval)

    def __enter__(self) -> "ClipboardSource":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


# Descriptive aliases for routers that name the concrete source by platform.
WindowsClipboardSource = ClipboardSource
ClipboardPollingSource = ClipboardSource
ClipboardPollingFallback = ClipboardSource
WindowsClipboardListener = _Win32ClipboardListener


__all__ = [
    "BROWSER_ROUTING_METADATA",
    "BrowserMetadata",
    "BrowserMetadataSource",
    "BrowserSource",
    "BrowserTitleSource",
    "BrowserURLSource",
    "BrowserUrlSource",
    "CF_UNICODETEXT",
    "CLIPBOARD_ROUTING_METADATA",
    "ClipboardEvent",
    "ClipboardPollingSource",
    "ClipboardPollingFallback",
    "ClipboardSnapshot",
    "ClipboardSource",
    "HWND_MESSAGE",
    "NoAudioVideoSource",
    "ROUTE_BROWSER",
    "ROUTE_CLIPBOARD",
    "SOURCE_BROWSER",
    "SOURCE_CLIPBOARD",
    "SOURCE_ROUTING_METADATA",
    "SafeBrowserMetadataFallback",
    "WindowsClipboardReader",
    "WindowsClipboardListener",
    "WindowsClipboardSource",
    "get_source_routing_metadata",
    "read_foreground_browser_metadata",
    "read_foreground_browser_title",
    "read_windows_clipboard_sequence_number",
    "read_windows_clipboard_text",
]
