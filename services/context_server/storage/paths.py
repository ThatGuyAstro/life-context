"""Content-addressed artifact path and hashing helpers."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import BinaryIO, Iterable


SHA256_LENGTH = 64
_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


class InvalidDigest(ValueError):
    """Raised when a content address is not a SHA-256 digest."""


def normalize_digest(digest: str) -> str:
    value = str(digest).strip()
    if value.lower().startswith("sha256:"):
        value = value[7:]
    if not _HEX_DIGEST.fullmatch(value):
        raise InvalidDigest("expected a 64-character SHA-256 hexadecimal digest")
    return value.lower()


def content_addressed_path(
    root_or_digest: str | os.PathLike[str],
    digest_or_root: str | os.PathLike[str],
) -> Path:
    """Return ``root/<first2>/<next2>/<sha256>``.

    The documented call form is ``content_addressed_path(root, digest)``.
    The small argument-order compatibility check also accepts
    ``content_addressed_path(digest, root)`` because both values are commonly
    supplied by ingestion code as strings.
    """

    first = str(root_or_digest)
    second = str(digest_or_root)
    if _HEX_DIGEST.fullmatch(first) and not _HEX_DIGEST.fullmatch(second):
        first, second = second, first
    digest = normalize_digest(second)
    root = Path(first)
    return root / digest[:2] / digest[2:4] / digest


def relative_content_addressed_path(digest: str) -> Path:
    normalized = normalize_digest(digest)
    return Path(normalized[:2]) / normalized[2:4] / normalized


def digest_bytes(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def digest_stream(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size


def digest_file(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    with Path(path).open("rb") as stream:
        return digest_stream(stream, chunk_size=chunk_size)


def digest_chunks(chunks: Iterable[bytes], chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    del chunk_size  # Kept for API symmetry with digest_stream.
    hasher = hashlib.sha256()
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("artifact chunks must be bytes-like")
        value = bytes(chunk)
        hasher.update(value)
        size += len(value)
    return hasher.hexdigest(), size


# A descriptive alias used by callers that prefer the artifact terminology.
artifact_path = content_addressed_path

