from __future__ import annotations

import json
import os
import unittest
from io import StringIO
from unittest.mock import patch

from apps.cli.contextctl import main
from services.context_server.query.models import QueryRequest


class FakeClient:
    def __init__(self):
        self.calls = []

    def start(self, options=None):
        self.calls.append(("start", options))
        return {"started": True, "options": options}

    def pause(self):
        self.calls.append(("pause",))
        return {"paused": True}

    def health(self):
        self.calls.append(("health",))
        return {"status": "ok", "ready": True}

    def status(self):
        self.calls.append(("status",))
        return {"state": "running"}

    def query(self, request):
        self.calls.append(("query", request))
        return {"query": request.query, "request": request.to_dict()}

    def timeline(self, options):
        self.calls.append(("timeline", options))
        return {"timeline": options}

    def purge(self, options):
        self.calls.append(("purge", options))
        return {"purge": options}

    def config(self, operation):
        self.calls.append(("config", operation))
        return {"config": operation}


class ContextCtlTests(unittest.TestCase):
    def test_start_pause_status_timeline_purge_and_config_dispatch_with_json(self):
        cases = (
            (
                ["start", "--mode", "resume", "--json"],
                {"started": True, "options": {"mode": "resume"}},
                ("start", {"mode": "resume"}),
            ),
            (
                ["pause", "--json"],
                {"paused": True},
                ("pause",),
            ),
            (
                ["stop", "--json"],
                {"paused": True},
                ("pause",),
            ),
            (
                ["health", "--json"],
                {"status": "ok", "ready": True},
                ("health",),
            ),
            (
                ["status", "--json"],
                {"state": "running"},
                ("status",),
            ),
            (
                [
                    "timeline",
                    "--from",
                    "2026-01-01T00:00:00Z",
                    "--to",
                    "2026-01-02T00:00:00Z",
                    "--limit",
                    "3",
                    "--subject",
                    "alice",
                    "--kind",
                    "note",
                    "--json",
                ],
                {
                    "timeline": {
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-01-02T00:00:00Z",
                        "limit": 3,
                        "subject": "alice",
                        "kind": "note",
                    }
                },
                (
                    "timeline",
                    {
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-01-02T00:00:00Z",
                        "limit": 3,
                        "subject": "alice",
                        "kind": "note",
                    },
                ),
            ),
            (
                ["purge", "--before", "2026-01-01T00:00:00Z", "--yes", "--json"],
                {
                    "purge": {
                        "before": "2026-01-01T00:00:00Z",
                        "all": False,
                        "confirmed": True,
                        "dry_run": False,
                    }
                },
                (
                    "purge",
                    {
                        "before": "2026-01-01T00:00:00Z",
                        "all": False,
                        "confirmed": True,
                        "dry_run": False,
                    },
                ),
            ),
            (
                ["config", "--set", "retention_days", "30", "--json"],
                {
                    "config": {"operation": "set", "key": "retention_days", "value": 30}
                },
                (
                    "config",
                    {"operation": "set", "key": "retention_days", "value": 30},
                ),
            ),
            (
                ["config", "--get", "mode", "--json"],
                {"config": {"operation": "get", "key": "mode"}},
                ("config", {"operation": "get", "key": "mode"}),
            ),
            (
                [
                    "model",
                    "--endpoint",
                    "http://127.0.0.1:8080/v1",
                    "--name",
                    "gemma-local",
                    "--revision",
                    "Q4_K_M",
                    "--json",
                ],
                {
                    "config": {
                        "operation": "update",
                        "settings": {
                            "model_endpoint": "http://127.0.0.1:8080/v1",
                            "model_name": "gemma-local",
                            "model_revision": "Q4_K_M",
                        },
                    }
                },
                (
                    "config",
                    {
                        "operation": "update",
                        "settings": {
                            "model_endpoint": "http://127.0.0.1:8080/v1",
                            "model_name": "gemma-local",
                            "model_revision": "Q4_K_M",
                        },
                    },
                ),
            ),
        )

        for argv, expected_output, expected_call in cases:
            with self.subTest(command=argv[0]):
                client = FakeClient()
                stdout = StringIO()
                stderr = StringIO()
                code = main(argv, client=client, stdout=stdout, stderr=stderr)

                self.assertEqual(code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(json.loads(stdout.getvalue()), expected_output)
                self.assertEqual(client.calls, [expected_call])

    def test_model_clear_rejects_mixed_fields(self):
        client = FakeClient()
        error = StringIO()
        self.assertEqual(
            main(
                ["model", "--clear", "--name", "gemma", "--json"],
                client=client,
                stdout=StringIO(),
                stderr=error,
            ),
            2,
        )
        self.assertEqual(
            error.getvalue(),
            "contextctl: error: model --clear cannot be combined with model fields\n",
        )
        self.assertEqual(client.calls, [])

    def test_query_builds_canonical_request_and_emits_json(self):
        client = FakeClient()
        stdout = StringIO()
        stderr = StringIO()
        code = main(
            [
                "query",
                "coffee",
                "--mode",
                "fts",
                "--limit",
                "4",
                "--filter",
                "kind=note",
                "--filter",
                "urgent=true",
                "--from",
                "2026-01-01",
                "--to",
                "2026-01-31",
                "--no-evidence",
                "--evidence-limit",
                "2",
                "--fts-weight",
                "0.8",
                "--semantic-weight",
                "0.2",
                "--explain",
                "--json",
            ],
            client=client,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["query"], "coffee")
        self.assertEqual(output["request"]["filters"], {"kind": "note", "urgent": True})
        request = client.calls[0][1]
        self.assertIsInstance(request, QueryRequest)
        self.assertEqual(request.query, "coffee")
        self.assertEqual(request.mode, "fts")
        self.assertEqual(request.limit, 4)
        self.assertEqual(dict(request.filters), {"kind": "note", "urgent": True})
        self.assertEqual(request.start, "2026-01-01")
        self.assertEqual(request.end, "2026-01-31")
        self.assertFalse(request.include_evidence)
        self.assertEqual(request.evidence_limit, 2)
        self.assertEqual(request.fts_weight, 0.8)
        self.assertEqual(request.semantic_weight, 0.2)
        self.assertTrue(request.explain)

    def test_purge_requires_destructive_confirmation_and_does_not_call_client(self):
        client = FakeClient()
        error = StringIO()
        self.assertEqual(
            main(
                ["purge", "--all", "--json"],
                client=client,
                stdout=StringIO(),
                stderr=error,
            ),
            2,
        )
        self.assertEqual(error.getvalue(), "contextctl: error: purge requires --yes confirmation\n")
        self.assertEqual(client.calls, [])

        dry_run_stdout = StringIO()
        dry_run_error = StringIO()
        self.assertEqual(
            main(
                ["purge", "--all", "--dry-run", "--json"],
                client=client,
                stdout=dry_run_stdout,
                stderr=dry_run_error,
            ),
            0,
        )
        self.assertEqual(dry_run_error.getvalue(), "")
        self.assertEqual(
            json.loads(dry_run_stdout.getvalue()),
            {
                "purge": {
                    "before": None,
                    "all": True,
                    "confirmed": False,
                    "dry_run": True,
                }
            },
        )
        self.assertEqual(
            client.calls,
            [
                (
                    "purge",
                    {"before": None, "all": True, "confirmed": False, "dry_run": True},
                )
            ],
        )

    def test_http_client_propagates_environment_bearer_token(self):
        requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return None

            def read(self):
                return b'{"state":"running"}'

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        with patch.dict(
            os.environ,
            {
                "CONTEXT_SERVER_URL": "https://context.test",
                "CONTEXT_SERVER_TOKEN": "",
                "CONTEXT_SERVER_AUTH_TOKEN": "cli-secret",
            },
            clear=False,
        ):
            with patch("apps.cli.client.urlopen", side_effect=fake_urlopen):
                stdout = StringIO()
                stderr = StringIO()
                code = main(["status", "--json"], stdout=stdout, stderr=stderr)

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), {"state": "running"})
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "https://context.test/v1/status")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer cli-secret")
        self.assertEqual(timeout, 15.0)

    def test_stale_html_endpoint_returns_process_error(self):
        class FakeResponse:
            headers = {"Content-Type": "text/html"}

            def read(self):
                return b"<html><body>dashboard</body></html>"

            def close(self):
                return None

        with patch("apps.cli.client.urlopen", return_value=FakeResponse()):
            stdout = StringIO()
            stderr = StringIO()
            code = main(
                ["health", "--base-url", "http://127.0.0.1:8000", "--json"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("non-JSON response", stderr.getvalue())
