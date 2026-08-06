"""Atomic, content-addressed local persistence for collector work."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Mapping

from .errors import SpoolCorruptionError, SpoolError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SpoolError(f"value is not JSON serializable: {exc}") from exc


def _digest(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True, slots=True)
class SpoolBatch:
    """A batch currently visible in the durable spool."""

    batch_id: str
    payload: Mapping[str, Any]
    path: Path
    state: str
    created_at: str

    @property
    def records(self) -> list[Mapping[str, Any]]:
        for field_name in ("records", "events", "items"):
            records = self.payload.get(field_name)
            if isinstance(records, list):
                return records
        return []


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """A SHA-256-addressed artifact held by the local spool."""

    sha256: str
    path: Path
    size: int
    metadata: Mapping[str, Any]
    deduplicated: bool = False


class LocalSpool:
    """Durable local spool with crash-safe batch claims and artifact storage.

    Files are first written to a temporary file in the destination directory,
    flushed (including ``fsync`` by default), and atomically renamed into
    place.  A batch is acknowledged only after a caller has received a
    successful server response.  Processing files are returned to pending on
    the next process start, so a crash cannot silently lose queued work.
    """

    schema_version = 1

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        fsync: bool = True,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.root = Path(root).expanduser()
        self.fsync = fsync
        self._clock = clock

        self.batches_dir = self.root / "batches"
        self.pending_dir = self.batches_dir / "pending"
        self.processing_dir = self.batches_dir / "processing"
        self.artifacts_dir = self.root / "artifacts"
        self.artifact_blobs_dir = self.artifacts_dir / "blobs"
        self.artifact_metadata_dir = self.artifacts_dir / "metadata"
        self.artifact_uploaded_dir = self.artifacts_dir / "uploaded"
        self.tmp_dir = self.root / "tmp"

        for directory in (
            self.pending_dir,
            self.processing_dir,
            self.artifact_blobs_dir,
            self.artifact_metadata_dir,
            self.artifact_uploaded_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._recover_processing()

    def _recover_processing(self) -> None:
        """Make claimed batches replayable after an interrupted process."""

        for path in sorted(self.processing_dir.glob("*.json")):
            destination = self.pending_dir / path.name
            try:
                os.replace(path, destination)
            except FileNotFoundError:
                continue

    def _fsync_directory(self, directory: Path) -> None:
        if not self.fsync or os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                if self.fsync:
                    os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self._fsync_directory(destination.parent)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _atomic_json(self, destination: Path, value: object) -> None:
        self._atomic_write(destination, _canonical_json(value))

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise SpoolCorruptionError(f"cannot read spool item {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise SpoolCorruptionError(f"spool item {path} must contain a JSON object")
        return value

    def _batch_from_path(self, path: Path, state: str) -> SpoolBatch:
        envelope = self._read_json(path)
        if envelope.get("schema_version") != self.schema_version:
            raise SpoolCorruptionError(f"unsupported batch schema in {path}")
        batch_id = envelope.get("batch_id")
        payload = envelope.get("payload")
        created_at = envelope.get("created_at")
        if not isinstance(batch_id, str) or not batch_id:
            raise SpoolCorruptionError(f"batch {path} has no valid batch_id")
        if not isinstance(payload, dict):
            raise SpoolCorruptionError(f"batch {path} has no JSON object payload")
        if not isinstance(created_at, str):
            created_at = ""
        return SpoolBatch(
            batch_id=batch_id,
            payload=payload,
            path=path,
            state=state,
            created_at=created_at,
        )

    def enqueue_batch(
        self,
        records: Iterable[Mapping[str, Any]] | Mapping[str, Any],
        *,
        batch_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> SpoolBatch:
        """Persist a generic record batch and return its stable spool ID.

        The default JSON contract is ``{"records": [<dict>, ...]}``.  A
        mapping input is treated as an already-formed generic contract
        envelope and preserved as-is; callers that want one record should
        pass a one-element iterable.  No capture-specific type is required or
        imported.
        """

        if batch_id is None:
            batch_id = uuid.uuid4().hex
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("batch_id must be a non-empty string")
        if payload is None:
            if isinstance(records, Mapping):
                batch_payload: Mapping[str, Any] = dict(records)
            else:
                record_list = []
                for record in records:
                    if not isinstance(record, Mapping):
                        raise TypeError("every batch record must be a mapping")
                    record_list.append(dict(record))
                batch_payload = {"records": record_list}
        else:
            if not isinstance(payload, Mapping):
                raise TypeError("payload must be a mapping")
            batch_payload = dict(payload)

        envelope = {
            "schema_version": self.schema_version,
            "batch_id": batch_id,
            "created_at": self._clock(),
            "payload": batch_payload,
        }
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{timestamp}-{uuid.uuid4().hex}.json"
        destination = self.pending_dir / filename
        self._atomic_json(destination, envelope)
        return self._batch_from_path(destination, "pending")

    # A descriptive alias for callers that use "queue" terminology.
    queue_batch = enqueue_batch
    enqueue = enqueue_batch

    def list_pending_batches(self, limit: int | None = None) -> list[SpoolBatch]:
        paths = sorted(self.pending_dir.glob("*.json"))
        if limit is not None:
            if limit < 0:
                raise ValueError("limit cannot be negative")
            paths = paths[:limit]
        return [self._batch_from_path(path, "pending") for path in paths]

    def claim_pending_batches(self, limit: int | None = None) -> list[SpoolBatch]:
        """Atomically claim pending batches for one replay worker."""

        claimed: list[SpoolBatch] = []
        for path in sorted(self.pending_dir.glob("*.json")):
            if limit is not None and len(claimed) >= limit:
                break
            destination = self.processing_dir / path.name
            try:
                os.replace(path, destination)
            except FileNotFoundError:
                # Another worker claimed it between glob and replace.
                continue
            try:
                claimed.append(self._batch_from_path(destination, "processing"))
            except BaseException:
                # A corrupt item should remain inspectable and retryable.
                try:
                    os.replace(destination, path)
                except OSError:
                    pass
                raise
        return claimed

    def claim_batch(self) -> SpoolBatch | None:
        claimed = self.claim_pending_batches(limit=1)
        return claimed[0] if claimed else None

    def _checked_batch_path(self, batch: SpoolBatch | Path) -> Path:
        path = batch.path if isinstance(batch, SpoolBatch) else Path(batch)
        resolved = path.resolve()
        allowed = (self.pending_dir.resolve(), self.processing_dir.resolve())
        if not any(resolved.parent == directory for directory in allowed):
            raise SpoolError(f"batch path is outside the spool: {path}")
        return path

    def acknowledge_batch(self, batch: SpoolBatch | Path) -> None:
        """Delete a successfully delivered batch from the spool."""

        path = self._checked_batch_path(batch)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(path.parent)

    # Common delivery vocabulary aliases.
    ack_batch = acknowledge_batch

    def release_batch(self, batch: SpoolBatch | Path) -> None:
        """Return a failed processing claim to the pending queue."""

        path = self._checked_batch_path(batch)
        if path.parent == self.pending_dir:
            return
        destination = self.pending_dir / path.name
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            return
        self._fsync_directory(self.pending_dir)

    def _artifact_path(self, sha256: str) -> Path:
        return self.artifact_blobs_dir / _digest(sha256)

    def _artifact_metadata_path(self, sha256: str) -> Path:
        return self.artifact_metadata_dir / f"{_digest(sha256)}.json"

    def _artifact_uploaded_path(self, sha256: str) -> Path:
        return self.artifact_uploaded_dir / f"{_digest(sha256)}.json"

    def _copy_artifact_source(self, source: object, destination: BinaryIO) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0

        def write_chunks(stream: BinaryIO) -> None:
            nonlocal size
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("artifact streams must yield bytes")
                bytes_chunk = bytes(chunk)
                hasher.update(bytes_chunk)
                destination.write(bytes_chunk)
                size += len(bytes_chunk)

        if isinstance(source, bytes):
            hasher.update(source)
            destination.write(source)
            size = len(source)
        elif isinstance(source, (bytearray, memoryview)):
            bytes_source = bytes(source)
            hasher.update(bytes_source)
            destination.write(bytes_source)
            size = len(bytes_source)
        elif isinstance(source, Mapping):
            encoded = _canonical_json(source)
            hasher.update(encoded)
            destination.write(encoded)
            size = len(encoded)
        elif isinstance(source, (str, os.PathLike)):
            with Path(source).open("rb") as stream:
                write_chunks(stream)
        elif hasattr(source, "read"):
            write_chunks(source)  # type: ignore[arg-type]
        else:
            raise TypeError("artifact source must be bytes, a path, a mapping, or a binary stream")
        return hasher.hexdigest(), size

    def put_artifact(
        self,
        source: object,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoredArtifact:
        """Store bytes once under their SHA-256 digest.

        Re-submitting identical bytes never overwrites the existing blob and
        returns ``deduplicated=True``.  Metadata from the first submission is
        retained, so a replay cannot mutate the identity of an artifact.
        """

        artifact_metadata = dict(metadata or {})
        metadata_payload = {
            "sha256": "pending",
            "size": 0,
            "metadata": artifact_metadata,
        }
        # Validate metadata before creating a blob that could outlive an error.
        _canonical_json(metadata_payload)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact-",
            suffix=".part",
            dir=str(self.tmp_dir),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                sha256, size = self._copy_artifact_source(source, stream)
                stream.flush()
                if self.fsync:
                    os.fsync(stream.fileno())
            destination = self._artifact_path(sha256)
            if destination.exists():
                temporary.unlink(missing_ok=True)
                existing = self._load_artifact_metadata(sha256)
                return StoredArtifact(
                    sha256=sha256,
                    path=destination,
                    size=destination.stat().st_size,
                    metadata=existing,
                    deduplicated=True,
                )

            os.replace(temporary, destination)
            self._fsync_directory(destination.parent)
            self._atomic_json(
                self._artifact_metadata_path(sha256),
                {"sha256": sha256, "size": size, "metadata": artifact_metadata},
            )
            return StoredArtifact(
                sha256=sha256,
                path=destination,
                size=size,
                metadata=artifact_metadata,
                deduplicated=False,
            )
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    # Artifact terminology used by callers can be either "put" or "store".
    store_artifact = put_artifact

    def _load_artifact_metadata(self, sha256: str) -> Mapping[str, Any]:
        path = self._artifact_metadata_path(sha256)
        if not path.exists():
            return {}
        envelope = self._read_json(path)
        metadata = envelope.get("metadata", {})
        return metadata if isinstance(metadata, dict) else {}

    def artifact(self, sha256: str) -> StoredArtifact:
        digest = _digest(sha256)
        path = self._artifact_path(digest)
        if not path.is_file():
            raise FileNotFoundError(path)
        return StoredArtifact(
            sha256=digest,
            path=path,
            size=path.stat().st_size,
            metadata=self._load_artifact_metadata(digest),
            deduplicated=False,
        )

    def is_artifact_uploaded(self, sha256: str) -> bool:
        return self._artifact_uploaded_path(sha256).is_file()

    def mark_artifact_uploaded(self, sha256: str, response: object = None) -> None:
        digest = _digest(sha256)
        if not self._artifact_path(digest).is_file():
            raise FileNotFoundError(self._artifact_path(digest))
        self._atomic_json(
            self._artifact_uploaded_path(digest),
            {"sha256": digest, "uploaded_at": self._clock(), "response": response},
        )

    def list_pending_artifacts(self) -> list[StoredArtifact]:
        artifacts: list[StoredArtifact] = []
        for path in sorted(self.artifact_blobs_dir.iterdir()):
            if (
                not path.is_file()
                or not _SHA256.fullmatch(path.name)
                or self.is_artifact_uploaded(path.name)
            ):
                continue
            artifacts.append(self.artifact(path.name))
        return artifacts

    def list_uploaded_artifacts(self) -> list[str]:
        return sorted(
            path.stem
            for path in self.artifact_uploaded_dir.glob("*.json")
            if _SHA256.fullmatch(path.stem)
        )

    pending_batches = list_pending_batches
    pending_artifacts = list_pending_artifacts

    def _regular_file_bytes(self, directory: Path) -> int:
        total = 0
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def status(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "pending_batches": len(list(self.pending_dir.glob("*.json"))),
            "processing_batches": len(list(self.processing_dir.glob("*.json"))),
            "pending_artifacts": len(self.list_pending_artifacts()),
            "uploaded_artifacts": len(self.list_uploaded_artifacts()),
            "spool_bytes": self._regular_file_bytes(self.root),
        }


Spool = LocalSpool
