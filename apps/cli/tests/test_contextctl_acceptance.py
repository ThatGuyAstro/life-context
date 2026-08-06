from __future__ import annotations

import json
import os
import unittest
from io import StringIO
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from apps.cli.contextctl import main


class StubResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._raw

    def close(self) -> None:
        self.closed = True


class DeterministicHTTPStub:
    """A request recorder with stable responses for the public CLI routes."""

    def __init__(self, *, error: tuple[int, Any] | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, *, timeout: float) -> StubResponse:
        parsed = urlsplit(request.full_url)
        body = None
        if request.data:
            body = json.loads(bytes(request.data).decode("utf-8"))
        query = {
            key: values[-1]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "path": parsed.path,
                "query": query,
                "body": body,
                "headers": headers,
                "timeout": timeout,
            }
        )

        if self.error is not None:
            return StubResponse(*self.error)

        route = (request.get_method(), parsed.path)
        if route == ("POST", "/context/v1/start"):
            return StubResponse(200, {"started": True, "mode": body.get("mode")})
        if route == ("POST", "/context/v1/pause"):
            return StubResponse(200, {"paused": True})
        if route == ("GET", "/context/v1/status"):
            return StubResponse(200, {"state": "running", "ready": True})
        if route == ("GET", "/context/v1/timeline"):
            return StubResponse(
                200,
                {
                    "items": [{"id": "event-1", "source": "clipboard"}],
                    "filters": query,
                },
            )
        if route == ("POST", "/context/v1/query"):
            return StubResponse(
                200,
                {"results": [{"id": "hit-1", "text": "coffee"}], "request": body},
            )
        if route == ("GET", "/context/v1/config"):
            return StubResponse(
                200,
                {"retention": {"raw_hours": 48, "keyframe_days": 14}},
            )
        if route == ("POST", "/context/v1/config"):
            return StubResponse(200, {"updated": body})
        if route == ("POST", "/context/v1/purge"):
            return StubResponse(200, {"purged": 2, "request": body})
        raise AssertionError(f"unexpected stub route: {route} {request.full_url}")


class TextResultClient:
    def status(self) -> str:
        return "running"


