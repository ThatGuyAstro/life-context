"""Read-only Git, PowerShell history, and terminal metadata sources.

The collector deliberately does not invoke a shell.  Every Git operation is
passed to :func:`subprocess.run` as a fixed argument list and persisted
PowerShell history is read as a file.  Process metadata is limited to identity
and state fields; command lines, stdin, keyboard hooks, and active input are
never collected.

The shared ``SourceEvent`` contract is strict, so events use its exact
dictionary envelope::

    {
        "event_id": "...",
        "source_id": "git_terminal:git:...",
        "source_kind": "application",
        "event_type": "observed",
        "occurred_at": "2026-01-01T00:00:00Z",
        "captured_at": "2026-01-01T00:00:00Z",
        "payload": {...},
        "artifacts": [],
        "metadata": {"collector": "git_terminal", "read_only": True},
    }

The specific collector operation is stored as ``payload["event_kind"]``;
the shared contract reserves ``event_type`` for its source-neutral
``observed`` value.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TypedDict


PathValue = str | os.PathLike[str]
SourceEvent = dict[str, Any]

DEFAULT_MAX_REPOSITORIES = 100
DEFAULT_MAX_DIFF_BYTES = 256 * 1024
DEFAULT_MAX_STATUS_BYTES = 128 * 1024
DEFAULT_HISTORY_LINES = 50
DEFAULT_HISTORY_BYTES = 128 * 1024
DEFAULT_MAX_PROCESSES = 128
DEFAULT_COMMAND_TIMEOUT = 15.0
_DISCOVERY_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
}


class StatusEntry(TypedDict):
    """Normalized subset of a porcelain-v1 Git status entry."""

    index: str
    worktree: str
    status: str
    path: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _event(source: str, event_kind: str, payload: Mapping[str, Any]) -> SourceEvent:
    """Build a dictionary accepted by the strict shared SourceEvent model."""

    normalized_payload = dict(payload)
    normalized_payload["event_kind"] = event_kind
    timestamp = _utc_now()
    return {
        "event_id": uuid.uuid4().hex,
        "source_id": _source_id(source, normalized_payload),
        "source_kind": "application",
        "event_type": "observed",
        "occurred_at": timestamp,
        "captured_at": timestamp,
        "payload": normalized_payload,
        "artifacts": [],
        "metadata": {"collector": "git_terminal", "read_only": True},
    }


def _source_id(source: str, payload: Mapping[str, Any]) -> str:
    identity = payload.get("repository") or payload.get("path") or source
    digest = hashlib.sha256(f"{source}:{identity}".encode("utf-8")).hexdigest()[:16]
    return f"git_terminal:{source}:{digest}"


def _as_paths(values: PathValue | Iterable[PathValue]) -> list[Path]:
    if isinstance(values, (str, os.PathLike)):
        return [Path(values)]
    return [Path(value) for value in values]


def _resolved_directory(value: PathValue) -> Path | None:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return path if path.is_dir() else None


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _is_filesystem_root(path: Path) -> bool:
    if path.parent == path:
        return True
    try:
        return bool(path.anchor) and path == Path(path.anchor)
    except (OSError, ValueError):
        return False


def discover_repositories(
    roots: PathValue | Iterable[PathValue],
    *,
    max_depth: int = 4,
    max_repositories: int = DEFAULT_MAX_REPOSITORIES,
) -> list[Path]:
    """Find Git worktrees below ``roots`` without invoking a shell.

    A repository is recognized by either a ``.git`` directory or a ``.git``
    file (the latter is used by Git worktrees and some submodules).  Symlinked
    directories are not followed.  ``max_depth`` is relative to each root and
    prevents an accidental scan of an entire drive.
    """

    if max_depth < 0 or max_repositories <= 0:
        return []

    discovered: list[Path] = []
    for root_value in _as_paths(roots):
        root = _resolved_directory(root_value)
        if root is None or _is_filesystem_root(root):
            continue

        # os.walk is used instead of a shell glob so paths are never parsed as
        # commands.  followlinks=False also keeps discovery bounded by the
        # requested tree.
        for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                continue

            git_marker = current_path / ".git"
            if not git_marker.is_symlink() and (git_marker.is_dir() or git_marker.is_file()):
                discovered.append(current_path.resolve())
                directories[:] = []
                if len(discovered) >= max_repositories:
                    break
                continue

            # Never descend into Git's object database.  It cannot contain a
            # nested worktree and can be very large.
            directories[:] = [
                directory
                for directory in directories
                if directory not in _DISCOVERY_IGNORED_DIRS and depth < max_depth
            ]
        if len(discovered) >= max_repositories:
            break

    return _dedupe_paths(discovered)[:max_repositories]


class _CommandResult:
    __slots__ = ("returncode", "stdout", "stderr", "error")

    def __init__(
        self,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT,
) -> _CommandResult:
    """Run a fixed Git subcommand with no shell interpretation."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
        *arguments,
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )

    try:
        completed = subprocess.run(
            command,
            cwd=str(repository),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return _CommandResult(127, error="git executable not found")
    except subprocess.TimeoutExpired:
        return _CommandResult(124, error="git command timed out")
    except OSError as exc:
        return _CommandResult(126, error=f"git command unavailable: {exc}")

    return _CommandResult(
        completed.returncode,
        _command_output_text(completed.stdout),
        _command_output_text(completed.stderr),
    )


def _command_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clip_text(value: str, max_bytes: int) -> tuple[str, bool, int]:
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    encoded = value.encode("utf-8", errors="replace")
    byte_length = len(encoded)
    if byte_length <= max_bytes:
        return value, False, byte_length
    return encoded[:max_bytes].decode("utf-8", errors="replace"), True, byte_length


def _parse_status(output: str) -> tuple[str | None, list[StatusEntry], dict[str, int]]:
    branch: str | None = None
    entries: list[StatusEntry] = []
    tracking: dict[str, int] = {}

    for line in output.splitlines():
        if line.startswith("## "):
            branch_text = line[3:]
            branch = branch_text
            if branch_text.startswith("No commits yet on "):
                branch = branch_text.removeprefix("No commits yet on ")
            elif branch_text.startswith("Initial commit on "):
                branch = branch_text.removeprefix("Initial commit on ")

            match = re.search(r"\[ahead (\d+)(?:, behind (\d+))?\]", branch_text)
            if match:
                tracking["ahead"] = int(match.group(1))
                tracking["behind"] = int(match.group(2) or 0)
            continue

        if len(line) < 4 or line[2] != " ":
            continue
        index_state, worktree_state = line[0], line[1]
        path = line[3:]
        entry: StatusEntry = {
            "index": index_state,
            "worktree": worktree_state,
            "status": index_state + worktree_state,
            "path": path,
        }
        if " -> " in path and index_state in {"R", "C"}:
            old_path, new_path = path.rsplit(" -> ", 1)
            entry["path"] = new_path
            entry["from"] = old_path  # type: ignore[typeddict-item]
        entries.append(entry)

    return branch, entries, tracking


def _repository_for_collection(value: PathValue) -> Path | None:
    return _resolved_directory(value)


def collect_git_status(
    repository: PathValue,
    *,
    max_status_bytes: int = DEFAULT_MAX_STATUS_BYTES,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT,
) -> SourceEvent | None:
    """Collect one normalized porcelain-v1 status event for a repository."""

    repo = _repository_for_collection(repository)
    if repo is None:
        return None

    result = _run_git(
        repo,
        ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
        timeout_seconds=timeout_seconds,
    )
    if not result.ok:
        return None

    output, truncated, byte_length = _clip_text(result.stdout, max_status_bytes)
    branch, entries, tracking = _parse_status(output)
    payload: dict[str, Any] = {
        "repository": str(repo),
        "branch": branch,
        "clean": not entries,
        "entries": entries,
        "tracking": tracking,
        "truncated": truncated,
        "byte_length": byte_length,
    }
    return _event("git", "git_status", payload)


def collect_git_diff_events(
    repository: PathValue,
    *,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT,
) -> list[SourceEvent]:
    """Collect bounded working-tree and index diff events.

    Untracked files are represented by the status source; Git does not include
    them in ``git diff`` until they are staged.
    """

    repo = _repository_for_collection(repository)
    if repo is None:
        return []

    events: list[SourceEvent] = []
    scopes = (
        ("working_tree", ["diff", "--no-ext-diff", "--no-textconv", "--binary", "--unified=0"]),
        (
            "index",
            [
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--unified=0",
            ],
        ),
    )
    for scope, arguments in scopes:
        result = _run_git(repo, arguments, timeout_seconds=timeout_seconds)
        if not result.ok:
            continue
        diff, truncated, byte_length = _clip_text(result.stdout, max_diff_bytes)
        events.append(
            _event(
                "git",
                "git_diff",
                {
                    "repository": str(repo),
                    "scope": scope,
                    "diff": diff,
                    "has_changes": bool(diff),
                    "truncated": truncated,
                    "byte_length": byte_length,
                },
            )
        )
    return events


def collect_git_diff(
    repository: PathValue,
    *,
    staged: bool = False,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT,
) -> SourceEvent | None:
    """Collect one working-tree diff event, or the staged diff when requested."""

    events = collect_git_diff_events(
        repository,
        max_diff_bytes=max_diff_bytes,
        timeout_seconds=timeout_seconds,
    )
    requested_scope = "index" if staged else "working_tree"
    return next(
        (
            event
            for event in events
            if event["payload"].get("scope") == requested_scope
        ),
        None,
    )


collect_git_diffs = collect_git_diff_events


def collect_git_repository_events(
    repositories: PathValue | Iterable[PathValue],
    *,
    include_clean: bool = True,
    max_status_bytes: int = DEFAULT_MAX_STATUS_BYTES,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT,
) -> list[SourceEvent]:
    """Collect status and diff events for already-discovered repositories."""

    events: list[SourceEvent] = []
    paths = _dedupe_paths(
        path
        for path in (_resolved_directory(value) for value in _as_paths(repositories))
        if path is not None
    )
    for repository in paths:
        status = collect_git_status(
            repository,
            max_status_bytes=max_status_bytes,
            timeout_seconds=timeout_seconds,
        )
        if status is not None and (include_clean or not status["payload"]["clean"]):
            events.append(status)
        events.extend(
            collect_git_diff_events(
                repository,
                max_diff_bytes=max_diff_bytes,
                timeout_seconds=timeout_seconds,
            )
        )
    return events


def _history_candidates(environment: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if environment is None else environment
    candidates: list[Path] = []

    appdata = env.get("APPDATA")
    if appdata:
        appdata_path = Path(appdata)
        candidates.extend(
            (
                appdata_path
                / "Microsoft"
                / "Windows"
                / "PowerShell"
                / "PSReadLine"
                / "ConsoleHost_history.txt",
                appdata_path
                / "Microsoft"
                / "PowerShell"
                / "PSReadLine"
                / "ConsoleHost_history.txt",
            )
        )

    home = env.get("HOME") or env.get("USERPROFILE")
    if home:
        home_path = Path(home)
        candidates.extend(
            (
                home_path
                / ".local"
                / "share"
                / "powershell"
                / "PSReadLine"
                / "ConsoleHost_history.txt",
                home_path
                / ".config"
                / "powershell"
                / "PSReadLine"
                / "ConsoleHost_history.txt",
            )
        )

    xdg_data_home = env.get("XDG_DATA_HOME")
    if xdg_data_home:
        candidates.append(
            Path(xdg_data_home)
            / "powershell"
            / "PSReadLine"
            / "ConsoleHost_history.txt"
        )

    return _dedupe_paths(path for path in candidates)


def find_powershell_history(
    *,
    history_path: PathValue | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Return an existing PSReadLine history file, if one is available."""

    if history_path is not None:
        candidate = Path(history_path).expanduser()
        if candidate.is_symlink():
            return None
        try:
            path = candidate.resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return path if path.is_file() else None

    for candidate in _history_candidates(environment):
        if candidate.is_symlink():
            continue
        try:
            path = candidate.expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if path.is_file():
            return path
    return None


def _tail_file(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int,
) -> tuple[list[str], bool]:
    if max_lines < 0:
        raise ValueError("max_lines must be non-negative")
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if max_lines == 0 or max_bytes == 0:
        return [], path.stat().st_size > 0

    with path.open("rb") as history_file:
        history_file.seek(0, os.SEEK_END)
        size = history_file.tell()
        start = max(0, size - max_bytes)
        history_file.seek(start, os.SEEK_SET)
        raw = history_file.read(max_bytes)

    truncated = start > 0
    if start > 0 and b"\n" in raw:
        # The first line may begin before the bounded read window.  Drop that
        # incomplete fragment rather than emitting a misleading command.
        raw = raw.split(b"\n", 1)[1]
    elif start > 0:
        raw = b""

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return lines, truncated


def collect_powershell_history(
    *,
    history_path: PathValue | None = None,
    environment: Mapping[str, str] | None = None,
    max_lines: int = DEFAULT_HISTORY_LINES,
    max_bytes: int = DEFAULT_HISTORY_BYTES,
) -> SourceEvent | None:
    """Tail persisted PSReadLine history without attaching to keyboard input."""

    path = find_powershell_history(history_path=history_path, environment=environment)
    if path is None:
        return None
    try:
        commands, truncated = _tail_file(
            path,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )
        stat = path.stat()
    except (OSError, ValueError):
        return None

    return _event(
        "powershell",
        "powershell_history",
        {
            "path": str(path),
            "commands": commands,
            "count": len(commands),
            "truncated": truncated,
            "max_lines": max_lines,
            "max_bytes": max_bytes,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "persisted_file_only": True,
        },
    )


# Both spellings are kept because callers commonly mirror PowerShell's name
# while Python callers generally use lower-case ``powershell``.
collect_powerShell_history = collect_powershell_history


def _terminal_value(environment: Mapping[str, str], key: str) -> str | None:
    value = environment.get(key)
    if not value:
        return None
    return value[:128]


def _stream_is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def terminal_metadata(*, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return terminal identity flags, never terminal input."""

    env = os.environ if environment is None else environment
    try:
        cwd = str(Path.cwd())
    except OSError:
        cwd = None

    return {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "python_version": platform.python_version(),
        "cwd": cwd,
        "stdin_tty": _stream_is_tty(sys.stdin),
        "stdout_tty": _stream_is_tty(sys.stdout),
        "terminal_program": _terminal_value(env, "TERM_PROGRAM"),
        "terminal": _terminal_value(env, "TERM"),
        "shell": _terminal_value(env, "SHELL") or _terminal_value(env, "ComSpec"),
        "windows_terminal": bool(env.get("WT_SESSION")),
        "ssh_session": bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY")),
        "ci_environment": bool(env.get("CI") or env.get("GITHUB_ACTIONS")),
        "input_collected": False,
        "keylogging": False,
    }


def collect_terminal_metadata(
    *,
    environment: Mapping[str, str] | None = None,
) -> SourceEvent:
    """Return one terminal metadata event."""

    return _event("terminal", "terminal_metadata", terminal_metadata(environment=environment))


def _iso_from_epoch(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _self_process_record() -> dict[str, Any]:
    executable = str(Path(sys.executable).resolve())
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "name": Path(sys.executable).name,
        "executable": executable,
        "cwd": terminal_metadata()["cwd"],
        "create_time": None,
    }


def process_metadata(*, max_processes: int = DEFAULT_MAX_PROCESSES) -> list[dict[str, Any]]:
    """Return process identity/state metadata without command lines.

    ``psutil`` is optional.  When unavailable, the current collector process
    is still described, which keeps this source useful without adding a hard
    dependency.  The requested ``attrs`` list intentionally excludes
    ``cmdline`` and ``environ``.
    """

    if max_processes < 0:
        raise ValueError("max_processes must be non-negative")
    if max_processes == 0:
        return []

    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return [_self_process_record()]

    records: list[dict[str, Any]] = []
    attrs = ("pid", "ppid", "name", "exe", "cwd", "status", "create_time")
    try:
        processes = psutil.process_iter(attrs=attrs, ad_value=None)
        for process in processes:
            try:
                info = dict(process.info)
            except (AttributeError, OSError, psutil.Error):
                continue
            record = {
                "pid": info.get("pid"),
                "ppid": info.get("ppid"),
                "name": info.get("name"),
                "executable": info.get("exe"),
                "cwd": info.get("cwd"),
                "status": info.get("status"),
                "create_time": _iso_from_epoch(info.get("create_time")),
            }
            records.append(record)
            if len(records) >= max_processes:
                break
    except (OSError, psutil.Error):
        records = []

    if not records:
        records.append(_self_process_record())
    return records[:max_processes]


def collect_process_metadata(
    *,
    max_processes: int = DEFAULT_MAX_PROCESSES,
) -> SourceEvent:
    """Return one process metadata event; no command lines are included."""

    processes = process_metadata(max_processes=max_processes)
    return _event(
        "process",
        "process_metadata",
        {
            "processes": processes,
            "count": len(processes),
            "command_lines_collected": False,
            "environment_collected": False,
            "keylogging": False,
        },
    )


def collect_terminal_process_events(
    *,
    environment: Mapping[str, str] | None = None,
    max_processes: int = DEFAULT_MAX_PROCESSES,
) -> list[SourceEvent]:
    """Collect terminal and process metadata events."""

    return [
        collect_terminal_metadata(environment=environment),
        collect_process_metadata(max_processes=max_processes),
    ]


def collect_git_terminal_events(
    repositories: PathValue | Iterable[PathValue] | None = None,
    *,
    discovered_repositories: PathValue | Iterable[PathValue] | None = None,
    search_roots: PathValue | Iterable[PathValue] | None = None,
    roots: PathValue | Iterable[PathValue] | None = None,
    include_history: bool = True,
    include_processes: bool = True,
    include_clean: bool = True,
    history_path: PathValue | None = None,
    environment: Mapping[str, str] | None = None,
    max_depth: int = 4,
    max_repositories: int = DEFAULT_MAX_REPOSITORIES,
    max_status_bytes: int = DEFAULT_MAX_STATUS_BYTES,
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    max_history_lines: int = DEFAULT_HISTORY_LINES,
    max_history_bytes: int = DEFAULT_HISTORY_BYTES,
    max_processes: int = DEFAULT_MAX_PROCESSES,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT,
) -> list[SourceEvent]:
    """Collect all Git/terminal sources in a bounded, read-only pass.

    ``repositories`` and ``discovered_repositories`` are synonyms; the latter
    is provided for callers whose discovery phase already ran.  If neither is
    supplied, only ``search_roots`` (or the current directory) is scanned.
    """

    if search_roots is not None and roots is not None:
        raise ValueError("provide search_roots or roots, not both")
    if repositories is not None and discovered_repositories is not None:
        raise ValueError("provide repositories or discovered_repositories, not both")

    selected = repositories if repositories is not None else discovered_repositories
    if selected is None:
        scan_roots = search_roots if search_roots is not None else roots
        scan_roots = scan_roots if scan_roots is not None else Path.cwd()
        selected_paths = discover_repositories(
            scan_roots,
            max_depth=max_depth,
            max_repositories=max_repositories,
        )
    else:
        selected_paths = _as_paths(selected)

    events = collect_git_repository_events(
        selected_paths,
        include_clean=include_clean,
        max_status_bytes=max_status_bytes,
        max_diff_bytes=max_diff_bytes,
        timeout_seconds=timeout_seconds,
    )

    if include_history:
        history = collect_powershell_history(
            history_path=history_path,
            environment=environment,
            max_lines=max_history_lines,
            max_bytes=max_history_bytes,
        )
        if history is not None:
            events.append(history)

    if include_processes:
        events.extend(
            collect_terminal_process_events(
                environment=environment,
                max_processes=max_processes,
            )
        )
    return events


class GitTerminalSource:
    """Small source object for registries that use ``collect``/``read``."""

    source = "git_terminal"

    def __init__(
        self,
        repositories: PathValue | Iterable[PathValue] | None = None,
        *,
        discovered_repositories: PathValue | Iterable[PathValue] | None = None,
        search_roots: PathValue | Iterable[PathValue] | None = None,
        roots: PathValue | Iterable[PathValue] | None = None,
        **collection_options: Any,
    ) -> None:
        if search_roots is not None and roots is not None:
            raise ValueError("provide search_roots or roots, not both")
        self.repositories = repositories
        self.discovered_repositories = discovered_repositories
        self.search_roots = search_roots if search_roots is not None else roots
        self.collection_options = dict(collection_options)

    def discover_repositories(self) -> list[Path]:
        roots = self.search_roots if self.search_roots is not None else Path.cwd()
        return discover_repositories(roots, **_discovery_options(self.collection_options))

    def collect(self) -> list[SourceEvent]:
        return collect_git_terminal_events(
            self.repositories,
            discovered_repositories=self.discovered_repositories,
            search_roots=self.search_roots,
            **self.collection_options,
        )

    def read(self) -> list[SourceEvent]:
        return self.collect()

    snapshot = read


def _discovery_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Select options accepted by standalone repository discovery."""

    return {
        key: options[key]
        for key in ("max_depth", "max_repositories")
        if key in options
    }


def collect(
    repositories: PathValue | Iterable[PathValue] | None = None,
    **kwargs: Any,
) -> list[SourceEvent]:
    """Short compatibility entry point for source registries."""

    return collect_git_terminal_events(repositories, **kwargs)


collect_sources = collect_git_terminal_events
collect_git_terminal_sources = collect_git_terminal_events
discover_git_repositories = discover_repositories


__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_HISTORY_BYTES",
    "DEFAULT_HISTORY_LINES",
    "DEFAULT_MAX_DIFF_BYTES",
    "DEFAULT_MAX_PROCESSES",
    "DEFAULT_MAX_REPOSITORIES",
    "DEFAULT_MAX_STATUS_BYTES",
    "GitTerminalSource",
    "SourceEvent",
    "collect",
    "collect_git_diff",
    "collect_git_diff_events",
    "collect_git_diffs",
    "collect_git_repository_events",
    "collect_git_status",
    "collect_git_terminal_events",
    "collect_git_terminal_sources",
    "collect_powerShell_history",
    "collect_powershell_history",
    "collect_process_metadata",
    "collect_sources",
    "collect_terminal_metadata",
    "collect_terminal_process_events",
    "discover_repositories",
    "discover_git_repositories",
    "find_powershell_history",
    "process_metadata",
    "terminal_metadata",
]
