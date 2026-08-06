"""Bounded live dashboard smoke check using the installed Python Playwright package.

The API is seeded through its existing HTTP ingestion and artifact endpoints so
the browser pass is deterministic without touching the server or collector.

Example:
    python tests/browser/dashboard_smoke.py \
        --api-url http://127.0.0.1:8000 \
        --dashboard-url "http://127.0.0.1:5173/?api=http://127.0.0.1:8000"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

API_TIMEOUT = 8.0
BROWSER_TIMEOUT_MS = 12_000
QUERY_TEXT = "dashboard-smoke-marker"


@dataclass(frozen=True, slots=True)
class SeedEvent:
    event_id: str
    source_id: str
    source_kind: str
    occurred_at: str
    text: str
    artifact_body: bytes


SEED_EVENTS = (
    SeedEvent(
        event_id="dashboard-smoke-event-001",
        source_id="smoke:messages",
        source_kind="message",
        occurred_at="2026-08-05T20:00:00Z",
        text="dashboard-smoke-marker: Launch checkpoint.",
        artifact_body=b"dashboard-smoke-artifact-launch-001",
    ),
    SeedEvent(
        event_id="dashboard-smoke-event-002",
        source_id="smoke:calendar",
        source_kind="calendar",
        occurred_at="2026-08-05T19:00:00Z",
        text="dashboard-smoke-secondary: Calendar review.",
        artifact_body=b"dashboard-smoke-artifact-calendar-002",
    ),
    SeedEvent(
        event_id="dashboard-smoke-event-003",
        source_id="smoke:documents",
        source_kind="file",
        occurred_at="2026-08-05T18:00:00Z",
        text="dashboard-smoke-secondary: Task handoff.",
        artifact_body=b"dashboard-smoke-artifact-document-003",
    ),
)


def api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def artifact_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def seed_api(base_url: str) -> str:
    """Upload deterministic artifacts and ingest one deterministic envelope."""

    with httpx.Client(timeout=API_TIMEOUT, headers={"Accept": "application/json"}) as client:
        health = client.get(api_url(base_url, "/health"))
        health.raise_for_status()
        if health.json().get("status") != "ok":
            raise AssertionError(f"API health was not ok: {health.text}")

        refs: list[dict[str, Any]] = []
        for event in SEED_EVENTS:
            digest = artifact_digest(event.artifact_body)
            response = client.put(
                api_url(base_url, f"/artifacts/{digest}"),
                content=event.artifact_body,
                headers={
                    "Content-Type": "text/plain",
                    "X-Artifact-SHA256": digest,
                    "X-Artifact-Metadata": json.dumps(
                        {"retention_tier": "raw", "evidence_label": event.event_id}
                    ),
                },
            )
            if response.status_code not in {200, 201}:
                raise AssertionError(
                    f"artifact upload failed: {response.status_code} {response.text}"
                )
            refs.append(
                {
                    "artifact_id": digest,
                    "kind": "text",
                    "media_type": "text/plain",
                    "size_bytes": len(event.artifact_body),
                    "sha256": digest,
                    "metadata": {"role": "dashboard-browser-smoke"},
                }
            )

        captured_at = "2026-08-05T20:01:00Z"
        envelope = {
            "envelope_id": "dashboard-smoke-envelope-001",
            "schema_version": "1",
            "captured_at": captured_at,
            "source_kind": "application",
            "producer": "dashboard-browser-smoke",
            "metadata": {"smoke": "dashboard", "deterministic": True},
            "events": [
                {
                    "event_id": event.event_id,
                    "source_id": event.source_id,
                    "source_kind": event.source_kind,
                    "event_type": "observed",
                    "occurred_at": event.occurred_at,
                    "captured_at": captured_at,
                    "payload": {"text": event.text},
                    "artifacts": [refs[index]],
                    "metadata": {"name": event.text.split(":", 1)[-1].rstrip(".")},
                }
                for index, event in enumerate(SEED_EVENTS)
            ],
        }
        response = client.post(
            api_url(base_url, "/ingest/batches"),
            json={"records": [envelope]},
            headers={"Idempotency-Key": "dashboard-smoke-batch-001"},
        )
        if response.status_code != 202:
            raise AssertionError(f"ingest failed: {response.status_code} {response.text}")
        result = response.json()
        if result.get("accepted") != len(SEED_EVENTS):
            raise AssertionError(f"unexpected ingest result: {result}")

        timeline = client.get(api_url(base_url, "/timeline?limit=50"))
        timeline.raise_for_status()
        ids = {item.get("id") for item in timeline.json().get("items", [])}
        expected_ids = {f"observation:{event.event_id}" for event in SEED_EVENTS}
        if not expected_ids.issubset(ids):
            raise AssertionError(f"seeded observations missing from timeline: {expected_ids - ids}")

    return artifact_digest(SEED_EVENTS[0].artifact_body)


def installed_chromium(playwright: Any) -> str:
    expected = Path(playwright.chromium.executable_path)
    if expected.is_file():
        return str(expected)
    browser_root = Path.home() / "AppData" / "Local" / "ms-playwright"
    candidates = sorted(browser_root.glob("chromium-*/chrome-win*/chrome.exe"))
    if not candidates:
        raise RuntimeError(f"no installed Chromium executable found under {browser_root}")
    return str(candidates[-1])


def run_browser(dashboard_url: str, expected_digest: str) -> None:
    requests: list[tuple[str, str]] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=installed_chromium(playwright),
            headless=True,
            args=["--disable-gpu"],
        )
        page = browser.new_page(
            accept_downloads=True,
            viewport={"width": 1440, "height": 1100},
        )
        page.set_default_timeout(BROWSER_TIMEOUT_MS)
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )

        try:
            page.goto(dashboard_url, wait_until="networkidle", timeout=BROWSER_TIMEOUT_MS)
            page.get_by_role("heading", name="Your context, in focus.", exact=True).wait_for()
            page.get_by_text("API connected", exact=True).wait_for()
            page.get_by_role("heading", name="Context timeline", exact=True).wait_for()

            timeline_items = page.locator("article.timeline-item")
            if timeline_items.count() != len(SEED_EVENTS):
                raise AssertionError(
                    f"expected {len(SEED_EVENTS)} timeline items, got {timeline_items.count()}"
                )
            for event in SEED_EVENTS:
                timeline_items.filter(has_text=event.text).wait_for()

            indexed_events = (
                page.locator(".metric-card")
                .filter(has_text="Indexed events")
                .locator("strong")
            )
            if indexed_events.inner_text() != str(len(SEED_EVENTS)):
                raise AssertionError(
                    f"unexpected indexed event count: {indexed_events.inner_text()!r}"
                )

            query = page.locator("#context-query")
            query.fill(QUERY_TEXT)
            page.get_by_role("button", name="Ask context", exact=True).click()
            page.locator(".answer-text").filter(
                has_text=re.compile(r"Found 1 matching context item")
            ).wait_for()

            evidence_link = page.locator(".evidence-list a")
            if evidence_link.count() != 1:
                raise AssertionError(
                    f"expected one query evidence link, got {evidence_link.count()}"
                )
            href = evidence_link.get_attribute("href") or ""
            expected_suffix = f"/v1/artifacts/{expected_digest}"
            if not href.endswith(expected_suffix):
                raise AssertionError(f"unexpected evidence href: {href!r}")
            if evidence_link.get_attribute("target") != "_blank":
                raise AssertionError("evidence link did not open in a new tab")

            with page.expect_download(timeout=BROWSER_TIMEOUT_MS) as download_info:
                evidence_link.click()
            download = download_info.value
            if download.failure() is not None:
                raise AssertionError(f"evidence download failed: {download.failure()}")
            if download.suggested_filename != expected_digest:
                raise AssertionError(
                    f"unexpected evidence filename: {download.suggested_filename!r}"
                )
            downloaded_path = download.path()
            if (
                downloaded_path is None
                or Path(downloaded_path).read_bytes() != SEED_EVENTS[0].artifact_body
            ):
                raise AssertionError("evidence download did not contain the seeded artifact")

            request_paths = [(method, urlparse(url).path) for method, url in requests]
            if not any(path.endswith("/status") for _, path in request_paths):
                raise AssertionError("browser did not request API status")
            if not any(path.endswith("/timeline") for _, path in request_paths):
                raise AssertionError("browser did not request API timeline")
            if not any(
                method == "POST" and path.endswith("/query")
                for method, path in request_paths
            ):
                raise AssertionError("browser did not submit a POST query")
        except Exception as error:
            body = page.locator("body").inner_text(timeout=2_000)
            detail = f"\nPage text:\n{body[:2_000]}"
            if console_errors:
                detail += f"\nConsole errors:\n{console_errors[:10]}"
            raise AssertionError(f"dashboard browser smoke failed: {error}{detail}") from error
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, help="API origin, without /v1")
    parser.add_argument(
        "--dashboard-url",
        required=True,
        help="Dashboard URL, including its API configuration",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        digest = seed_api(args.api_url)
        print(f"seeded={len(SEED_EVENTS)} artifact={digest}")
        run_browser(args.dashboard_url, digest)
        print("browser_smoke=PASS query=status+timeline+evidence-link")
    except Exception as error:
        print(f"browser_smoke=FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
