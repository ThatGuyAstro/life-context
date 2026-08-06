"""Scoped filesystem and Git repository discovery for the collector.

The source is deliberately local-first and metadata-only.  It watches only
the roots supplied by the caller, never follows directory symlinks, and does
not put file contents on an event.  A SHA-256 digest may be computed as a
change fingerprint; the digest is the only file-derived value retained.

``watchdog`` is optional.  When it is not installed (or cannot watch a
configured root), :class:`FilesystemSource` uses a bounded polling loop.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterable, Iterator, Literal, Sequence

try:  # pragma: no cover - exercised when watchdog is installed
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - the fallback is covered directly
    WATCHDOG_AVAILABLE = False
    Observer = None  # type: ignore[assignment]

    class FileSystemEventHandler:  # type: ignore[no-redef]
        """Small import-time fallback so the handler remains testable."""

        pass


PathLike = str | os.PathLike[str]
EventType = Literal["created", "modified", "deleted", "moved"]

# These directories generate a great deal of build/cache noise and can hold
# generated or third-party code.  ``.git`` is inspected explicitly by Git
# discovery, but its internal files are never watched as source files.
DEFAULT_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
    }
)

# Do not emit events for common credential/private-key files.  The filter is
# name-based by design so it works for files that have just been deleted.
DEFAULT_IGNORED_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
    }
)
DEFAULT_IGNORED_FILE_PATTERNS = (
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.secret",
)


def _path(value: PathLike | Path) -> Path:
    """Convert a user path without interpreting it as a glob."""

    if isinstance(value, Path):
        return value
    return Path(os.path.expandvars(os.path.expanduser(os.fspath(value))))


def normalize_path(value: PathLike | Path) -> Path:
    """Return an absolute, normalized path without requiring it to exist."""

    return _path(value).resolve(strict=False)


def _absolute_without_resolving(value: PathLike | Path) -> Path:
    """Return an absolute lexical path while preserving symlink components."""

    return Path(os.path.abspath(os.fspath(_path(value))))


def _root_values(roots: PathLike | Path | Iterable[PathLike | Path]) -> list[PathLike | Path]:
    if isinstance(roots, (str, os.PathLike, Path)):
        return [roots]
    return list(roots)


def _is_filesystem_root(path: Path) -> bool:
    """Return whether ``path`` is a drive/filesystem root.

    A configured directory may be large, but a bare drive root or ``/`` is
    rejected so a caller cannot accidentally request a full-disk watch.
    """

    if path.parent == path:
        return True
    if path.anchor:
        try:
            return path == Path(path.anchor)
        except (OSError, ValueError):
            return False
    return False


def normalize_configured_roots(
    roots: PathLike | Path | Iterable[PathLike | Path],
) -> tuple[Path, ...]:
    """Normalize and validate explicit watch roots.

    Nonexistent roots are retained so polling can notice them if they are
    created later.  Existing roots must be directories, and filesystem roots
    are always rejected.
    """

    values = _root_values(roots)
    if not values:
        raise ValueError("at least one configured filesystem root is required")

    result: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        candidate = normalize_path(value)
        if _is_filesystem_root(candidate):
            raise ValueError(f"refusing to watch filesystem root: {candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"configured watch root is not a directory: {candidate}")
        if candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return tuple(result)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _contains_symlink(path: Path, root: Path | None = None) -> bool:
    """Check existing components without resolving away a symlink first."""

    current = _absolute_without_resolving(path)
    stop = None if root is None else _absolute_without_resolving(root)
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if stop is not None and current == stop:
            return False
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _path_is_excluded(
    path: Path,
    root: Path,
    *,
    exclude_names: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True

    ignored_names = DEFAULT_IGNORED_DIRECTORY_NAMES | DEFAULT_IGNORED_FILE_NAMES | frozenset(
        exclude_names
    )
    ignored_patterns = (*DEFAULT_IGNORED_FILE_PATTERNS, *exclude_patterns)
    parts = relative.parts
    if any(part in DEFAULT_IGNORED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if any(part in ignored_names for part in parts):
        return True
    if parts and any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in ignored_patterns):
        return True
    return False


def is_safe_path(
    path: PathLike | Path,
    roots: PathLike | Path | Iterable[PathLike | Path],
    *,
    allow_directories: bool = True,
    exclude_names: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
) -> bool:
    """Return whether a path can be reported under configured roots.

    The check is lexical *and* resolved: ``..`` traversal, symlink escapes,
    ignored metadata/credential paths, and paths outside every configured
    root are rejected.  A missing path can still be checked, which is needed
    for safe delete events.
    """

    raw_candidate = _absolute_without_resolving(path)
    candidate = normalize_path(raw_candidate)
    normalized_roots = normalize_configured_roots(roots)

    for root in normalized_roots:
        if not _is_relative_to(candidate, root):
            continue
        if candidate == root and not allow_directories:
            continue
        # Check the lexical path before resolving it.  Otherwise a symlink
        # inside the root that points back inside the root would be silently
        # converted to its target and treated as a regular source file.
        if _contains_symlink(raw_candidate):
            continue
        if _path_is_excluded(
            candidate,
            root,
            exclude_names=exclude_names,
            exclude_patterns=exclude_patterns,
        ):
            continue
        return True
    return False


# A descriptive alias is useful to callers that prefer filter terminology.
safe_path_filter = is_safe_path


def sha256_file(path: PathLike | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest without retaining file contents."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    raw_path = _absolute_without_resolving(path)
    if _contains_symlink(raw_path):
        raise OSError(f"refusing to hash a symlinked path: {raw_path}")
    digest = hashlib.sha256()
    with raw_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Metadata associated with a regular file or directory."""

    path: Path
    root: Path
    size: int | None
    mtime_ns: int | None
    sha256: str | None = None
    is_directory: bool = False

    @property
    def mtime(self) -> float | None:
        return None if self.mtime_ns is None else self.mtime_ns / 1_000_000_000

    @property
    def hash(self) -> str | None:
        """Compatibility alias for consumers that call the digest ``hash``."""

        return self.sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "root": str(self.root),
            "size": self.size,
            "mtime": self.mtime,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "is_directory": self.is_directory,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        values = self.to_dict()
        if key == "hash":
            return self.sha256
        return values[key]


