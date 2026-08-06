from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from services.collector.sources.filesystem import (
    Debouncer,
    FileEvent,
    FilesystemConfig,
    FilesystemSource,
    discover_git_repositories,
    file_metadata,
    is_safe_path,
    normalize_file_event,
)


class FilesystemSourceTests(unittest.TestCase):
    def test_roots_are_explicit_and_drive_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = FilesystemConfig(roots=[root])
            self.assertEqual(config.roots, (root.resolve(),))
            self.assertFalse(is_safe_path(root.parent / "outside.txt", config.roots))
            with self.assertRaises(ValueError):
                FilesystemConfig(roots=[Path(root.anchor)])

    def test_safe_filter_rejects_git_credentials_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(is_safe_path(root / "src" / "main.py", [root]))
            self.assertFalse(is_safe_path(root / ".git" / "config", [root]))
            self.assertFalse(is_safe_path(root / ".env", [root]))
            self.assertFalse(is_safe_path(root / "private.pem", [root]))
            self.assertFalse(is_safe_path(root / ".." / "outside.txt", [root]))

    def test_safe_filter_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            root = temporary_root / "root"
            outside = temporary_root / "outside"
            root.mkdir()
            outside.mkdir()
            target = outside / "secret.txt"
            target.write_text("secret")
            link = root / "link.txt"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            self.assertFalse(is_safe_path(link, [root]))
            self.assertIsNone(file_metadata(link, root=root))

    def test_discovers_git_directories_and_worktree_git_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            worktree = root / "worktree"
            regular.mkdir()
            worktree.mkdir()
            (regular / ".git").mkdir()
            (worktree / ".git").write_text("gitdir: ../regular/.git/worktrees/worktree\n")
            self.assertEqual(
                discover_git_repositories([root]),
                [regular.resolve(), worktree.resolve()],
            )

    def test_metadata_has_hash_size_mtime_and_no_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "note.txt"
            payload = b"metadata only\n"
            target.write_bytes(payload)
            details = file_metadata(target, root=root)
            assert details is not None
            self.assertEqual(details.size, len(payload))
            self.assertEqual(details.sha256, hashlib.sha256(payload).hexdigest())
            self.assertIsInstance(details.mtime_ns, int)
            self.assertNotIn("content", details.to_dict())

            event = normalize_file_event("modified", target, root, metadata=details)
            assert event is not None
            self.assertEqual(event.path, target.resolve())
            self.assertEqual(event["hash"], details.sha256)
            self.assertNotIn("content", event.to_dict())

    def test_debouncer_coalesces_create_and_modify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "file.txt"
            first = FileEvent("created", target, root, False, 100.0, 1, 1, "a")
            second = FileEvent("modified", target, root, False, 100.1, 2, 2, "b")
            debouncer = Debouncer(delay_seconds=0.5)
            self.assertEqual(debouncer.add(first, now=0.0), [])
            self.assertEqual(debouncer.add(second, now=0.1), [])
            ready = debouncer.flush(now=0.6)
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0].event_type, "created")
            self.assertEqual(ready[0].size, 2)
            self.assertEqual(ready[0].sha256, "b")

    def test_polling_reports_create_modify_delete_and_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = FilesystemSource(
                roots=[root],
                force_polling=True,
                debounce_seconds=0,
                compute_hashes=True,
            )
            self.assertEqual(source.poll_once(), [])

            target = root / "file.txt"
            target.write_text("one")
            created = source.poll_once()
            self.assertEqual([event.event_type for event in created], ["created"])
            self.assertEqual(created[0].size, 3)

            target.write_text("two")
            modified = source.poll_once()
            self.assertEqual([event.event_type for event in modified], ["modified"])
            self.assertEqual(modified[0].sha256, hashlib.sha256(b"two").hexdigest())

            renamed = root / "renamed.txt"
            target.replace(renamed)
            moved = source.poll_once()
            self.assertEqual([event.event_type for event in moved], ["moved"])
            self.assertEqual(moved[0].path, renamed.resolve())
            self.assertEqual(moved[0].old_path, target.resolve())

            renamed.unlink()
            deleted = source.poll_once()
            self.assertEqual([event.event_type for event in deleted], ["deleted"])
            self.assertEqual(deleted[0].path, renamed.resolve())

    def test_running_polling_fallback_publishes_debounced_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = FilesystemSource(
                roots=[root],
                force_polling=True,
                poll_interval=0.02,
                debounce_seconds=0.01,
            )
            source.start()
            try:
                target = root / "live.txt"
                target.write_text("live")
                deadline = time.monotonic() + 2
                event = None
                while time.monotonic() < deadline and event is None:
                    event = source.get_event(timeout=0.1)
                self.assertIsNotNone(event)
                assert event is not None
                self.assertEqual(event.event_type, "created")
                self.assertEqual(event.path, target.resolve())
                self.assertEqual(event["size"], 4)
            finally:
                source.stop()


if __name__ == "__main__":
    unittest.main()
