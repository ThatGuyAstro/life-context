from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.context_server.application import create_runtime_app
from services.context_server.config import Settings


def _record_activity(
    runtime: object,
    *,
    event_id: str,
    source_id: str,
    observed_at: datetime,
    text: str,
) -> None:
    runtime.observe(  # type: ignore[attr-defined]
        {
            "event_id": event_id,
            "source_id": source_id,
            "kind": "source_event:observed",
            "observed_at": observed_at.isoformat(),
            "text": text,
            "payload": {"text": text},
            "metadata": {},
        }
    )


@pytest.mark.parametrize(
    ("question", "recent_age", "stale_age"),
    (
        (
            "What have I been doing for the past 20 minutes?",
            timedelta(minutes=5),
            timedelta(minutes=45),
        ),
        (
            "What have I been doing for the last 2 hours?",
            timedelta(minutes=30),
            timedelta(hours=3),
        ),
        (
            "Review the last 20 minutes of activity.",
            timedelta(minutes=5),
            timedelta(minutes=45),
        ),
    ),
    ids=("past-minutes", "last-hours", "imperative-review"),
)
def test_query_api_honors_relative_window_and_returns_cited_readable_prose(
    tmp_path: Path,
    question: str,
    recent_age: timedelta,
    stale_age: timedelta,
) -> None:
    app = create_runtime_app(
        Settings(data_dir=tmp_path, docs_enabled=False, model_endpoint="")
    )
    runtime = app.state.context_runtime
    now = datetime.now(UTC)
    raw_terminal_json = json.dumps(
        {
            "commands": [
                r"cd .\life-context",
                "claude --permission-mode bypassPermissions",
                "pnpm test",
            ]
        }
    )
    _record_activity(
        runtime,
        event_id="recent-terminal",
        source_id="terminal:history",
        observed_at=now - recent_age,
        text=raw_terminal_json,
    )
    _record_activity(
        runtime,
        event_id="stale-private-project",
        source_id="window:stale",
        observed_at=now - stale_age,
        text="Worked on the stale-private-project migration.",
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/query",
            json={
                "query": question,
                "mode": "hybrid",
                "limit": 8,
                "include_evidence": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    answer_text = body["answer_text"]

    assert isinstance(answer_text, str)
    assert answer_text.startswith("I found ")
    assert "evidence-backed recap" in answer_text
    assert "terminal-history snapshot" in answer_text
    assert "projects life-context" in answer_text
    assert "tools Claude, pnpm" in answer_text
    assert "[1]" in answer_text

    assert raw_terminal_json not in answer_text
    assert '{"commands"' not in answer_text
    assert "stale-private-project" not in answer_text
    with pytest.raises(json.JSONDecodeError):
        json.loads(answer_text)

    result_ids = {item["id"] for item in body["results"]}
    evidence_ids = [item["id"] for item in body["evidence"]]
    assert result_ids == {"observation:recent-terminal"}
    assert evidence_ids == ["observation:recent-terminal"]
    assert body["answer"]["evidence_ids"] == evidence_ids


def test_query_api_answer_text_is_not_the_raw_json_evidence_field(tmp_path: Path) -> None:
    app = create_runtime_app(
        Settings(data_dir=tmp_path, docs_enabled=False, model_endpoint="")
    )
    runtime = app.state.context_runtime
    raw_context = (
        '{"commands":["cd .\\\\network-optimizer-bo7\\\\","ls",'
        '"claude --dangerously-skip-permissions"]}'
    )
    _record_activity(
        runtime,
        event_id="terminal-json",
        source_id="terminal:history",
        observed_at=datetime.now(UTC) - timedelta(minutes=2),
        text=raw_context,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/query",
            json={
                "query": "Summarize what I did in the past 10 minutes.",
                "mode": "hybrid",
                "include_evidence": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["evidence"][0]["text"] == raw_context
    assert body["answer_text"] != raw_context
    assert "network-optimizer-bo7" in body["answer_text"]
    assert "tools Claude" in body["answer_text"]
    assert body["answer_text"].endswith("[1]")
    assert body["answer"]["evidence_ids"] == [body["evidence"][0]["id"]]
