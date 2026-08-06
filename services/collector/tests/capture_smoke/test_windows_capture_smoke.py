"""Opt-in smoke coverage for the real Windows screen-capture path."""

from __future__ import annotations

import os
from hashlib import sha256
from io import BytesIO

import pytest

from services.collector.capture import (
    CaptureBackendError,
    CaptureUnavailableError,
    ImageDependencyError,
    NoMonitorsError,
    ScreenCaptureService,
)


def _assert_jpeg(image, *, expected_sha256: str | None = None) -> None:
    """Check both the encoded bytes and the digest reported by the service."""

    assert image.mime_type == "image/jpeg"
    assert image.jpeg_bytes.startswith(b"\xff\xd8")
    assert image.jpeg_bytes.endswith(b"\xff\xd9")
    actual_sha256 = sha256(image.jpeg_bytes).hexdigest()
    assert image.sha256 == actual_sha256
    if expected_sha256 is not None:
        assert expected_sha256 == actual_sha256

    # Re-open the real output so a JPEG-looking byte prefix cannot satisfy the
    # smoke test without being a decodable image of the advertised dimensions.
    from PIL import Image

    with Image.open(BytesIO(image.jpeg_bytes)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (image.width, image.height)


@pytest.mark.skipif(os.name != "nt", reason="Windows capture smoke test requires Windows")
def test_real_screen_capture_service_captures_all_discovered_monitors() -> None:
    """Capture the current desktop when a usable display backend is present."""

    try:
        service = ScreenCaptureService()
    except (CaptureUnavailableError, ImageDependencyError) as exc:
        pytest.skip(f"no display backend available: {exc}")

    try:
        try:
            discovered = tuple(service.backend.enumerate_monitors())
        except (CaptureUnavailableError, CaptureBackendError) as exc:
            pytest.skip(f"no display backend available: {exc}")
        if not discovered:
            pytest.skip("no physical display monitors were discovered")

        try:
            sequence = service.capture_once()
        except (CaptureUnavailableError, ImageDependencyError, NoMonitorsError) as exc:
            pytest.skip(f"no display backend available: {exc}")
    finally:
        service.close()

    backend = service.backend_name
    captured_ids = [capture.monitor.monitor_id for capture in sequence.monitors]
    discovered_ids = [monitor.monitor_id for monitor in discovered]

    print(f"capture_smoke backend={backend} monitor_count={len(discovered_ids)}")

    assert captured_ids == discovered_ids
    assert len(sequence.monitors) == len(discovered)
    assert sequence.metadata["backend"] == backend
    assert sequence.metadata["monitor_count"] == len(discovered)
    assert sequence.metadata["captured_at"] == sequence.captured_at.isoformat()
    assert sequence.contact_sheet is not None

    for capture in sequence.monitors:
        assert capture.sequence_id == sequence.sequence_id
        assert capture.captured_at == sequence.captured_at
        assert capture.metadata["sequence_id"] == sequence.sequence_id
        assert capture.metadata["captured_at"] == sequence.captured_at.isoformat()
        assert capture.metadata["backend"] == backend
        assert capture.metadata["monitor_id"] == capture.monitor.monitor_id
        _assert_jpeg(capture.image, expected_sha256=capture.metadata["sha256"])

    _assert_jpeg(
        sequence.contact_sheet,
        expected_sha256=sequence.metadata["contact_sheet_sha256"],
    )
    artifact_hashes = [capture.sha256 for capture in sequence.monitors]
    artifact_hashes.append(sequence.contact_sheet.sha256)
    expected_sequence_sha256 = sha256("\n".join(artifact_hashes).encode("ascii")).hexdigest()
    assert sequence.metadata["sha256"] == expected_sequence_sha256
