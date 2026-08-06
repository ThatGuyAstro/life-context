"""Tests for the portable foreground-window metadata source."""

from __future__ import annotations

import os
import unittest

from services.collector.sources.window_metadata import (
    UnavailableWindowMetadataSource,
    WindowMetadata,
    WindowMetadataSource,
    WindowsWindowMetadataSource,
    create_window_metadata_source,
)


class _FakeWindowsApi:
    def foreground_window(self):
        return 101

    def process_id(self, hwnd):
        assert hwnd == 101
        return 4242

    def process_executable(self, process_id):
        assert process_id == 4242
        return r"C:\Program Files\Example\example.exe"

    def process_created_at(self, process_id):
        assert process_id == 4242
        return 1_700_000_000.25

    def window_title(self, hwnd):
        assert hwnd == 101
        return "Example window"

    def monitor(self, hwnd):
        assert hwnd == 101
        return r"\\.\DISPLAY2"


class _FailingWindowsApi:
    def foreground_window(self):
        raise RuntimeError("foreground lookup denied")


class WindowMetadataTests(unittest.TestCase):
    def test_windows_source_collects_all_metadata_with_stdlib_fake(self):
        source = WindowsWindowMetadataSource(
            api=_FakeWindowsApi(),
            clock=lambda: 1_800_000_000.5,
        )

        self.assertIsInstance(source, WindowMetadataSource)
        metadata = source.collect()

        self.assertEqual(
            metadata,
            WindowMetadata(
                timestamp=1_800_000_000.5,
                foreground_process="example.exe",
                executable=r"C:\Program Files\Example\example.exe",
                window_title="Example window",
                monitor=r"\\.\DISPLAY2",
                process_id=4242,
                process_created_at=1_700_000_000.25,
                available=True,
            ),
        )
        self.assertEqual(metadata.captured_at, metadata.timestamp)
        self.assertEqual(metadata.observed_at, metadata.timestamp)
        self.assertEqual(metadata.monitor_name, metadata.monitor)
        self.assertEqual(metadata.process_name, metadata.foreground_process)

    def test_individual_platform_failures_become_unavailable_values(self):
        class PartiallyFailingApi(_FakeWindowsApi):
            def process_executable(self, process_id):
                raise PermissionError("query denied")

            def window_title(self, hwnd):
                raise OSError("title unavailable")

        metadata = WindowsWindowMetadataSource(
            api=PartiallyFailingApi(),
            clock=lambda: 123.0,
        ).read()

        self.assertEqual(metadata.timestamp, 123.0)
        self.assertTrue(metadata.available)
        self.assertIsNone(metadata.foreground_process)
        self.assertIsNone(metadata.executable)
        self.assertIsNone(metadata.window_title)
        self.assertEqual(metadata.monitor, r"\\.\DISPLAY2")
        self.assertEqual(metadata.process_created_at, 1_700_000_000.25)

    def test_foreground_lookup_failure_is_graceful(self):
        metadata = WindowsWindowMetadataSource(
            api=_FailingWindowsApi(),
            clock=lambda: 456.0,
        ).collect()

        self.assertEqual(metadata.timestamp, 456.0)
        self.assertFalse(metadata.available)
        self.assertIsNone(metadata.foreground_process)
        self.assertIsNone(metadata.executable)
        self.assertIsNone(metadata.window_title)
        self.assertIsNone(metadata.monitor)
        self.assertIsNone(metadata.process_id)

    def test_unavailable_source_returns_timestamped_empty_record(self):
        source = UnavailableWindowMetadataSource(
            reason="test unavailable",
            clock=lambda: 789.0,
        )

        metadata = source.snapshot()

        self.assertIsInstance(metadata, WindowMetadata)
        self.assertEqual(metadata.as_dict()["timestamp"], 789.0)
        self.assertFalse(metadata.available)
        self.assertIsNone(metadata.foreground_process)
        self.assertEqual(source.reason, "test unavailable")

    def test_factory_is_portable_and_returns_the_contract(self):
        source = create_window_metadata_source()
        metadata = source.collect()

        self.assertIsInstance(source, WindowMetadataSource)
        self.assertIsInstance(metadata, WindowMetadata)
        self.assertIsInstance(metadata.timestamp, float)
        if os.name != "nt":
            self.assertIsInstance(source, UnavailableWindowMetadataSource)
            self.assertFalse(metadata.available)


if __name__ == "__main__":
    unittest.main()
