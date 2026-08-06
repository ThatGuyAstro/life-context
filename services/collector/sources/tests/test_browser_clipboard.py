"""Focused tests for the browser metadata and clipboard source contracts."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from services.collector.sources.browser_clipboard import (
    BrowserSource,
    ClipboardSource,
    SafeBrowserMetadataFallback,
    SOURCE_BROWSER,
    SOURCE_CLIPBOARD,
    SOURCE_ROUTING_METADATA,
    WindowsClipboardReader,
)


class BrowserClipboardTests(unittest.TestCase):
    def test_routing_metadata_explicitly_disables_audio_and_video(self) -> None:
        for source_name in (SOURCE_BROWSER, SOURCE_CLIPBOARD):
            routing = SOURCE_ROUTING_METADATA[source_name]
            self.assertFalse(routing["supports_audio"])
            self.assertFalse(routing["supports_video"])
            self.assertIs(routing["audio"], False)
            self.assertIs(routing["video"], False)

        browser = BrowserSource(enabled=False)
        metadata = browser.read()
        self.assertIsNone(browser.read_audio())
        self.assertIsNone(browser.read_video())
        self.assertFalse(metadata.as_dict()["routing"]["enabled"])
        self.assertIsNone(metadata.audio)
        self.assertIsNone(metadata.video)

    def test_browser_title_and_url_interfaces_are_composable(self) -> None:
        title = "  A title with exact spacing  "
        url = "https://example.test/a path?q=1#fragment"

        class TitleProvider:
            def read_title(self) -> str:
                return title

        class UrlProvider:
            def read_url(self) -> str:
                return url

        result = BrowserSource(
            title_source=TitleProvider(),
            url_source=UrlProvider(),
        ).read()

        self.assertEqual(result.title, title)
        self.assertEqual(result.url, url)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.routing["route"], "collector.sources.browser")
        self.assertTrue(result.routing["metadata_only"])

    def test_browser_provider_error_uses_safe_metadata_fallback(self) -> None:
        class BrokenProvider:
            def read_metadata(self) -> object:
                raise RuntimeError("provider details must not escape")

        fallback = SafeBrowserMetadataFallback(lambda: "Foreground browser title")
        result = BrowserSource(
            metadata_source=BrokenProvider(),
            fallback=fallback,
        ).read()

        self.assertEqual(result.title, "Foreground browser title")
        self.assertIsNone(result.url)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.fallback_reason, "metadata_provider_error")
        self.assertNotIn("provider details", repr(result.as_dict()))

    def test_clipboard_is_opt_in_and_does_not_call_reader_when_disabled(self) -> None:
        calls: list[int] = []

        def reader() -> str:
            calls.append(1)
            return "secret"

        source = ClipboardSource(enabled=False, reader=reader)
        self.assertIsNone(source.read_current())
        self.assertEqual(source.start(), "disabled")
        self.assertEqual(calls, [])
        source.stop()

    def test_clipboard_poll_preserves_raw_text_exactly(self) -> None:
        raw = "  first\r\n\tsecond\nthird  \r\n"
        source = ClipboardSource(enabled=True, reader=lambda: raw, prefer_listener=False)

        event = source.poll_once()
        self.assertIsNotNone(event)
        self.assertEqual(event.text, raw)
        self.assertEqual(event.raw_text, raw)
        self.assertEqual(event.as_dict()["text"], raw)
        self.assertEqual(event.as_dict()["raw_text"], raw)
        self.assertIsNone(event.audio)
        self.assertIsNone(event.video)
        self.assertIsNone(source.poll_once())

    def test_empty_clipboard_text_is_a_valid_exact_value(self) -> None:
        source = ClipboardSource(enabled=True, reader=lambda: "", prefer_listener=False)
        event = source.poll_once()
        self.assertIsNotNone(event)
        self.assertEqual(event.text, "")

    def test_sequence_number_can_report_same_text_copied_again(self) -> None:
        snapshots = iter([
            {"text": "same", "sequence_number": 1},
            {"text": "same", "sequence_number": 1},
            {"text": "same", "sequence_number": 2},
        ])
        source = ClipboardSource(enabled=True, reader=lambda: next(snapshots), prefer_listener=False)

        self.assertIsNotNone(source.poll_once())
        self.assertIsNone(source.poll_once())
        self.assertIsNotNone(source.poll_once())

    def test_non_windows_reader_is_safe_and_injected_reader_can_poll(self) -> None:
        # The real Win32 reader must be harmless on non-Windows hosts.  The
        # injected reader still lets the polling fallback be exercised there.
        with patch(
            "services.collector.sources.browser_clipboard.read_windows_clipboard_text",
            return_value=None,
        ):
            self.assertIsNone(WindowsClipboardReader().read_text())
        source = ClipboardSource(
            enabled=True,
            reader=lambda: "portable",
            platform_name="linux",
            prefer_listener=True,
            poll_interval=0.01,
        )
        self.assertEqual(source.start(), "polling")
        source.stop()
        self.assertEqual(source.mode, "stopped")

    def test_events_can_stop_without_media_or_browser_side_effects(self) -> None:
        source = ClipboardSource(
            enabled=True,
            reader=lambda: None,
            platform_name="linux",
            poll_interval=0.01,
        )
        stopper = threading.Event()
        stopper.set()
        self.assertEqual(list(source.events(stop_event=stopper)), [])
        self.assertIsNone(source.capture_audio())
        self.assertIsNone(source.capture_video())


if __name__ == "__main__":
    unittest.main()
