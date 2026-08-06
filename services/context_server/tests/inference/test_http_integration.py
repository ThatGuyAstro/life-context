from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any

from services.context_server.inference import (
    InferenceJob,
    InferenceWorker,
    InMemoryJobAdapter,
    JobStatus,
    JsonSchemaContract,
    ModelClientConfig,
    RetryPolicy,
    build_model_client,
    build_strict_json_prompt,
)


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    body: str
    headers: Mapping[str, str]


class _FakeOpenAIServer:
    """Small deterministic loopback server for the real urllib transport."""

    def __init__(
        self,
        responses: Sequence[_HttpResponse],
        *,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self._responses = deque(responses)
        self._capabilities = dict(capabilities) if capabilities is not None else None
        self._lock = Lock()
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> _FakeOpenAIServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if owner._capabilities is None:
                    self.send_error(404)
                    return
                with owner._lock:
                    owner.requests.append(
                        {
                            "path": self.path,
                            "headers": {
                                str(key).lower(): str(value) for key, value in self.headers.items()
                            },
                            "body": None,
                        }
                    )
                payload = json.dumps(owner._capabilities, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                with owner._lock:
                    owner.requests.append(
                        {
                            "path": self.path,
                            "headers": {
                                str(key).lower(): str(value) for key, value in self.headers.items()
                            },
                            "body": body,
                        }
                    )
                    response = (
                        owner._responses.popleft()
                        if owner._responses
                        else _HttpResponse(500, '{"error":"unexpected request"}', {})
                    )

                payload = response.body.encode("utf-8")
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler


def _completion_response(
    *, index: int, content: str, status: int = 200, headers: Mapping[str, str] | None = None
) -> _HttpResponse:
    body = {
        "id": f"chatcmpl-{index:04d}",
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    response_headers = {"Content-Type": "application/json"}
    if headers:
        response_headers.update(headers)
    if status == 200:
        response_headers["X-Request-ID"] = f"server-request-{index:04d}"
    return _HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        headers=response_headers,
    )


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_openai_http_server_runs_strict_inference_with_retry_and_provenance() -> None:
    responses = [
        _HttpResponse(
            status=503,
            body='{"error":{"message":"temporary overload"}}',
            headers={"Content-Type": "application/json", "Retry-After": "0"},
        ),
        _completion_response(index=2, content='Here is the answer: {"summary":"wrong"}'),
        _completion_response(index=3, content='{"summary":"too confident","confidence":1.2}'),
        _completion_response(
            index=4, content='{"confidence":0.93,"summary":"editing a Python module"}'
        ),
    ]
    contract = JsonSchemaContract(
        name="context_observation",
        version="1",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["summary", "confidence"],
        },
    )
    clock = _Clock()

    with _FakeOpenAIServer(responses) as server:
        config = ModelClientConfig(
            name="mock-local",
            model="mock-model",
            base_url=server.base_url,
            mode="external",
            api_key="integration-secret",
            timeout_s=2.0,
            model_revision="mock-revision-1",
        )
        client = build_model_client(config)
        prompt = build_strict_json_prompt(
            "Describe the current context.",
            contract,
            context={"capture_id": "capture-001", "window": "editor"},
        )
        adapter = InMemoryJobAdapter()
        adapter.enqueue(
            InferenceJob(
                "job-http-1",
                prompt,
                model="mock-local",
                created_at=clock(),
                max_attempts=4,
            )
        )
        worker = InferenceWorker(
            adapter,
            {"mock-local": client},
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=4, base_delay_s=0, max_delay_s=0),
            random_fn=lambda: 0.5,
        )

        first = worker.run_once(now=clock())
        second = worker.run_once(now=clock())
        third = worker.run_once(now=clock())
        fourth = worker.run_once(now=clock())

        assert first.retried == 1
        assert first.failures[0].code == "model_http_503"
        assert second.retried == 1
        assert second.failures[0].code == "json_extraction"
        assert third.retried == 1
        assert third.failures[0].code == "json_schema_validation"
        assert fourth.succeeded == 1

        snapshot = adapter.get("job-http-1")
        assert snapshot.status is JobStatus.SUCCEEDED
        assert snapshot.result is not None
        assert snapshot.result.value == {
            "confidence": 0.93,
            "summary": "editing a Python module",
        }
        assert snapshot.result.raw_text == '{"confidence":0.93,"summary":"editing a Python module"}'

        provenance = snapshot.result.provenance
        assert provenance.request_id == "job-http-1"
        assert provenance.attempt == 4
        assert provenance.prompt.prompt_hash == prompt.prompt_hash
        assert provenance.prompt.schema_hash == contract.schema_hash
        assert provenance.model.client_name == "mock-local"
        assert provenance.model.mode == "external"
        assert provenance.model.configured_model == "mock-model"
        assert provenance.model.model_revision == "mock-revision-1"
        assert provenance.model.returned_model == "mock-model"
        assert provenance.model.endpoint == config.safe_base_url
        assert provenance.model.response_id == "chatcmpl-0004"
        assert provenance.model.request_id == "server-request-0004"
        assert provenance.model.usage == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }

        assert [request["path"] for request in server.requests] == [
            "/v1/chat/completions"
        ] * 4
        assert [request["body"]["model"] for request in server.requests] == [
            "mock-model"
        ] * 4
        assert all(
            request["headers"]["authorization"] == "Bearer integration-secret"
            and request["headers"]["x-request-id"] == "job-http-1"
            and request["body"]["stream"] is False
            and request["body"]["response_format"]["type"] == "json_schema"
            and request["body"]["response_format"]["json_schema"]["strict"] is True
            and request["body"]["response_format"]["json_schema"]["schema"]
            == contract.schema
            for request in server.requests
        )

    assert worker.metrics().succeeded == 1
    assert worker.counters.failed == 3
    assert worker.metrics().dead_lettered == 0
    assert adapter.events[-1][1] == JobStatus.SUCCEEDED.value


def test_openai_http_server_auto_selects_json_object_without_schema_support() -> None:
    contract = JsonSchemaContract(
        name="context_observation",
        version="1",
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )

    with _FakeOpenAIServer(
        [_completion_response(index=1, content='{"summary":"editing"}')],
        capabilities={
            "data": [
                {
                    "id": "mock-model",
                    "capabilities": {
                        "supports_json_object": True,
                        "supports_json_schema": False,
                    },
                }
            ]
        },
    ) as server:
        client = build_model_client(
            ModelClientConfig(
                name="mock-local",
                model="mock-model",
                base_url=server.base_url,
                structured_output_mode="auto",
            )
        )
        response = client.complete(build_strict_json_prompt("Describe the context.", contract))

        assert response.text == '{"summary":"editing"}'
        assert [request["path"] for request in server.requests] == [
            "/v1/models",
            "/v1/chat/completions",
        ]
        assert server.requests[1]["body"]["response_format"] == {"type": "json_object"}
        assert client.provenance_metadata["structured_output_mode"] == "json_object"
