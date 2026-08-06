from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from services.context_server.application import create_runtime_app
from services.context_server.config import Settings
from services.context_server.inference.fake import DeterministicFakeModelClient

QUESTION = "What have I been doing for the past 20 minutes?"
RECENT_TERMINAL_ID = "observation:acceptance-recent-terminal"
RECENT_GIT_ID = "observation:acceptance-recent-git"
OLD_CONTEXT_ID = "observation:acceptance-old-context"


@contextmanager
def _query_client(
    model_outputs: list[str],
) -> Iterator[tuple[TestClient, Any, DeterministicFakeModelClient]]:
    with TemporaryDirectory() as data_dir:
        app = create_runtime_app(
            Settings(
                data_dir=Path(data_dir),
                docs_enabled=False,
                model_endpoint="http://127.0.0.1:18080/v1",
                model_name="acceptance-query-model",
            )
        )
        fake_model = DeterministicFakeModelClient(
            model_outputs,
            model_name="acceptance-query-model",
        )
        with TestClient(app) as client:
            # Startup constructs the configured HTTP client, so replace it only
            # after lifespan startup to keep this acceptance path network/GPU-free.
            app.state.context_runtime.inference.client = fake_model
            yield client, app.state.context_runtime, fake_model


def _seed_activity(runtime: Any) -> None:
    now = datetime.now(UTC)
    observations = (
        {
            "event_id": OLD_CONTEXT_ID.removeprefix("observation:"),
            "source_id": "window:old-editor",
            "kind": "source_event:observed",
            "observed_at": (now - timedelta(minutes=35)).isoformat(),
            "text": "Edited an unrelated archived project that is outside the requested window.",
        },
        {
            "event_id": RECENT_TERMINAL_ID.removeprefix("observation:"),
            "source_id": "terminal:powershell-history",
            "kind": "source_event:observed",
            "observed_at": (now - timedelta(minutes=7)).isoformat(),
            "text": (
                '{"commands":["cd .\\\\life-context\\\\",'
                '"claude --permission-mode bypassPermissions",'
                '"pnpm --dir apps/dashboard test"]}'
            ),
        },
        {
            "event_id": RECENT_GIT_ID.removeprefix("observation:"),
            "source_id": "git:life-context",
            "kind": "source_event:observed",
            "observed_at": (now - timedelta(minutes=3)).isoformat(),
            "text": "Reviewed the Life Context dashboard query flow and its evidence citations.",
        },
    )
    for observation in observations:
        runtime.observe(
            {
                **observation,
                "payload": {"text": observation["text"]},
                "metadata": {"fixture": "recent-activity-query-flow"},
            }
        )


def _ask(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/query",
        json={
            "query": QUESTION,
            "mode": "hybrid",
            "limit": 8,
            "include_evidence": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_dashboard_evidence_contract(payload: dict[str, Any]) -> None:
    result_ids = {item["id"] for item in payload["results"]}
    evidence_ids = {item["id"] for item in payload["evidence"]}

    assert result_ids == {RECENT_TERMINAL_ID, RECENT_GIT_ID}
    assert evidence_ids == result_ids
    assert set(payload["answer"]["evidence_ids"]) == evidence_ids
    assert payload["meta"]["result_count"] == 2
    assert payload["meta"]["evidence_count"] == 2
    assert all(item["citation"].strip() for item in payload["evidence"])
    assert payload["answer_text"] == payload["answer"]["text"]
    assert OLD_CONTEXT_ID not in result_ids


def test_dashboard_query_api_honors_twenty_minute_window_and_returns_citations() -> None:
    human_answer = (
        "Over the past 20 minutes, you worked on the Life Context query experience: "
        "you ran dashboard tests from the project terminal [1] and reviewed the "
        "dashboard's evidence-citation flow [2]."
    )
    with _query_client([human_answer]) as (client, runtime, fake_model):
        _seed_activity(runtime)

        payload = _ask(client)

    _assert_dashboard_evidence_contract(payload)
    assert payload["answer"]["status"] == "model_synthesized"
    assert payload["answer"]["text"] == human_answer
    assert payload["answer"]["metadata"]["model_name"] == "acceptance-query-model"

    assert len(fake_model.calls) == 1
    model_prompt = fake_model.calls[0].messages[1]["content"]
    assert QUESTION in model_prompt
    assert "dashboard query flow" in model_prompt
    assert "apps/dashboard test" in model_prompt
    assert "unrelated archived project" not in model_prompt


def test_dashboard_query_api_humanizes_evidence_when_model_returns_empty_text() -> None:
    with _query_client([""]) as (client, runtime, fake_model):
        _seed_activity(runtime)

        payload = _ask(client)

    _assert_dashboard_evidence_contract(payload)
    answer = payload["answer"]
    assert answer["status"] == "fallback_evidence_summary"
    assert answer["metadata"]["model_error"] == "empty_response"
    assert "evidence-backed recap" in answer["text"]
    assert "projects life-context" in answer["text"]
    assert "tools Claude, pnpm" in answer["text"]
    assert "dashboard query flow" in answer["text"]
    assert '{"commands"' not in answer["text"]
    assert "--permission-mode" not in answer["text"]
    assert "[1]" in answer["text"] or "[2]" in answer["text"]
    assert len(fake_model.calls) == 1
