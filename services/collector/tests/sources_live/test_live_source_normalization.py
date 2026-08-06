"""Live normalization checks for the current workspace source adapters."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from contracts import SourceEvent
from services.collector.sources import git_terminal


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SOURCE_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "source_id",
        "source_kind",
        "event_type",
        "occurred_at",
        "captured_at",
        "payload",
        "artifacts",
        "metadata",
    }
)
PROCESS_CAPTURE_FIELDS = frozenset(
    {
        "args",
        "argv",
        "command_line",
        "command_line_args",
        "cmdline",
        "environ",
        "environment",
        "input",
        "key_events",
        "keyboard",
        "keystrokes",
        "raw_input",
        "raw_keyboard",
    }
)
PROCESS_METADATA_FIELDS = frozenset(
    {"pid", "ppid", "name", "executable", "cwd", "status", "create_time"}
)


def _validate_source_event(event: Mapping[str, Any]) -> SourceEvent:
    """Require the adapter's exact envelope and strict shared contract."""

    assert set(event) == SOURCE_EVENT_FIELDS
    return SourceEvent.model_validate(event)


def _normalized_keys(value: Mapping[str, Any]) -> set[str]:
    return {str(key).casefold().replace("-", "_") for key in value}


def test_current_workspace_git_discovery_status_and_diff_are_strict_events() -> None:
    """Run the real workspace Git commands and normalize every result."""

    assert WORKSPACE_ROOT.is_dir()
    assert (WORKSPACE_ROOT / ".git").exists()

    repositories = git_terminal.discover_repositories(
        WORKSPACE_ROOT,
        max_depth=4,
        max_repositories=8,
    )
    assert WORKSPACE_ROOT in repositories

    status_event = git_terminal.collect_git_status(WORKSPACE_ROOT)
    assert status_event is not None
    diff_events = git_terminal.collect_git_diff_events(WORKSPACE_ROOT)

    events = [status_event, *diff_events]
    validated = [_validate_source_event(event) for event in events]
    assert all(item.source_kind.value == "application" for item in validated)
    assert all(item.event_type.value == "observed" for item in validated)

    status_payload = status_event["payload"]
    assert status_payload["event_kind"] == "git_status"
    assert status_payload["repository"] == str(WORKSPACE_ROOT)
    assert isinstance(status_payload["clean"], bool)
    assert isinstance(status_payload["entries"], list)
    assert isinstance(status_payload["truncated"], bool)

    assert {event["payload"]["scope"] for event in diff_events} == {
        "working_tree",
        "index",
    }
    for event in diff_events:
        payload = event["payload"]
        assert payload["event_kind"] == "git_diff"
        assert payload["repository"] == str(WORKSPACE_ROOT)
        assert isinstance(payload["diff"], str)
        assert isinstance(payload["has_changes"], bool)
        assert isinstance(payload["truncated"], bool)


def test_live_history_process_and_terminal_events_are_strict_and_input_free() -> None:
    """Validate persisted history and live process metadata privacy guards."""

    history_path = git_terminal.find_powershell_history()
    if history_path is None:
        pytest.skip("no persisted PowerShell PSReadLine history file is available")

    history_event = git_terminal.collect_powershell_history(
        history_path=history_path,
        max_lines=8,
        max_bytes=64 * 1024,
    )
    assert history_event is not None
    _validate_source_event(history_event)

    history_payload = history_event["payload"]
    assert set(history_payload) == {
        "path",
        "commands",
        "count",
        "truncated",
        "max_lines",
        "max_bytes",
        "modified_at",
        "persisted_file_only",
        "event_kind",
    }
    assert history_payload["event_kind"] == "powershell_history"
    assert history_payload["path"] == str(history_path)
    assert history_payload["persisted_file_only"] is True
    assert history_payload["count"] == len(history_payload["commands"])
    assert history_payload["count"] <= 8
    assert all(isinstance(command, str) for command in history_payload["commands"])

    process_event = git_terminal.collect_process_metadata(max_processes=8)
    terminal_event = git_terminal.collect_terminal_metadata(environment={})
    _validate_source_event(process_event)
    terminal_payload = terminal_event["payload"]
    _validate_source_event(terminal_event)

    process_payload = process_event["payload"]
    assert set(process_payload) == {
        "processes",
        "count",
        "command_lines_collected",
        "environment_collected",
        "keylogging",
        "event_kind",
    }
    assert process_payload["event_kind"] == "process_metadata"
    assert process_payload["command_lines_collected"] is False
    assert process_payload["environment_collected"] is False
    assert process_payload["keylogging"] is False
    assert process_payload["count"] == len(process_payload["processes"])
    for process in process_payload["processes"]:
        assert _normalized_keys(process).isdisjoint(PROCESS_CAPTURE_FIELDS)
        assert _normalized_keys(process).issubset(PROCESS_METADATA_FIELDS)

    assert terminal_payload["event_kind"] == "terminal_metadata"
    assert terminal_payload["input_collected"] is False
    assert terminal_payload["keylogging"] is False

