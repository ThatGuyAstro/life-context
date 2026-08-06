"""Portable backend protocol and the Python ``mss`` fallback backend."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

from .models import Monitor, RawFrame


class CaptureUnavailableError(RuntimeError):
    """Raised when a capture backend cannot be used on the current host."""


class CaptureBackendError(RuntimeError):
    """Raised when a backend fails while enumerating or capturing a monitor."""


@runtime_checkable
class CaptureBackend(Protocol):
    """The OS-neutral contract consumed by :class:`ScreenCaptureService`."""

    @property
    def name(self) -> str:
        ...

    def enumerate_monitors(self) -> Sequence[Monitor]:
        ...

    def capture(self, monitor: Monitor) -> RawFrame:
        ...

    def close(self) -> None:
        ...


class MssPillowCaptureBackend(AbstractContextManager["MssPillowCaptureBackend"]):
    """Cross-platform Python fallback using ``mss`` for pixels.

    JPEG conversion is deliberately kept in ``images.py``.  This backend only
    knows how to enumerate physical monitors and return tightly packed RGB or
    BGRA bytes, which makes it straightforward to replace on Windows.
    """

    def __init__(
        self,
        *,
        sct_factory: Callable[[], Any] | None = None,
    ) -> None:
        if sct_factory is None:
            try:
                import mss
            except ImportError as exc:  # pragma: no cover - environment-specific
                raise CaptureUnavailableError(
                    "mss is required for the Python screen-capture fallback"
                ) from exc
            sct_factory = mss.mss
        try:
            self._sct = sct_factory()
        except Exception as exc:  # pragma: no cover - environment-specific
            raise CaptureUnavailableError("unable to initialize mss") from exc

    @property
    def name(self) -> str:
        return "mss-pillow-fallback"

    def enumerate_monitors(self) -> tuple[Monitor, ...]:
        """Return all physical monitors, excluding mss's virtual desktop entry."""

        try:
            entries = list(self._sct.monitors)
        except Exception as exc:
            raise CaptureBackendError("mss monitor enumeration failed") from exc

        # mss documents monitors[0] as the virtual desktop and monitors[1:]
        # as physical monitors.  A one-entry fake/provider is accepted as a
        # useful adapter behavior for tests and alternate mss-compatible APIs.
        physical_entries = entries[1:] if len(entries) > 1 else entries
        monitors: list[Monitor] = []
        seen_ids: set[str] = set()
        for offset, entry in enumerate(physical_entries, start=1):
            if not isinstance(entry, Mapping):
                raise CaptureBackendError("mss returned an invalid monitor entry")
            monitor = self._monitor_from_mapping(entry, offset)
            if monitor.monitor_id in seen_ids:
                monitor = Monitor(
                    monitor_id=f"{monitor.monitor_id}-{offset}",
                    name=monitor.name,
                    left=monitor.left,
                    top=monitor.top,
                    width=monitor.width,
                    height=monitor.height,
                    is_primary=monitor.is_primary,
                )
            seen_ids.add(monitor.monitor_id)
            monitors.append(monitor)
        return tuple(monitors)

    @staticmethod
    def _monitor_from_mapping(entry: Mapping[str, Any], index: int) -> Monitor:
        try:
            left = int(entry["left"])
            top = int(entry["top"])
            width = int(entry["width"])
            height = int(entry["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaptureBackendError("mss monitor is missing valid geometry") from exc

        name = str(entry.get("name") or entry.get("device") or f"Monitor {index}")
        monitor_id = str(entry.get("id") or entry.get("name") or f"monitor-{index}")
        return Monitor(
            monitor_id=monitor_id,
            name=name,
            left=left,
            top=top,
            width=width,
            height=height,
            # mss orders the primary monitor first in its physical list on
            # supported Windows/Linux backends; this remains best-effort.
            is_primary=index == 1,
        )

    def capture(self, monitor: Monitor) -> RawFrame:
        request = {
            "left": monitor.left,
            "top": monitor.top,
            "width": monitor.width,
            "height": monitor.height,
        }
        try:
            screenshot = self._sct.grab(request)
            width = int(getattr(screenshot, "width", monitor.width))
            height = int(getattr(screenshot, "height", monitor.height))
            rgb = getattr(screenshot, "rgb", None)
            if rgb is not None:
                return RawFrame(
                    width=width,
                    height=height,
                    pixel_bytes=bytes(rgb),
                    pixel_format="RGB",
                    metadata={"backend": self.name, "monitor_id": monitor.monitor_id},
                )

            bgra = getattr(screenshot, "bgra", None)
            if bgra is not None:
                return RawFrame(
                    width=width,
                    height=height,
                    pixel_bytes=bytes(bgra),
                    pixel_format="BGRA",
                    metadata={"backend": self.name, "monitor_id": monitor.monitor_id},
                )
        except Exception as exc:
            raise CaptureBackendError(
                f"mss capture failed for monitor {monitor.monitor_id}"
            ) from exc
        raise CaptureBackendError("mss screenshot had no RGB or BGRA pixel buffer")

    def close(self) -> None:
        close = getattr(self._sct, "close", None)
        if close is not None:
            close()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def create_default_backend(
    *,
    prefer_windows: bool = True,
    sct_factory: Callable[[], Any] | None = None,
) -> CaptureBackend:
    """Select the Windows adapter when available, otherwise use mss/Pillow.

    The Windows module is imported only on Windows.  A native/OS-specific
    failure therefore cannot make the portable fallback unimportable.
    """

    if prefer_windows and os.name == "nt":
        try:
            from .windows import WindowsCaptureBackend

            return WindowsCaptureBackend()
        except (CaptureUnavailableError, OSError, RuntimeError):
            # The fallback remains usable in services, RDP sessions, and
            # environments where the Windows desktop API is unavailable.
            pass
    return MssPillowCaptureBackend(sct_factory=sct_factory)


# Friendly aliases for callers that name the implementation by its role.
MssCaptureBackend = MssPillowCaptureBackend
PythonFallbackCaptureBackend = MssPillowCaptureBackend
