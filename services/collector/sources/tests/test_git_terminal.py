"""Focused tests for the read-only Git and terminal source collector."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.collector.sources import git_terminal


class GitTerminalSourceTests(unittest.TestCase):
    def _git(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Source Test",
                "GIT_AUTHOR_EMAIL": "source-test@example.invalid",
                "GIT_COMMITTER_NAME": "Source Test",
                "GIT_COMMITTER_EMAIL": "source-test@example.invalid",
            }
        )
        return subprocess.run(
            ["git", *arguments],
            cwd=str(repository),
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_discovers_repo_and_normalizes_status_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repo with spaces; harmless"
            repository.mkdir()
            self._git(repository, "init", "--initial-branch=main")
            tracked = repository / "tracked.txt"
            tracked.write_text("one\n", encoding="utf-8")
            self._git(repository, "add", "tracked.txt")
            self._git(repository, "commit", "-m", "initial")
            tracked.write_text("one\ntwo\n", encoding="utf-8")

            self.assertEqual(git_terminal.discover_repositories(root), [repository.resolve()])
            events = git_terminal.collect_git_repository_events([repository])

            status_events = [
                event
                for event in events
                if event["payload"]["event_kind"] == "git_status"
            ]
            diff_events = [
                event
                for event in events
                if event["payload"]["event_kind"] == "git_diff"
            ]
            self.assertEqual(len(status_events), 1)
            self.assertEqual(len(diff_events), 2)
            status_payload = status_events[0]["payload"]
            self.assertFalse(status_payload["clean"])
            self.assertEqual(status_payload["entries"][0]["path"], "tracked.txt")
            working_diff = next(
                event
                for event in diff_events
                if event["payload"]["scope"] == "working_tree"
            )
            self.assertIn("+two", working_diff["payload"]["diff"])
            self.assertEqual(status_events[0]["event_type"], "observed")
            self.assertEqual(status_events[0]["source_kind"], "application")
            self.assertEqual(
                set(status_events[0]),
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
                },
            )
            self.assertIsNotNone(git_terminal.collect_git_diff(repository))

    def test_git_invocation_uses_argument_vector_not_shell(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "## main\n", "")

        with tempfile.TemporaryDirectory() as temporary_directory:
            malicious_directory = Path(temporary_directory) / "repo; echo injected"
            malicious_directory.mkdir()
            with patch.object(git_terminal.subprocess, "run", side_effect=fake_run):
                event = git_terminal.collect_git_status(malicious_directory)

        self.assertIsNotNone(event)
        self.assertIsInstance(captured["command"], list)
        self.assertFalse(captured["shell"])
        self.assertEqual(captured["cwd"], str(malicious_directory.resolve()))
        self.assertNotIn("; echo injected", " ".join(captured["command"]))

    def test_powershell_history_is_bounded_and_file_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            history = Path(temporary_directory) / "ConsoleHost_history.txt"
            history.write_text("first\nsecond\nthird\nfourth\n", encoding="utf-8")

            event = git_terminal.collect_powershell_history(
                history_path=history,
                max_lines=2,
                max_bytes=1024,
            )

            self.assertIsNotNone(event)
            payload = event["payload"]
            self.assertEqual(payload["commands"], ["third", "fourth"])
            self.assertTrue(payload["truncated"])
            self.assertTrue(payload["persisted_file_only"])
            self.assertNotIn("keylog", repr(event).lower())

    def test_missing_history_is_not_an_error(self) -> None:
        event = git_terminal.collect_powershell_history(
            history_path=Path(tempfile.gettempdir()) / "not-present-console-history.txt"
        )
        self.assertIsNone(event)

    def test_process_event_excludes_command_lines(self) -> None:
        event = git_terminal.collect_process_metadata(max_processes=4)
        payload = event["payload"]
        self.assertFalse(payload["command_lines_collected"])
        self.assertFalse(payload["environment_collected"])
        self.assertFalse(payload["keylogging"])
        for process in payload["processes"]:
            self.assertNotIn("cmdline", process)
            self.assertNotIn("command_line", process)


if __name__ == "__main__":
    unittest.main()
