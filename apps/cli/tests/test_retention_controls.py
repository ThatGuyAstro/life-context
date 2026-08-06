from __future__ import annotations

import json
import unittest
from io import StringIO

from apps.cli.contextctl import main


class RetentionRecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def config(self, operation):
        self.calls.append(("config", operation))
        return {"config": operation}

    def purge(self, options):
        self.calls.append(("purge", options))
        return {"purge": options}


class RetentionControlTests(unittest.TestCase):
    def test_config_payload_preserves_raw_hours_and_keyframe_days(self):
        client = RetentionRecordingClient()
        stdout = StringIO()
        stderr = StringIO()

        code = main(
            [
                "config",
                "--set",
                "retention",
                '{"raw_hours":48,"keyframe_days":14}',
                "--json",
            ],
            client=client,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "config": {
                    "operation": "set",
                    "key": "retention",
                    "value": {"raw_hours": 48, "keyframe_days": 14},
                }
            },
        )
        self.assertEqual(
            client.calls,
            [
                (
                    "config",
                    {
                        "operation": "set",
                        "key": "retention",
                        "value": {"raw_hours": 48, "keyframe_days": 14},
                    },
                )
            ],
        )

    def test_purge_payload_includes_date_source_project_and_explicit_confirmation(self):
        client = RetentionRecordingClient()
        stdout = StringIO()
        stderr = StringIO()

        code = main(
            [
                "purge",
                "--before",
                "2026-01-01T00:00:00Z",
                "--source",
                "clipboard",
                "--project",
                "life-context",
                "--yes",
                "--json",
            ],
            client=client,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        expected = {
            "before": "2026-01-01T00:00:00Z",
            "source": "clipboard",
            "project": "life-context",
            "all": False,
            "confirmed": True,
            "dry_run": False,
        }
        self.assertEqual(json.loads(stdout.getvalue()), {"purge": expected})
        self.assertEqual(client.calls, [("purge", expected)])
