"""Bounded browser assertions for the dashboard settings/privacy surface.

This intentionally complements, rather than changes, ``dashboard_smoke.py``.
Run it against the same live server/dashboard pair after the existing smoke has
seeded deterministic timeline data.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from dashboard_smoke import installed_chromium


BROWSER_TIMEOUT_MS = 12_000


def run_browser(dashboard_url: str) -> None:
    requests: list[tuple[str, str]] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=installed_chromium(playwright),
            headless=True,
            args=["--disable-gpu"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
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
            page.get_by_role("heading", name="Keep your context yours.", exact=True).wait_for()

            warning = page.locator(".plaintext-warning")
            warning_text = warning.inner_text()
            if "Plaintext warning" not in warning_text:
                raise AssertionError("plaintext warning heading was not visible")
            if "Raw clipboard text and screenshots are stored as plaintext" not in warning_text:
                raise AssertionError("plaintext warning did not describe raw archive storage")
            if "Clipboard is local-only by default" not in warning_text:
                raise AssertionError("plaintext warning did not state clipboard local-only routing")
            if page.get_by_role("checkbox", name="I understand").is_checked():
                raise AssertionError("plaintext warning acknowledgement was unexpectedly checked")

            source_card = page.locator(".source-routing-card")
            source_card.get_by_role("heading", name="Source routing", exact=True).wait_for()
            clipboard_route = page.get_by_label("Clipboard route")
            if clipboard_route.input_value() != "local":
                raise AssertionError(
                    f"clipboard route was not local-only: {clipboard_route.input_value()!r}"
                )
            if page.get_by_label("Screenshots route").input_value() != "local":
                raise AssertionError("screenshots route did not default to local-only")

            retention = page.locator(".retention-card")
            expected_retention = {
                "Context events retention": "365",
                "Transcripts retention": "30",
                "Raw payloads retention": "1",
                "Raw retention hours": "24",
                "Keyframe retention days": "30",
            }
            for label, expected in expected_retention.items():
                field = retention.get_by_label(label)
                if field.input_value() != expected:
                    raise AssertionError(
                        f"{label} was {field.input_value()!r}, expected {expected!r}"
                    )

            runtime = page.locator(".runtime-card")
            runtime.get_by_role("heading", name="Worker and model", exact=True).wait_for()
            expected_fields = {
                "Worker count": "1",
                "Queue capacity": "1000",
                "Model endpoint": "",
                "Model name": "dashboard-smoke-model",
                "Model revision": "dashboard-smoke-rev",
            }
            for label, expected in expected_fields.items():
                field = runtime.get_by_label(label)
                if field.input_value() != expected:
                    raise AssertionError(
                        f"{label} was {field.input_value()!r}, expected {expected!r}"
                    )
            worker_status = runtime.locator(".runtime-status-line").filter(has_text="Workers")
            model_status = runtime.locator(".runtime-status-line").filter(has_text="Model")
            if "1" not in worker_status.inner_text() or "Not Configured" not in worker_status.inner_text():
                raise AssertionError(f"unexpected worker status: {worker_status.inner_text()!r}")
            if "Not Configured" not in model_status.inner_text():
                raise AssertionError(f"unexpected model status: {model_status.inner_text()!r}")

            dashboard_origin = urlparse(dashboard_url).netloc
            parsed_requests = [(method, urlparse(url)) for method, url in requests]
            api_requests = [
                (method, parsed)
                for method, parsed in parsed_requests
                if parsed.path.startswith("/api/")
            ]
            required_api_requests = {
                ("GET", "/api/status"),
                ("GET", "/api/timeline"),
                ("GET", "/api/config"),
            }
            seen_api_requests = {(method, parsed.path) for method, parsed in api_requests}
            missing = required_api_requests - seen_api_requests
            if missing:
                raise AssertionError(f"same-origin API proxy requests missing: {sorted(missing)}")
            if any(parsed.netloc != dashboard_origin for _, parsed in api_requests):
                raise AssertionError("browser API request bypassed the dashboard origin")
            if not api_requests:
                raise AssertionError("browser made no /api proxy requests")
        except Exception as error:
            try:
                body = page.locator("body").inner_text(timeout=2_000)
            except Exception:
                body = "<body unavailable>"
            detail = f"\nPage text:\n{body[:2_000]}"
            if console_errors:
                detail += f"\nConsole errors:\n{console_errors[:10]}"
            raise AssertionError(f"dashboard settings/privacy smoke failed: {error}{detail}") from error
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-url", required=True, help="Dashboard origin URL")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run_browser(args.dashboard_url)
        print(
            "settings_browser=PASS plaintext-warning+clipboard-local-only+"
            "retention+worker-model+same-origin-api-proxy"
        )
    except Exception as error:
        print(f"settings_browser=FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
