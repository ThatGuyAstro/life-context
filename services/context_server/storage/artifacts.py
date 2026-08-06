"""Atomic, content-addressed artifact storage backed by SQLite metadata."""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, TYPE_CHECKING, Any

from .models import (
    ArtifactRecord,
    ArtifactRecoveryResult,
    RetentionTier,
    utc_now,
)
from .paths import content_addressed_path, digest_file, normalize_digest

if TYPE_CHECKING:
    from .database import SQLiteStorage


class ArtifactNotFound(FileNotFoundError):
    """Raised when metadata or content for an artifact is absent."""


class ArtifactCorruption(RuntimeError):
    """Raised when a content-addressed file does not hash to its address."""


class ArtifactReferenced(RuntimeError):
    """Raised when deletion would leave a capture/observation/job dangling."""


class ArtifactStore:
    """Store immutable bytes under SHA-256-derived paths.

    A put writes to ``.staging`` and atomically renames into the final path
    before registering the metadata row.  Repeating a put for the same bytes
    is therefore safe and returns the existing digest/row.
    """

    def __init__(self, storage: "SQLiteStorage", root: str | os.PathLike[str]):
        self.storage = storage
        self.root = Path(root).expanduser().resolve()
        self.staging_root = self.root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return content_addressed_path(self.root, normalize_digest(digest))

    def _write_stage(
        self,
        data: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO | Iterable[bytes],
    ) -> tuple[Path, str, int]:
        stage = self.staging_root / f"{uuid.uuid4().hex}.part"
        hasher = __import__("hashlib").sha256()
        size = 0
        try:
            with stage.open("xb") as output:
                for chunk in self._chunks(data):
                    hasher.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            stage.unlink(missing_ok=True)
            raise
        return stage, hasher.hexdigest(), size

    @staticmethod
    def _chunks(
        data: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO | Iterable[bytes],
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        if isinstance(data, str):
            yield data.encode("utf-8")
            return
        if isinstance(data, (bytes, bytearray, memoryview)):
            yield bytes(data)
            return
        if isinstance(data, (Path, os.PathLike)):
            with Path(data).open("rb") as source:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        return
                    yield chunk
        elif hasattr(data, "read"):
            stream = data  # type: ignore[assignment]
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    return
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("artifact stream must return bytes")
                yield bytes(chunk)
        else:
            for chunk in data:  # type: ignore[union-attr]
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("artifact chunks must be bytes-like")
                yield bytes(chunk)

    def put(
        self,
        data: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO | Iterable[bytes],
        *,
        media_type: str = "application/octet-stream",
        retention_tier: RetentionTier | str = RetentionTier.DERIVED,
        metadata: dict[str, Any] | None = None,
        created_at=None,
        expires_at=None,
        expected_digest: str | None = None,
    ) -> ArtifactRecord:
        """Write/register an artifact idempotently."""

        stage, digest, size = self._write_stage(data)
        try:
            if expected_digest is not None and normalize_digest(expected_digest) != digest:
                raise ArtifactCorruption(
                    f"provided digest does not match content: expected {expected_digest}, got {digest}"
                )
            final_path = self.path_for(digest)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                if final_path.is_symlink() or not final_path.is_file():
                    raise ArtifactCorruption(f"artifact path is not a regular file: {final_path}")
                existing_digest, existing_size = digest_file(final_path)
                if existing_digest != digest or existing_size != size:
                    raise ArtifactCorruption(f"content-addressed path is corrupt: {final_path}")
                stage.unlink(missing_ok=True)
            else:
                os.replace(stage, final_path)
                self._fsync_directory(final_path.parent)
            relative_path = str(final_path.relative_to(self.root))
            return self.storage.register_artifact(
                digest=digest,
                size_bytes=size,
                media_type=media_type,
                relative_path=relative_path,
                retention_tier=retention_tier,
                metadata=metadata or {},
                created_at=created_at or utc_now(),
                expires_at=expires_at,
            )
        finally:
            stage.unlink(missing_ok=True)

    def put_bytes(self, data: bytes, **kwargs) -> ArtifactRecord:
        return self.put(data, **kwargs)

    def put_file(self, path: str | os.PathLike[str], **kwargs) -> ArtifactRecord:
        return self.put(path, **kwargs)

    def get(self, digest: str) -> ArtifactRecord:
        record = self.storage.get_artifact(digest)
        if record is None:
            raise ArtifactNotFound(normalize_digest(digest))
        path = self.root / record.relative_path
        if not path.exists() or not path.is_file():
            raise ArtifactNotFound(f"artifact content missing for {record.digest}: {path}")
        self.storage.touch_artifact(record.digest)
        return self.storage.get_artifact(record.digest) or record

    def read(self, digest: str) -> bytes:
        record = self.get(digest)
        return (self.root / record.relative_path).read_bytes()

    @contextlib.contextmanager
    def open(self, digest: str, mode: str = "rb") -> Iterator[BinaryIO]:
        if any(flag in mode for flag in ("w", "a", "+", "x")):
            raise ValueError("content-addressed artifacts are immutable; open them read-only")
        record = self.get(digest)
        with (self.root / record.relative_path).open(mode) as stream:
            yield stream

    def exists(self, digest: str) -> bool:
        try:
            record = self.storage.get_artifact(digest)
        except ValueError:
            return False
        return record is not None and (self.root / record.relative_path).is_file()

    def verify(self, digest: str) -> bool:
        record = self.storage.get_artifact(digest)
        if record is None:
            raise ArtifactNotFound(normalize_digest(digest))
        path = self.root / record.relative_path
        if not path.is_file():
            raise ArtifactNotFound(str(path))
        actual, size = digest_file(path)
        if actual != record.digest or size != record.size_bytes:
            raise ArtifactCorruption(f"artifact {record.digest} failed integrity verification")
        return True

    def delete(self, digest: str, *, force: bool = False) -> bool:
        normalized = normalize_digest(digest)
        record = self.storage.get_artifact(normalized)
        if record is None:
            return False
        if not force and self.storage.artifact_reference_count(normalized) > 0:
            raise ArtifactReferenced(normalized)
        self.storage.delete_artifact_record(normalized, force=force)
        path = self.root / record.relative_path
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The metadata deletion is already committed.  Recovery will
            # report/clean the remaining file without resurrecting metadata.
            raise
        return True

    def recover(
        self,
        *,
        remove_orphans: bool = False,
        remove_staging: bool = True,
        verify_hashes: bool = True,
    ) -> ArtifactRecoveryResult:
        """Reconcile filesystem state after interrupted writes/purges."""

        removed_staging = 0
        if remove_staging and self.staging_root.exists():
            for item in self.staging_root.iterdir():
                if item.is_file() or item.is_symlink():
                    item.unlink(missing_ok=True)
                    removed_staging += 1

        records = self.storage.list_artifacts()
        record_paths = {Path(record.relative_path).as_posix(): record for record in records}
        missing: list[str] = []
        corrupt: list[str] = []
        for record in records:
            path = self.root / record.relative_path
            if not path.is_file():
                missing.append(record.digest)
                continue
            if verify_hashes:
                actual, size = digest_file(path)
                if actual != record.digest or size != record.size_bytes:
                    corrupt.append(record.digest)

        orphan_files: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or self.staging_root in path.parents:
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative not in record_paths:
                orphan_files.append(relative)
                if remove_orphans:
                    path.unlink(missing_ok=True)

        return ArtifactRecoveryResult(
            removed_staging_files=removed_staging,
            missing_records=tuple(sorted(missing)),
            corrupt_records=tuple(sorted(corrupt)),
            orphan_files=tuple(sorted(orphan_files)),
            removed_orphan_files=len(orphan_files) if remove_orphans else 0,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Best-effort directory durability; unsupported Windows handles are OK."""

        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


__all__ = [
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactReferenced",
    "ArtifactStore",
]
