"""Repeated local dashboard stability smoke using the installed Chromium fallback.

This runner is intentionally narrower than ``dashboard_smoke.py``: it exercises
the same-origin Vite proxy repeatedly and fails on blank renders, framework
error overlays, page errors, failed browser requests, HTTP API failures, direct
cross-origin API calls, or a timeline that has gone stale after a reload.

Seed deterministic data first with ``dashboard_smoke.py`` (or provide an
equivalent marker already present in the API), then run:

    python tests/browser/dashboard_stability_smoke.py \
        --dashboard-url http://127.0.0.1:5173 \
        --expected-text dashboard-smoke-marker

The script never starts or stops application services. That makes it safe to
run against a developer's local site while keeping lifecycle ownership with the
caller or a separate harness.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from dashboard_smoke import installed_chromium


BROWSER_TIMEOUT_MS = 12_000
DEFAULT_RELOADS = 3
EXPECTED_API_PATHS = {"/api/status", "/api/timeline"}
ERROR_OVERLAY_SELECTORS = (
    "[data-nextjs-dialog]",
    ".vite-error-overlay",
    "#webpack-dev-server-client-overlay",
)


def _api_paths(requests: list[tuple[str, str]], dashboard_origin: str) -> set[str]:
    paths: set[str] = set()
    for _method, raw_url in requests:
        parsed = urlparse(raw_url)
        if parsed.path.startswith("/api/"):
            if parsed.netloc != dashboard_origin:
                raise AssertionError(
                    f"dashboard API request bypassed same-origin proxy: {raw_url}"
                )
            paths.add(parsed.path)
    return paths


def _assert_rendered(page: Page, expected_text: str, cycle: int) -> None:
    body = page.locator("body").inner_text(timeout=2_000).strip()
    if len(body) < 80:
        raise AssertionError(f"reload {cycle}: page body is blank or unexpectedly short")

    overlay = page.locator(", ".join(ERROR_OVERLAY_SELECTORS))
    if overlay.count():
        raise AssertionError(f"reload {cycle}: framework error overlay is visible")

    page.get_by_role("heading", name="Your context, in focus.", exact=True).wait_for()
    page.get_by_role("heading", name="Context timeline", exact=True).wait_for()
    page.get_by_text("API connected", exact=True).wait_for()

    if expected_text not in body:
        raise AssertionError(
            f"reload {cycle}: expected fresh API marker {expected_text!r} was absent"
        )

    timeline_items = page.locator("article.timeline-item")
    if timeline_items.count() == 0:
        raise AssertionError(f"reload {cycle}: timeline rendered no items")


def run_browser(dashboard_url: str, expected_text: str, reloads: int) -> None:
    parsed_dashboard = urlparse(dashboard_url)
    if parsed_dashboard.scheme not in {"http", "https"} or not parsed_dashboard.netloc:
        raise ValueError("dashboard URL must be an absolute HTTP(S) URL")
    if reloads < 1:
        raise ValueError("reload count must be at least 1")

    requests: list[tuple[str, str]] = []
    failed_requests: list[str] = []
    http_errors: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=installed_chromium(playwright),
            headless=True,
            args=["--disable-gpu"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.set_default_timeout(BROWSER_TIMEOUT_MS)
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.on("requestfailed", lambda request: failed_requests.append(request.url))
        page.on(
            "response",
            lambda response: (
                http_errors.append(f"{response.status} {response.url}")
                if response.url.startswith(f"{parsed_dashboard.scheme}://{parsed_dashboard.netloc}/api/")
                and response.status >= 400
                else None
            ),
        )
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        try:
            for cycle in range(1, reloads + 1):
                start_request_count = len(requests)
                if cycle == 1:
                    page.goto(dashboard_url, wait_until="networkidle", timeout=BROWSER_TIMEOUT_MS)
                else:
                    page.reload(wait_until="networkidle", timeout=BROWSER_TIMEOUT_MS)

                _assert_rendered(page, expected_text, cycle)
                cycle_paths = _api_paths(requests[start_request_count:], parsed_dashboard.netloc)
                missing = EXPECTED_API_PATHS - cycle_paths
                if missing:
                    raise AssertionError(
                        f"reload {cycle}: same-origin proxy requests missing: {sorted(missing)}"
                    )

            if failed_requests:
                raise AssertionError(f"browser requests failed: {failed_requests[:10]}")
            if http_errors:
                raise AssertionError(f"proxy/API responses failed: {http_errors[:10]}")
            if page_errors:
                raise AssertionError(f"page errors: {page_errors[:10]}")
            if console_errors:
                raise AssertionError(f"console errors: {console_errors[:10]}")
        except Exception as error:
            try:
                body = page.locator("body").inner_text(timeout=2_000)
            except Exception:
                body = "<body unavailable>"
            detail = f"\nPage text:\n{body[:2_000]}"
            if console_errors:
                detail += f"\nConsole errors:\n{console_errors[:10]}"
            if page_errors:
                detail += f"\nPage errors:\n{page_errors[:10]}"
            raise AssertionError(f"dashboard stability smoke failed: {error}{detail}") from error
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-url", required=True, help="Dashboard origin URL")
    parser.add_argument(
        "--expected-text",
        required=True,
        help="Fresh marker that must be visible after every load/reload",
    )
    parser.add_argument(
        "--reloads",
        type=int,
        default=DEFAULT_RELOADS,
        help=f"Number of page loads to verify (default: {DEFAULT_RELOADS})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run_browser(args.dashboard_url, args.expected_text, args.reloads)
        print(
            f"dashboard_stability=PASS reloads={args.reloads} "
            "nonblank+no-overlays+no-console-errors+proxy+fresh-marker"
        )
    except Exception as error:
        print(f"dashboard_stability=FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