def file_metadata(
    path: PathLike | Path,
    *,
    root: PathLike | Path | None = None,
    compute_hash: bool = True,
    max_hash_bytes: int | None = None,
) -> FileMetadata | None:
    """Read size/mtime and, optionally, a digest for one safe regular path.

    No file content is returned or stored.  ``None`` is returned for missing,
    symlink, special, or otherwise unreadable paths.
    """

    if max_hash_bytes is not None and max_hash_bytes < 0:
        raise ValueError("max_hash_bytes cannot be negative")
    raw_candidate = _absolute_without_resolving(path)
    if _contains_symlink(raw_candidate):
        return None
    candidate = normalize_path(raw_candidate)
    normalized_root = normalize_path(root) if root is not None else candidate.parent
    try:
        info = raw_candidate.lstat()
    except (FileNotFoundError, OSError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return None
    is_directory = stat.S_ISDIR(info.st_mode)
    if not (is_directory or stat.S_ISREG(info.st_mode)):
        return None

    digest: str | None = None
    if not is_directory and compute_hash:
        if max_hash_bytes is None or info.st_size <= max_hash_bytes:
            try:
                digest = sha256_file(candidate)
            except (FileNotFoundError, OSError):
                return None

    return FileMetadata(
        path=candidate,
        root=normalized_root,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        sha256=digest,
        is_directory=is_directory,
    )


# A noun-like alias reads naturally in callers and tests.
metadata_for_path = file_metadata


def _canonical_event_type(event_type: str) -> EventType:
    value = event_type.lower().strip()
    aliases = {
        "create": "created",
        "change": "modified",
        "modify": "modified",
        "delete": "deleted",
        "move": "moved",
        "rename": "moved",
    }
    value = aliases.get(value, value)
    if value not in {"created", "modified", "deleted", "moved"}:
        raise ValueError(f"unsupported filesystem event type: {event_type!r}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FileEvent:
    """Normalized, JSON-friendly file event with no content payload."""

    event_type: EventType
    path: Path
    root: Path
    is_directory: bool
    timestamp: float
    size: int | None = None
    mtime_ns: int | None = None
    sha256: str | None = None
    old_path: Path | None = None

    @property
    def type(self) -> EventType:
        return self.event_type

    @property
    def mtime(self) -> float | None:
        return None if self.mtime_ns is None else self.mtime_ns / 1_000_000_000

    @property
    def hash(self) -> str | None:
        return self.sha256

    @property
    def source_path(self) -> Path | None:
        return self.old_path

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "event_type": self.event_type,
            "path": str(self.path),
            "root": str(self.root),
            "is_directory": self.is_directory,
            "timestamp": self.timestamp,
            "size": self.size,
            "mtime": self.mtime,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }
        if self.old_path is not None:
            value["old_path"] = str(self.old_path)
        return value

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        if key == "type":
            return self.event_type
        if key in {"hash", "file_hash"}:
            return self.sha256
        if key == "source_path":
            return self.old_path
        return self.to_dict()[key]


NormalizedFileEvent = FileEvent


def normalize_file_event(
    event_type: str,
    path: PathLike | Path,
    root: PathLike | Path,
    *,
    is_directory: bool = False,
    dest_path: PathLike | Path | None = None,
    timestamp: float | None = None,
    compute_hash: bool = True,
    max_hash_bytes: int | None = None,
    include_directories: bool = True,
    metadata: FileMetadata | None = None,
    exclude_names: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
) -> FileEvent | None:
    """Normalize one raw watcher event, returning ``None`` when unsafe."""

    canonical_type = _canonical_event_type(event_type)
    normalized_root = normalize_path(root)
    source = normalize_path(path)
    if is_directory and not include_directories:
        return None
    if not is_safe_path(
        source,
        (normalized_root,),
        allow_directories=is_directory,
        exclude_names=exclude_names,
        exclude_patterns=exclude_patterns,
    ):
        return None

    destination: Path | None = None
    if canonical_type == "moved":
        if dest_path is None:
            raise ValueError("moved events require dest_path")
        destination = normalize_path(dest_path)
        if not is_safe_path(
            destination,
            (normalized_root,),
            allow_directories=is_directory,
            exclude_names=exclude_names,
            exclude_patterns=exclude_patterns,
        ):
            # A move outside the configured root is safely represented as a
            # deletion of the in-scope source, without exposing the target.
            canonical_type = "deleted"
        else:
            source = destination

    details = metadata
    if details is None and canonical_type != "deleted":
        details = file_metadata(
            source,
            root=normalized_root,
            compute_hash=compute_hash,
            max_hash_bytes=max_hash_bytes,
        )

    return FileEvent(
        event_type=canonical_type,
        path=source,
        root=normalized_root,
        is_directory=is_directory,
        timestamp=time.time() if timestamp is None else timestamp,
        size=None if details is None else details.size,
        mtime_ns=None if details is None else details.mtime_ns,
        sha256=None if details is None else details.sha256,
        old_path=(
            normalize_path(path)
            if canonical_type == "moved" and destination is not None
            else None
        ),
    )


# Shorter spelling for callers that already know the input is a watcher event.
normalize_event = normalize_file_event


def _event_key(event: FileEvent) -> tuple[str, str, str | None]:
    # A move is keyed by its destination so a following modified event for
    # that destination is coalesced into the same quiet period.
    return (os.path.normcase(str(event.root)), os.path.normcase(str(event.path)), None)


def _merge_debounced(previous: FileEvent, current: FileEvent) -> FileEvent | None:
    """Merge a burst while preserving the net filesystem transition."""

    first, second = previous.event_type, current.event_type
    if first == "created" and second == "deleted":
        return None
    if first == "deleted" and second == "created":
        return replace(current, event_type="created")
    if first == "created" and second in {"modified", "moved"}:
        return replace(current, event_type="created")
    if first == "modified" and second == "created":
        return replace(current, event_type="created")
    if first == "moved" and second == "modified":
        return replace(current, event_type="moved", old_path=previous.old_path)
    return current


class Debouncer:
    """Coalesce events for the same path until they are quiet."""

    def __init__(self, delay_seconds: float = 0.25) -> None:
        if delay_seconds < 0:
            raise ValueError("debounce delay must be non-negative")
        self.delay_seconds = float(delay_seconds)
        self._pending: dict[
            tuple[str, str, str | None], tuple[float, FileEvent | None]
        ] = {}
        self._lock = threading.RLock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(event is not None for _, event in self._pending.values())

    def add(self, event: FileEvent, *, now: float | None = None) -> list[FileEvent]:
        current_time = time.monotonic() if now is None else now
        key = _event_key(event)
        with self._lock:
            _, previous = self._pending.get(key, (current_time, None))
            merged = event if previous is None else _merge_debounced(previous, event)
            self._pending[key] = (current_time, merged)
            return self._flush_locked(current_time, force=False)

    push = add

    def _flush_locked(self, current_time: float, *, force: bool) -> list[FileEvent]:
        ready: list[FileEvent] = []
        for key, (last_seen, event) in list(self._pending.items()):
            if force or current_time - last_seen >= self.delay_seconds:
                del self._pending[key]
                if event is not None:
                    ready.append(event)
        ready.sort(key=lambda item: (item.timestamp, str(item.path)))
        return ready

    def flush(
        self,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> list[FileEvent]:
        current_time = time.monotonic() if now is None else now
        with self._lock:
            return self._flush_locked(current_time, force=force)


def debounce_events(
    events: Iterable[FileEvent],
    *,
    delay_seconds: float = 0.25,
) -> list[FileEvent]:
    """Debounce a finite sequence using event timestamps as its clock."""

    debouncer = Debouncer(delay_seconds)
    output: list[FileEvent] = []
    last_clock = 0.0
    for event in events:
        clock = event.timestamp if event.timestamp >= last_clock else last_clock
        output.extend(debouncer.add(event, now=clock))
        last_clock = clock
    output.extend(debouncer.flush(now=last_clock + delay_seconds, force=True))
    return output


@dataclass(frozen=True, slots=True)
class FilesystemConfig:
    """Configuration for a bounded filesystem source."""

    roots: Sequence[PathLike | Path]
    recursive: bool = True
    poll_interval: float = 0.5
    debounce_seconds: float = 0.25
    compute_hashes: bool = True
    max_hash_bytes: int | None = None
    include_directories: bool = False
    force_polling: bool = False
    capture_content: bool = False
    exclude_names: frozenset[str] = field(default_factory=frozenset)
    exclude_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", normalize_configured_roots(self.roots))
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if self.debounce_seconds < 0:
            raise ValueError("debounce_seconds must be non-negative")
        if self.max_hash_bytes is not None and self.max_hash_bytes < 0:
            raise ValueError("max_hash_bytes cannot be negative")
        if self.capture_content:
            raise ValueError("filesystem events do not support content capture")
        object.__setattr__(self, "exclude_names", frozenset(self.exclude_names))
        object.__setattr__(self, "exclude_patterns", tuple(self.exclude_patterns))


WatcherConfig = FilesystemConfig


def discover_git_repositories(
    roots: PathLike | Path | Iterable[PathLike | Path],
    *,
    max_depth: int | None = None,
    max_repositories: int | None = None,
) -> list[Path]:
    """Find Git worktree roots below configured directories.

    Both regular ``.git`` directories and worktree/submodule ``.git`` files
    are recognized.  Traversal is bounded to the supplied roots, does not
    follow symlink directories, and prunes generated/cache trees.
    """

    if max_depth is not None and max_depth < 0:
        return []
    if max_repositories is not None and max_repositories <= 0:
        return []

    normalized_roots = normalize_configured_roots(roots)
    found: set[Path] = set()
    for root in normalized_roots:
        if not root.exists() or not root.is_dir():
            continue
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = normalize_path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                continue
            if max_depth is not None and depth > max_depth:
                directories[:] = []
                continue
            marker = current_path / ".git"
            try:
                marker_info = marker.lstat()
            except (FileNotFoundError, OSError):
                marker_info = None
            if marker_info is not None and not stat.S_ISLNK(marker_info.st_mode):
                if stat.S_ISDIR(marker_info.st_mode) or stat.S_ISREG(marker_info.st_mode):
                    found.add(current_path)
                    if max_repositories is not None and len(found) >= max_repositories:
                        break

            kept: list[str] = []
            for name in directories:
                candidate = current_path / name
                if name == ".git" or name in DEFAULT_IGNORED_DIRECTORY_NAMES:
                    continue
                try:
                    if candidate.is_symlink():
                        continue
                except OSError:
                    continue
                if is_safe_path(candidate, (root,), allow_directories=True):
                    kept.append(name)
            if max_depth is not None and depth >= max_depth:
                kept = []
            directories[:] = kept
            # ``files`` is only consumed to make the intent explicit: a
            # worktree .git marker is discovered by the lstat above.
            _ = files
        if max_repositories is not None and len(found) >= max_repositories:
            break
    return sorted(found, key=lambda item: os.path.normcase(str(item)))


discover_repositories = discover_git_repositories


class _WatchdogHandler(FileSystemEventHandler):
    def __init__(self, source: "FilesystemSource") -> None:
        super().__init__()
        self.source = source

    def on_created(self, event: Any) -> None:  # pragma: no cover - optional backend
        self.source._handle_watchdog_event("created", event)

    def on_modified(self, event: Any) -> None:  # pragma: no cover - optional backend
        self.source._handle_watchdog_event("modified", event)

    def on_deleted(self, event: Any) -> None:  # pragma: no cover - optional backend
        self.source._handle_watchdog_event("deleted", event)

    def on_moved(self, event: Any) -> None:  # pragma: no cover - optional backend
        self.source._handle_watchdog_event("moved", event)


class FilesystemSource:
    """Watch configured roots and publish normalized metadata-only events."""

    def __init__(
        self,
        roots: FilesystemConfig | PathLike | Path | Iterable[PathLike | Path] | None = None,
        *,
        config: FilesystemConfig | None = None,
        **config_overrides: Any,
    ) -> None:
        if isinstance(roots, FilesystemConfig):
            if config is not None or config_overrides:
                raise TypeError("do not combine a FilesystemConfig with config overrides")
            selected_config = roots
        elif config is not None:
            if roots is not None or config_overrides:
                raise TypeError("provide either config or roots/config overrides")
            selected_config = config
        else:
            if roots is None:
                raise ValueError("configured filesystem roots are required")
            if isinstance(roots, (str, os.PathLike, Path)):
                root_values: Sequence[PathLike | Path] = [roots]
            else:
                root_values = list(roots)
            selected_config = FilesystemConfig(roots=root_values, **config_overrides)

        self.config = selected_config
        self.roots = self.config.roots
        self.backend: Literal["watchdog", "polling"] | None = None
        self._stop_event = threading.Event()
        self._raw_events: Queue[FileEvent | None] = Queue()
        self._events: Queue[FileEvent] = Queue()
        self._debouncer = Debouncer(self.config.debounce_seconds)
        self._snapshot: dict[Path, FileMetadata] | None = None
        self._snapshot_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._running = False
        self._observer: Any = None
        self._dispatcher_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def using_watchdog(self) -> bool:
        return self.backend == "watchdog"

    @property
    def mode(self) -> str:
        if self._running and self.backend is not None:
            return self.backend
        return "stopped"

    def discover_repositories(self) -> list[Path]:
        return discover_git_repositories(self.roots)

    discover_git_repositories = discover_repositories

    def _safe(self, path: PathLike | Path, *, is_directory: bool) -> bool:
        return is_safe_path(
            path,
            self.roots,
            allow_directories=is_directory,
            exclude_names=self.config.exclude_names,
            exclude_patterns=self.config.exclude_patterns,
        )

    def _metadata(self, path: Path, root: Path) -> FileMetadata | None:
        if not self._safe(path, is_directory=path.is_dir() if path.exists() else False):
            return None
        return file_metadata(
            path,
            root=root,
            compute_hash=self.config.compute_hashes,
            max_hash_bytes=self.config.max_hash_bytes,
        )

    def _scan_snapshot(self) -> dict[Path, FileMetadata]:
        snapshot: dict[Path, FileMetadata] = {}
        for root in self.roots:
            if not root.exists() or not root.is_dir():
                continue
            if self.config.recursive:
                walker: Iterable[tuple[str, list[str], list[str]]] = os.walk(
                    root, topdown=True, followlinks=False
                )
                for current, directories, files in walker:
                    current_path = normalize_path(current)
                    directories[:] = [
                        name
                        for name in directories
                        if name not in DEFAULT_IGNORED_DIRECTORY_NAMES
                        and self._safe(current_path / name, is_directory=True)
                    ]
                    for name in files:
                        candidate = current_path / name
                        if not self._safe(candidate, is_directory=False):
                            continue
                        details = file_metadata(
                            candidate,
                            root=self._root_for_path(candidate),
                            compute_hash=self.config.compute_hashes,
                            max_hash_bytes=self.config.max_hash_bytes,
                        )
                        if details is not None and not details.is_directory:
                            snapshot[details.path] = details
            else:
                try:
                    entries = list(root.iterdir())
                except OSError:
                    continue
                for candidate in entries:
                    if not self._safe(candidate, is_directory=candidate.is_dir()):
                        continue
                    details = file_metadata(
                        candidate,
                        root=self._root_for_path(candidate),
                        compute_hash=self.config.compute_hashes,
                        max_hash_bytes=self.config.max_hash_bytes,
                    )
                    if details is not None and not details.is_directory:
                        snapshot[details.path] = details
        return snapshot

    def _root_for_path(self, path: Path) -> Path:
        matches = [root for root in self.roots if _is_relative_to(path, root)]
        if not matches:
            return self.roots[0]
        return max(matches, key=lambda item: len(item.parts))

    @staticmethod
    def _fingerprint(details: FileMetadata) -> tuple[Any, ...]:
        return (details.sha256, details.size, details.mtime_ns)

    def _poll_changes(self) -> list[FileEvent]:
        current = self._scan_snapshot()
        with self._snapshot_lock:
            previous = self._snapshot
            self._snapshot = current
        if previous is None:
            return []

        added = {path: value for path, value in current.items() if path not in previous}
        removed = {path: value for path, value in previous.items() if path not in current}
        changed = {
            path: (previous[path], current[path])
            for path in current.keys() & previous.keys()
            if self._fingerprint(previous[path]) != self._fingerprint(current[path])
        }

        events: list[FileEvent] = []
        # Pair an in-root rename when the before/after fingerprints are exact.
        # Unpaired paths remain ordinary create/delete events.
        removed_by_fingerprint: dict[tuple[Any, ...], list[tuple[Path, FileMetadata]]] = {}
        for old_path, details in removed.items():
            removed_by_fingerprint.setdefault(self._fingerprint(details), []).append(
                (old_path, details)
            )
        paired_removed: set[Path] = set()
        paired_added: set[Path] = set()
        for new_path, details in sorted(added.items(), key=lambda item: str(item[0])):
            candidates = removed_by_fingerprint.get(self._fingerprint(details), [])
            if not candidates:
                continue
            old_path, _ = candidates.pop(0)
            paired_removed.add(old_path)
            paired_added.add(new_path)
            event = normalize_file_event(
                "moved",
                old_path,
                self._root_for_path(new_path),
                dest_path=new_path,
                timestamp=time.time(),
                compute_hash=self.config.compute_hashes,
                max_hash_bytes=self.config.max_hash_bytes,
                include_directories=self.config.include_directories,
                metadata=details,
                exclude_names=self.config.exclude_names,
                exclude_patterns=self.config.exclude_patterns,
            )
            if event is not None:
                events.append(event)

        for path, details in sorted(added.items(), key=lambda item: str(item[0])):
            if path in paired_added:
                continue
            event = normalize_file_event(
                "created",
                path,
                details.root,
                timestamp=time.time(),
                compute_hash=self.config.compute_hashes,
                max_hash_bytes=self.config.max_hash_bytes,
                include_directories=self.config.include_directories,
                metadata=details,
                exclude_names=self.config.exclude_names,
                exclude_patterns=self.config.exclude_patterns,
            )
            if event is not None:
                events.append(event)

        for path, details in sorted(removed.items(), key=lambda item: str(item[0])):
            if path in paired_removed:
                continue
            event = normalize_file_event(
                "deleted",
                path,
                details.root,
                timestamp=time.time(),
                compute_hash=self.config.compute_hashes,
                max_hash_bytes=self.config.max_hash_bytes,
                include_directories=self.config.include_directories,
                metadata=details,
                exclude_names=self.config.exclude_names,
                exclude_patterns=self.config.exclude_patterns,
            )
            if event is not None:
                events.append(event)

        for path, (_, details) in sorted(changed.items(), key=lambda item: str(item[0])):
            event = normalize_file_event(
                "modified",
                path,
                details.root,
                timestamp=time.time(),
                compute_hash=self.config.compute_hashes,
                max_hash_bytes=self.config.max_hash_bytes,
                include_directories=self.config.include_directories,
                metadata=details,
                exclude_names=self.config.exclude_names,
                exclude_patterns=self.config.exclude_patterns,
            )
            if event is not None:
                events.append(event)
        return events

    def poll_once(self) -> list[FileEvent]:
        """Scan once and return raw normalized changes.

        When the source is running, the same events are also sent through the
        debounced event queue.  The first scan establishes a baseline and
        intentionally emits no events for pre-existing files.
        """

        events = self._poll_changes()
        if self._running:
            for event in events:
                self._raw_events.put(event)
        return events

    poll = poll_once

    def _handle_watchdog_event(self, event_type: str, event: Any) -> None:
        is_directory = bool(getattr(event, "is_directory", False))
        source = getattr(event, "src_path", None)
        if source is None:
            return
        destination = getattr(event, "dest_path", None) if event_type == "moved" else None
        event_root = self._root_for_path(normalize_path(destination or source))
        previous_metadata: FileMetadata | None = None
        if event_type in {"deleted", "moved"}:
            source_path = normalize_path(source)
            with self._snapshot_lock:
                previous_metadata = (
                    self._snapshot.get(source_path) if self._snapshot is not None else None
                )
        normalized = normalize_file_event(
            event_type,
            source,
            event_root,
            is_directory=is_directory,
            dest_path=destination,
            compute_hash=self.config.compute_hashes,
            max_hash_bytes=self.config.max_hash_bytes,
            include_directories=self.config.include_directories,
            metadata=previous_metadata,
            exclude_names=self.config.exclude_names,
            exclude_patterns=self.config.exclude_patterns,
        )
        if normalized is not None:
            self._raw_events.put(normalized)

    def _dispatch_loop(self) -> None:
        while True:
            try:
                item = self._raw_events.get(
                    timeout=min(0.05, max(0.01, self.config.debounce_seconds / 2))
                )
            except Empty:
                item = None
            if item is not None:
                for ready in self._debouncer.add(item):
                    self._events.put(ready)
            for ready in self._debouncer.flush():
                self._events.put(ready)
            if self._stop_event.is_set() and self._raw_events.empty():
                for ready in self._debouncer.flush(force=True):
                    self._events.put(ready)
                return

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self.config.poll_interval):
            self.poll_once()

    def start(self) -> "FilesystemSource":
        """Start watchdog or polling for the configured roots."""

        with self._lifecycle_lock:
            if self._running:
                return self
            self._stop_event.clear()
            self._debouncer = Debouncer(self.config.debounce_seconds)
            with self._snapshot_lock:
                self._snapshot = self._scan_snapshot()
            self._dispatcher_thread = threading.Thread(
                target=self._dispatch_loop,
                name="filesystem-event-dispatch",
                daemon=True,
            )
            self._running = True
            self._dispatcher_thread.start()

            self.backend = None
            if WATCHDOG_AVAILABLE and not self.config.force_polling and Observer is not None:
                observer: Any = None
                try:  # pragma: no cover - depends on optional platform backend
                    observer = Observer()
                    handler = _WatchdogHandler(self)
                    scheduled = 0
                    for root in self.roots:
                        if root.exists() and root.is_dir():
                            observer.schedule(handler, str(root), recursive=self.config.recursive)
                            scheduled += 1
                    if not scheduled:
                        raise RuntimeError("no existing configured root can be watched")
                    observer.start()
                    self._observer = observer
                    self.backend = "watchdog"
                except Exception:
                    if observer is not None:
                        try:
                            observer.stop()
                            observer.join(timeout=1)
                        except Exception:
                            pass
                    self._observer = None

            if self.backend != "watchdog":
                self.backend = "polling"
                self._poll_thread = threading.Thread(
                    target=self._poll_loop,
                    name="filesystem-polling",
                    daemon=True,
                )
                self._poll_thread.start()
            return self

    def stop(self) -> None:
        """Stop the backend and flush already-observed debounced events."""

        with self._lifecycle_lock:
            if not self._running:
                return
            self._stop_event.set()
            observer = self._observer
            if observer is not None:
                try:
                    observer.stop()
                except Exception:
                    pass
            if self._poll_thread is not None:
                self._poll_thread.join(timeout=max(1.0, self.config.poll_interval * 2))
                self._poll_thread = None
            if observer is not None:
                try:
                    observer.join(timeout=max(1.0, self.config.poll_interval * 2))
                except Exception:
                    pass
                self._observer = None
            self._raw_events.put(None)
            if self._dispatcher_thread is not None:
                self._dispatcher_thread.join(timeout=2)
                self._dispatcher_thread = None
            self._running = False

    close = stop

    def get_event(self, timeout: float | None = None) -> FileEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except Empty:
            return None

    wait_for_event = get_event

    def drain_events(self) -> list[FileEvent]:
        output: list[FileEvent] = []
        while True:
            try:
                output.append(self._events.get_nowait())
            except Empty:
                return output

    def iter_events(self, timeout: float = 0.1) -> Iterator[FileEvent]:
        """Yield queued events until a stopped source has drained."""

        while self._running or not self._events.empty():
            event = self.get_event(timeout=timeout)
            if event is not None:
                yield event

    events = iter_events

    def __enter__(self) -> "FilesystemSource":
        return self.start()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.stop()


# Both spellings are common in existing collector code.
FileSystemSource = FilesystemSource
FilesystemWatcher = FilesystemSource
FileSystemWatcher = FilesystemSource


__all__ = [
    "Debouncer",
    "EventType",
    "FileEvent",
    "FileMetadata",
    "FileSystemSource",
    "FileSystemWatcher",
    "FilesystemConfig",
    "FilesystemSource",
    "FilesystemWatcher",
    "NormalizedFileEvent",
    "WatcherConfig",
    "WATCHDOG_AVAILABLE",
    "debounce_events",
    "discover_repositories",
    "discover_git_repositories",
    "file_metadata",
    "is_safe_path",
    "metadata_for_path",
    "normalize_configured_roots",
    "normalize_event",
    "normalize_file_event",
    "normalize_path",
    "safe_path_filter",
    "sha256_file",
]
