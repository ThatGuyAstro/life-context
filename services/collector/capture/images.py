"""JPEG encoding, hashing, and contact-sheet generation.

Pillow is loaded lazily so importing the portable capture interface does not
require an image library until a backend result is encoded.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from io import BytesIO
from math import ceil, sqrt

from .models import EncodedImage, RawFrame


class ImageDependencyError(RuntimeError):
    """Raised when Pillow is needed but is not installed."""


def _pillow():
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImageDependencyError(
            "Pillow is required for JPEG encoding and contact sheets"
        ) from exc
    return Image, ImageDraw


def _validate_quality(quality: int) -> None:
    if not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ValueError("JPEG quality must be an integer from 1 through 100")


def _image_from_frame(frame: RawFrame):
    Image, _ = _pillow()
    size = (frame.width, frame.height)
    if frame.pixel_format == "BGR":
        return Image.frombytes("RGB", size, frame.pixel_bytes, "raw", "BGR")
    if frame.pixel_format == "BGRA":
        return Image.frombytes("RGBA", size, frame.pixel_bytes, "raw", "BGRA").convert(
            "RGB"
        )
    image = Image.frombytes(frame.pixel_format, size, frame.pixel_bytes)
    return image if frame.pixel_format == "RGB" else image.convert("RGB")


def encode_jpeg(frame: RawFrame, *, quality: int = 85) -> EncodedImage:
    """Encode a raw RGB/BGR/RGBA/BGRA frame as a JPEG and hash its bytes."""

    _validate_quality(quality)
    image = _image_from_frame(frame)
    output = BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    jpeg_bytes = output.getvalue()
    return EncodedImage(
        jpeg_bytes=jpeg_bytes,
        width=frame.width,
        height=frame.height,
        sha256=sha256(jpeg_bytes).hexdigest(),
    )


def _load_jpeg(encoded: EncodedImage):
    Image, _ = _pillow()
    image = Image.open(BytesIO(encoded.jpeg_bytes))
    image.load()
    return image.convert("RGB")


def generate_contact_sheet(
    items: Sequence[tuple[str, EncodedImage]],
    *,
    quality: int = 85,
    columns: int | None = None,
    max_tile_size: tuple[int, int] = (640, 360),
    padding: int = 12,
    label_height: int = 24,
) -> EncodedImage:
    """Build a labeled JPEG contact sheet from monitor JPEGs.

    Each item is ``(label, encoded_image)``.  Images are reduced to fit the
    configured tile size but are never enlarged, so a multi-monitor desktop
    cannot accidentally create an unbounded contact sheet.
    """

    _validate_quality(quality)
    if not items:
        raise ValueError("at least one image is required for a contact sheet")
    if columns is not None and (not isinstance(columns, int) or columns <= 0):
        raise ValueError("contact-sheet columns must be a positive integer")
    if len(max_tile_size) != 2 or any(
        not isinstance(value, int) or value <= 0 for value in max_tile_size
    ):
        raise ValueError("max_tile_size must contain two positive integers")
    if padding < 0 or label_height < 0:
        raise ValueError("padding and label_height must not be negative")

    Image, ImageDraw = _pillow()
    loaded = [(label, _load_jpeg(image)) for label, image in items]
    max_width, max_height = max_tile_size
    tile_width = min(max_width, max(image.width for _, image in loaded))
    tile_height = min(max_height, max(image.height for _, image in loaded))

    count = len(loaded)
    column_count = columns or max(1, ceil(sqrt(count)))
    column_count = min(column_count, count)
    row_count = ceil(count / column_count)

    sheet_width = padding + column_count * (tile_width + padding)
    cell_height = label_height + tile_height
    sheet_height = padding + row_count * (cell_height + padding)
    sheet = Image.new("RGB", (sheet_width, sheet_height), (30, 34, 42))
    draw = ImageDraw.Draw(sheet)

    for index, (label, image) in enumerate(loaded):
        column = index % column_count
        row = index // column_count
        cell_left = padding + column * (tile_width + padding)
        cell_top = padding + row * (cell_height + padding)
        draw.rectangle(
            (
                cell_left,
                cell_top,
                cell_left + tile_width,
                cell_top + cell_height,
            ),
            fill=(12, 15, 20),
        )
        draw.text((cell_left + 6, cell_top + 4), label, fill=(235, 239, 245))

        thumbnail = image.copy()
        thumbnail.thumbnail((tile_width, tile_height), Image.Resampling.LANCZOS)
        image_left = cell_left + (tile_width - thumbnail.width) // 2
        image_top = cell_top + label_height + (tile_height - thumbnail.height) // 2
        sheet.paste(thumbnail, (image_left, image_top))

    output = BytesIO()
    sheet.save(output, format="JPEG", quality=quality, optimize=True)
    jpeg_bytes = output.getvalue()
    return EncodedImage(
        jpeg_bytes=jpeg_bytes,
        width=sheet.width,
        height=sheet.height,
        sha256=sha256(jpeg_bytes).hexdigest(),
    )
