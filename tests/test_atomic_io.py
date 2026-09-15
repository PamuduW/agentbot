"""Failure-injection coverage for the shared atomic text writer."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.atomic_io import write_text_atomic


class AtomicIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.path = self.root / "settings.json"
        self.original = (
            json.dumps({"theme": "dark", "permissions": {"allow": ["Bash"]}}, indent=2) + "\n"
        )
        self.updated = (
            json.dumps(
                {
                    "theme": "dark",
                    "permissions": {"allow": ["Bash"]},
                    "statusLine": {
                        "type": "command",
                        "command": "~/.claude/statusline-command.sh",
                    },
                },
                indent=2,
            )
            + "\n"
        )
        self.path.write_text(self.original, encoding="utf-8")
        os.chmod(self.path, 0o640)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _leftover_temps(self) -> list[Path]:
        return sorted(
            path
            for path in self.root.iterdir()
            if path.name.startswith(f".{self.path.name}.agentbot-")
        )

    def test_successful_write_replaces_content_and_preserves_mode(self) -> None:
        write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.updated, self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(self.path.stat().st_mode))
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual("dark", loaded["theme"])
        self.assertEqual(["Bash"], loaded["permissions"]["allow"])
        backup = self.path.with_name(f"{self.path.name}.agentbot-backup")
        self.assertEqual(self.original, backup.read_text(encoding="utf-8"))
        self.assertEqual([], self._leftover_temps())

    def test_mkstemp_failure_before_write_leaves_original(self) -> None:
        with (
            mock.patch("tempfile.mkstemp", side_effect=OSError("mkstemp failed")),
            self.assertRaisesRegex(OSError, "mkstemp failed"),
        ):
            write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.original, self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(self.path.stat().st_mode))
        self.assertFalse(self.path.with_name(f"{self.path.name}.agentbot-backup").exists())
        self.assertEqual([], self._leftover_temps())

    def test_fsync_failure_during_write_leaves_original(self) -> None:
        with (
            mock.patch("os.fsync", side_effect=OSError("temporary write failed")),
            self.assertRaisesRegex(OSError, "temporary write failed"),
        ):
            write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.original, self.path.read_text(encoding="utf-8"))
        json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(self.path.stat().st_mode))
        self.assertFalse(self.path.with_name(f"{self.path.name}.agentbot-backup").exists())
        self.assertEqual([], self._leftover_temps())

    def test_replace_failure_leaves_original(self) -> None:
        with (
            mock.patch("os.replace", side_effect=OSError("replace failed")),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.original, self.path.read_text(encoding="utf-8"))
        json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual([], self._leftover_temps())

    def test_directory_fsync_failure_after_replace_keeps_complete_new_content(self) -> None:
        real_fsync = os.fsync
        calls = {"n": 0}

        def fail_directory_fsync(fd: int) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                real_fsync(fd)
                return
            raise OSError("directory fsync failed")

        with (
            mock.patch("os.fsync", side_effect=fail_directory_fsync),
            self.assertRaisesRegex(OSError, "directory fsync failed"),
        ):
            write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.updated, self.path.read_text(encoding="utf-8"))
        json.loads(self.path.read_text(encoding="utf-8"))
        backup = self.path.with_name(f"{self.path.name}.agentbot-backup")
        self.assertEqual(self.original, backup.read_text(encoding="utf-8"))
        self.assertEqual([], self._leftover_temps())

    def test_symlink_destination_is_rejected(self) -> None:
        target = self.root / "real.json"
        target.write_text(self.original, encoding="utf-8")
        self.path.unlink()
        self.path.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "symlink"):
            write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.original, target.read_text(encoding="utf-8"))
        self.assertTrue(self.path.is_symlink())
        self.assertEqual([], self._leftover_temps())

    def test_non_regular_destination_is_rejected(self) -> None:
        self.path.unlink()
        self.path.mkdir()

        with self.assertRaisesRegex(ValueError, "not a regular file"):
            write_text_atomic(self.path, self.updated, backup=True)

        self.assertTrue(self.path.is_dir())

    def test_creates_missing_file_without_backup(self) -> None:
        self.path.unlink()

        write_text_atomic(self.path, self.updated, backup=True)

        self.assertEqual(self.updated, self.path.read_text(encoding="utf-8"))
        self.assertFalse(self.path.with_name(f"{self.path.name}.agentbot-backup").exists())
        self.assertEqual(0o644, stat.S_IMODE(self.path.stat().st_mode))
