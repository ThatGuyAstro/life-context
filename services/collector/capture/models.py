"""Portable models used by the screen-capture service.

The models intentionally contain no operating-system or image-library types.
Backends produce :class:`RawFrame` instances and the service turns those
frames into JPEG artifacts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


_BYTES_PER_PIXEL = {
    "L": 1,
    "RGB": 3,
    "BGR": 3,
    "RGBA": 4,
    "BGRA": 4,
}


@dataclass(frozen=True, slots=True)
class Monitor:
    """A physical monitor in virtual-desktop coordinates."""

    monitor_id: str
    name: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False

    def __post_init__(self) -> None:
        if not self.monitor_id:
            raise ValueError("monitor_id must not be empty")
        if not self.name:
            raise ValueError("name must not be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("monitor width and height must be positive")

    @property
    def id(self) -> str:
        """Compatibility alias for callers that use ``id``."""

        return self.monitor_id

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """Return ``(left, top, right, bottom)``."""

        return self.left, self.top, self.right, self.bottom

    def as_metadata(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "name": self.name,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "right": self.right,
            "bottom": self.bottom,
            "is_primary": self.is_primary,
        }


@dataclass(frozen=True, slots=True)
class RawFrame:
    """A raw monitor frame independent of Pillow or a specific OS.

    ``pixel_bytes`` are tightly packed rows.  The supported formats mirror
    the common formats returned by desktop capture APIs and by ``mss``.
    """

    width: int
    height: int
    pixel_bytes: bytes
    pixel_format: str = "RGB"
    captured_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame width and height must be positive")

        pixel_format = self.pixel_format.upper()
        if pixel_format not in _BYTES_PER_PIXEL:
            raise ValueError(f"unsupported pixel format: {self.pixel_format}")
        object.__setattr__(self, "pixel_format", pixel_format)

        pixels = bytes(self.pixel_bytes)
        expected = self.width * self.height * _BYTES_PER_PIXEL[pixel_format]
        if len(pixels) != expected:
            raise ValueError(
                f"pixel buffer has {len(pixels)} bytes; expected {expected} "
                f"for {self.width}x{self.height} {pixel_format}"
            )
        object.__setattr__(self, "pixel_bytes", pixels)


@dataclass(frozen=True, slots=True)
class EncodedImage:
    """An encoded JPEG and the information needed to identify it."""

    jpeg_bytes: bytes
    width: int
    height: int
    sha256: str
    mime_type: str = "image/jpeg"

    def __post_init__(self) -> None:
        if not self.jpeg_bytes:
            raise ValueError("jpeg_bytes must not be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("encoded image dimensions must be positive")
        if len(self.sha256) != 64:
            raise ValueError("sha256 must be a hexadecimal SHA-256 digest")

    @property
    def data(self) -> bytes:
        """Short alias for callers that treat an encoded image as a blob."""

        return self.jpeg_bytes


@dataclass(frozen=True, slots=True)
class MonitorCapture:
    """One encoded monitor image belonging to a capture sequence."""

    sequence_id: str
    monitor: Monitor
    captured_at: datetime
    image: EncodedImage
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def jpeg_bytes(self) -> bytes:
        return self.image.jpeg_bytes

    @property
    def sha256(self) -> str:
        return self.image.sha256


@dataclass(frozen=True, slots=True)
class CaptureSequence:
    """All monitor images and the optional contact sheet for one tick."""

    sequence_id: str
    captured_at: datetime
    monitors: tuple[MonitorCapture, ...]
    contact_sheet: EncodedImage | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def monitor_captures(self) -> tuple[MonitorCapture, ...]:
        return self.monitors

    @property
    def contact_sheet_jpeg(self) -> bytes | None:
        if self.contact_sheet is None:
            return None
        return self.contact_sheet.jpeg_bytes

    @property
    def contact_sheet_sha256(self) -> str | None:
        if self.contact_sheet is None:
            return None
        return self.contact_sheet.sha256
