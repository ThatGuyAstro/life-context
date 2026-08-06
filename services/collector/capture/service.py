"""Capture orchestration, interval scheduling, metadata, and sequence IDs."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from hashlib import sha256
from threading import Event
from typing import Any

from .backends import CaptureBackend, create_default_backend
from .images import encode_jpeg, generate_contact_sheet
from .models import CaptureSequence, MonitorCapture


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_sequence_id() -> str:
    return f"capture-{uuid.uuid4().hex}"


class NoMonitorsError(RuntimeError):
    """Raised when a capture tick finds no physical monitors."""


class CaptureConfig:
    """Runtime settings for one capture service."""

    def __init__(
        self,
        *,
        interval_seconds: float = 5.0,
        jpeg_quality: int = 85,
        include_contact_sheet: bool = True,
        contact_sheet_columns: int | None = None,
        contact_sheet_max_tile_size: tuple[int, int] = (640, 360),
        contact_sheet_padding: int = 12,
        contact_sheet_label_height: int = 24,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if not isinstance(jpeg_quality, int) or not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be an integer from 1 through 100")
        if contact_sheet_columns is not None and contact_sheet_columns <= 0:
            raise ValueError("contact_sheet_columns must be positive")
        if len(contact_sheet_max_tile_size) != 2 or any(
            not isinstance(value, int) or value <= 0
            for value in contact_sheet_max_tile_size
        ):
            raise ValueError("contact_sheet_max_tile_size must be two positive integers")
        if contact_sheet_padding < 0 or contact_sheet_label_height < 0:
            raise ValueError("contact-sheet spacing must not be negative")

        self.interval_seconds = float(interval_seconds)
        self.jpeg_quality = jpeg_quality
        self.include_contact_sheet = bool(include_contact_sheet)
        self.contact_sheet_columns = contact_sheet_columns
        self.contact_sheet_max_tile_size = contact_sheet_max_tile_size
        self.contact_sheet_padding = contact_sheet_padding
        self.contact_sheet_label_height = contact_sheet_label_height


class ScreenCaptureService:
    """Capture every physical monitor at a configured interval.

    The service is deliberately in-memory: callers can write returned JPEGs
    to their desired store, enqueue metadata, or discard them.  No audio or
    video streams are opened or represented by this API.
    """

    def __init__(
        self,
        backend: CaptureBackend | None = None,
        *,
        config: CaptureConfig | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        sequence_id_factory: Callable[[], str] = _new_sequence_id,
    ) -> None:
        self.backend = backend or create_default_backend()
        self.config = config or CaptureConfig()
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._sequence_id_factory = sequence_id_factory

    def capture_once(self) -> CaptureSequence:
        """Enumerate and capture every monitor once."""

        sequence_id = self._sequence_id_factory()
        captured_at = self._now()
        monitors = tuple(self.backend.enumerate_monitors())
        if not monitors:
            raise NoMonitorsError("screen capture found no physical monitors")

        captures: list[MonitorCapture] = []
        for monitor in monitors:
            raw_frame = self.backend.capture(monitor)
            encoded = encode_jpeg(raw_frame, quality=self.config.jpeg_quality)
            metadata: dict[str, Any] = {
                **dict(raw_frame.metadata),
                **monitor.as_metadata(),
                "sequence_id": sequence_id,
                "captured_at": captured_at.isoformat(),
                "backend": self.backend_name,
                "pixel_format": raw_frame.pixel_format,
                "width": encoded.width,
                "height": encoded.height,
                "mime_type": encoded.mime_type,
                "jpeg_size_bytes": len(encoded.jpeg_bytes),
                "sha256": encoded.sha256,
            }
            if raw_frame.captured_at is not None:
                metadata["frame_captured_at"] = raw_frame.captured_at.isoformat()
            captures.append(
                MonitorCapture(
                    sequence_id=sequence_id,
                    monitor=monitor,
                    captured_at=captured_at,
                    image=encoded,
                    metadata=metadata,
                )
            )

        contact_sheet = None
        if self.config.include_contact_sheet:
            contact_sheet = generate_contact_sheet(
                [
                    (f"{capture.monitor.name} ({capture.monitor.monitor_id})", capture.image)
                    for capture in captures
                ],
                quality=self.config.jpeg_quality,
                columns=self.config.contact_sheet_columns,
                max_tile_size=self.config.contact_sheet_max_tile_size,
                padding=self.config.contact_sheet_padding,
                label_height=self.config.contact_sheet_label_height,
            )

        hashes = [capture.sha256 for capture in captures]
        if contact_sheet is not None:
            hashes.append(contact_sheet.sha256)
        sequence_hash = sha256("\n".join(hashes).encode("ascii")).hexdigest()
        metadata = {
            "sequence_id": sequence_id,
            "captured_at": captured_at.isoformat(),
            "backend": self.backend_name,
            "monitor_count": len(captures),
            "sha256": sequence_hash,
            "contact_sheet_sha256": contact_sheet.sha256 if contact_sheet else None,
            "audio": False,
            "video": False,
        }
        return CaptureSequence(
            sequence_id=sequence_id,
            captured_at=captured_at,
            monitors=tuple(captures),
            contact_sheet=contact_sheet,
            metadata=metadata,
        )

    @property
    def backend_name(self) -> str:
        return str(getattr(self.backend, "name", type(self.backend).__name__))

    def iter_sequences(
        self,
        *,
        stop_event: Event | None = None,
        max_sequences: int | None = None,
    ) -> Iterator[CaptureSequence]:
        """Yield captures immediately, then at each configured interval."""

        if max_sequences is not None and max_sequences < 1:
            return

        emitted = 0
        next_due = self._monotonic()
        while stop_event is None or not stop_event.is_set():
            if max_sequences is not None and emitted >= max_sequences:
                return

            sequence = self.capture_once()
            emitted += 1
            yield sequence

            if max_sequences is not None and emitted >= max_sequences:
                return
            next_due += self.config.interval_seconds
            delay = next_due - self._monotonic()
            if delay > 0:
                self._sleep(delay)
            else:
                # A slow capture should not cause an unbounded burst of
                # catch-up captures or accumulate scheduler drift.
                next_due = self._monotonic()

    def run(
        self,
        on_capture: Callable[[CaptureSequence], None] | None = None,
        *,
        stop_event: Event | None = None,
        max_sequences: int | None = None,
    ) -> int:
        """Run the interval loop and return the number of emitted sequences."""

        emitted = 0
        for sequence in self.iter_sequences(
            stop_event=stop_event, max_sequences=max_sequences
        ):
            if on_capture is not None:
                on_capture(sequence)
            emitted += 1
        return emitted

    # Explicit alias for callers who prefer the operation's full name.
    capture_at_interval = run

    def close(self) -> None:
        self.backend.close()

    def __enter__(self) -> "ScreenCaptureService":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