class ContextCtlAcceptanceTests(unittest.TestCase):
    def invoke_http(
        self,
        argv: list[str],
        stub: DeterministicHTTPStub,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        env = {
            "CONTEXT_SERVER_URL": "https://env.example/context",
            "CONTEXT_SERVER_TOKEN": "",
            "CONTEXT_SERVER_AUTH_TOKEN": "fallback-token",
            "CUDA_VISIBLE_DEVICES": "",
            "NVIDIA_VISIBLE_DEVICES": "",
        }
        env.update(environment or {})
        with patch.dict(os.environ, env, clear=False):
            # This patch is the network boundary for the entire acceptance suite.
            with patch("apps.cli.client.urlopen", side_effect=stub):
                code = main(argv, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_start_pause_and_status_return_zero_and_json(self) -> None:
        cases = (
            (["start", "--mode", "resume", "--json"], {"started": True, "mode": "resume"}),
            (["pause", "--json"], {"paused": True}),
            (["status", "--json"], {"state": "running", "ready": True}),
        )

        for argv, expected_output in cases:
            with self.subTest(command=argv[0]):
                stub = DeterministicHTTPStub()
                code, stdout, stderr = self.invoke_http(argv, stub)

                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout), expected_output)
                self.assertEqual(len(stub.calls), 1)
                self.assertEqual(stub.calls[0]["path"], f"/context/v1/{argv[0]}")
                self.assertEqual(stub.calls[0]["timeout"], 15.0)

        self.assertEqual(stub.calls[0]["headers"]["authorization"], "Bearer fallback-token")

    def test_timeline_and_query_preserve_cli_filters_and_emit_json(self) -> None:
        timeline_stub = DeterministicHTTPStub()
        timeline_code, timeline_stdout, timeline_stderr = self.invoke_http(
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
                "--pretty",
            ],
            timeline_stub,
        )

        self.assertEqual(timeline_code, 0)
        self.assertEqual(timeline_stderr, "")
        self.assertTrue(timeline_stdout.startswith("{\n  "))
        self.assertEqual(
            json.loads(timeline_stdout),
            {
                "items": [{"id": "event-1", "source": "clipboard"}],
                "filters": {
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-02T00:00:00Z",
                    "limit": "3",
                    "subject": "alice",
                    "kind": "note",
                },
            },
        )
        self.assertEqual(
            timeline_stub.calls[0]["query"],
            {
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-02T00:00:00Z",
                "limit": "3",
                "subject": "alice",
                "kind": "note",
            },
        )

        query_stub = DeterministicHTTPStub()
        query_code, query_stdout, query_stderr = self.invoke_http(
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
            query_stub,
        )
        expected_request = {
            "query": "coffee",
            "limit": 4,
            "mode": "fts",
            "filters": {"kind": "note", "urgent": True},
            "start": "2026-01-01",
            "end": "2026-01-31",
            "fts_weight": 0.8,
            "semantic_weight": 0.2,
            "include_evidence": False,
            "evidence_limit": 2,
            "explain": True,
        }
        self.assertEqual(query_code, 0)
        self.assertEqual(query_stderr, "")
        self.assertEqual(
            json.loads(query_stdout),
            {"results": [{"id": "hit-1", "text": "coffee"}], "request": expected_request},
        )
        self.assertEqual(query_stub.calls[0]["body"], expected_request)

    def test_config_retention_and_purge_source_project_controls_round_trip(self) -> None:
        retention = {"raw_hours": 48, "keyframe_days": 14}
        config_set_stub = DeterministicHTTPStub()
        code, stdout, stderr = self.invoke_http(
            ["config", "--set", "retention", json.dumps(retention), "--json"],
            config_set_stub,
        )
        expected_config_set = {
            "operation": "set",
            "key": "retention",
            "value": retention,
        }
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"updated": expected_config_set})
        self.assertEqual(config_set_stub.calls[0]["method"], "POST")
        self.assertEqual(config_set_stub.calls[0]["body"], expected_config_set)

        config_get_stub = DeterministicHTTPStub()
        code, stdout, stderr = self.invoke_http(
            ["config", "--get", "retention", "--json"], config_get_stub
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"retention": retention})
        self.assertEqual(config_get_stub.calls[0]["method"], "GET")
        self.assertEqual(
            config_get_stub.calls[0]["query"], {"operation": "get", "key": "retention"}
        )

        purge_stub = DeterministicHTTPStub()
        purge_request = {
            "before": "2026-01-01T00:00:00Z",
            "all": False,
            "confirmed": True,
            "dry_run": False,
            "source": "clipboard",
            "project": "life-context",
        }
        code, stdout, stderr = self.invoke_http(
            [
                "purge",
                "--before",
                purge_request["before"],
                "--source",
                purge_request["source"],
                "--project",
                purge_request["project"],
                "--yes",
                "--json",
            ],
            purge_stub,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"purged": 2, "request": purge_request})
        self.assertEqual(purge_stub.calls[0]["method"], "POST")
        self.assertEqual(purge_stub.calls[0]["body"], purge_request)

        dry_run_stub = DeterministicHTTPStub()
        code, stdout, stderr = self.invoke_http(
            [
                "purge",
                "--all",
                "--source",
                "window",
                "--project",
                "demo",
                "--dry-run",
                "--json",
            ],
            dry_run_stub,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            dry_run_stub.calls[0]["body"],
            {
                "before": None,
                "all": True,
                "confirmed": False,
                "dry_run": True,
                "source": "window",
                "project": "demo",
            },
        )

    def test_explicit_base_url_overrides_environment_and_auth_precedence_is_stable(self) -> None:
        stub = DeterministicHTTPStub()
        code, stdout, stderr = self.invoke_http(
            [
                "status",
                "--base-url",
                "https://cli.example/context/v1/",
                "--json",
            ],
            stub,
            environment={
                "CONTEXT_SERVER_URL": "https://env.example/should-not-be-used",
                "CONTEXT_SERVER_TOKEN": "primary-token",
                "CONTEXT_SERVER_AUTH_TOKEN": "fallback-token",
            },
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {"state": "running", "ready": True})
        self.assertEqual(stub.calls[0]["url"], "https://cli.example/context/v1/status")
        self.assertEqual(stub.calls[0]["headers"]["authorization"], "Bearer primary-token")

    def test_text_output_and_hardware_disabled_path_need_no_external_runtime(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with patch.dict(
            os.environ,
            {"CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": ""},
            clear=False,
        ):
            with patch(
                "apps.cli.client.urlopen",
                side_effect=AssertionError("text-only acceptance path attempted network I/O"),
            ) as opener:
                code = main(
                    ["status"],
                    client=TextResultClient(),
                    stdout=stdout,
                    stderr=stderr,
                )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "running\n")
        self.assertEqual(stderr.getvalue(), "")
        opener.assert_not_called()

    def test_purge_requires_confirmation_and_http_failures_return_exit_two(self) -> None:
        refusal_stub = DeterministicHTTPStub()
        code, stdout, stderr = self.invoke_http(["purge", "--all", "--json"], refusal_stub)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "contextctl: error: purge requires --yes confirmation\n")
        self.assertEqual(refusal_stub.calls, [])

        error_stub = DeterministicHTTPStub(error=(503, {"detail": "busy"}))
        code, stdout, stderr = self.invoke_http(["status", "--json"], error_stub)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "contextctl: error: context server returned HTTP 503: busy\n",
        )
        self.assertEqual(error_stub.calls[0]["headers"]["authorization"], "Bearer fallback-token")


if __name__ == "__main__":
    unittest.main()
