"""Read-only snapshots must not expose a writer's intermediate state."""

import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory_read
from tests.test_memory import VaultFixture


class ConsistentReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vault"
        self.vault = VaultFixture(self.root)
        self.vault.write("a.md", "old-a")
        self.vault.write("b.md", "old-b")
        self.state = self.root / ".git/agentbot-memory"

    def test_read_does_not_create_sync_state(self) -> None:
        self.assertEqual(
            "old-a", memory_read.consistent(self.root, lambda: (self.root / "a.md").read_text())
        )
        self.assertFalse(self.state.exists())

    def test_interrupted_journal_is_unavailable_and_never_recovered_by_read(self) -> None:
        self.state.mkdir()
        journal = self.state / "writing.json"
        journal.write_text("interrupted fixture")
        called = []
        with self.assertRaises(memory_read.MemoryReadUnavailable):
            memory_read.consistent(self.root, lambda: called.append(True))
        self.assertEqual([], called)
        self.assertEqual("interrupted fixture", journal.read_text())

    def test_concurrent_manual_change_retries_instead_of_returning_mixed_data(self) -> None:
        calls = []

        def read():
            calls.append(True)
            a = (self.root / "a.md").read_text()
            if len(calls) == 1:
                (self.root / "a.md").write_text("new-a")
                (self.root / "b.md").write_text("new-b")
            return a, (self.root / "b.md").read_text()

        self.assertEqual(("new-a", "new-b"), memory_read.consistent(self.root, read))
        self.assertEqual(2, len(calls))

    def test_queued_but_uncommitted_operation_is_unavailable(self) -> None:
        self.state.mkdir()
        pending = self.state / "pending.json"
        pending.write_text('[{"id":"synthetic-interrupted","commit":null}]')
        with self.assertRaises(memory_read.MemoryReadUnavailable):
            memory_read.consistent(self.root, lambda: "uncommitted data")
        self.assertIn('"commit":null', pending.read_text())

    def test_committed_offline_operations_do_not_block_recall(self) -> None:
        self.state.mkdir()
        (self.state / "pending.json").write_text('[{"id":"synthetic-offline","commit":"abc123"}]')
        self.assertEqual(
            "local committed data",
            memory_read.consistent(self.root, lambda: "local committed data"),
        )

    def test_malformed_operation_state_is_unavailable(self) -> None:
        self.state.mkdir()
        (self.state / "pending.json").write_text("malformed fixture")
        with self.assertRaises(memory_read.MemoryReadUnavailable):
            memory_read.consistent(self.root, lambda: "must not be published")

    def test_nonregular_lock_is_refused_without_waiting_on_a_pipe(self) -> None:
        self.state.mkdir()
        os.mkfifo(self.state / "lock")
        with self.assertRaises(memory_read.MemoryReadUnavailable):
            memory_read.consistent(self.root, lambda: "must not be published")

    def test_repeated_changes_are_unavailable_after_bounded_attempts(self) -> None:
        calls = []

        def read():
            calls.append(True)
            (self.root / "a.md").write_text(str(len(calls)))
            return "must not be published"

        with self.assertRaises(memory_read.MemoryReadUnavailable):
            memory_read.consistent(self.root, read)
        self.assertLessEqual(len(calls), 3)

    def test_active_writer_lock_is_unavailable(self) -> None:
        self.state.mkdir()
        process = subprocess.Popen(
            [
                "python3",
                "-c",
                "import fcntl,sys,time; f=open(sys.argv[1],'a'); "
                "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(20)",
                str(self.state / "lock"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("locked", process.stdout.readline().strip())
            with self.assertRaises(memory_read.MemoryReadUnavailable):
                memory_read.consistent(self.root, lambda: "must not be published")
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_readable_existing_lock_does_not_need_a_write_open(self) -> None:
        self.state.mkdir()
        lock = self.state / "lock"
        lock.touch(mode=0o400)
        self.assertEqual("ok", memory_read.consistent(self.root, lambda: "ok"))
        self.assertEqual(0, lock.stat().st_size)

    def test_first_writer_creating_a_lock_before_snapshot_is_not_bypassed(self) -> None:
        original = memory_read._fingerprint
        writer = None
        calls = []

        def fingerprint(root, state):
            nonlocal writer
            if writer is None:
                state.mkdir()
                writer = (state / "lock").open("a")
                fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(root, state)

        try:
            with patch.object(memory_read, "_fingerprint", side_effect=fingerprint):
                with self.assertRaises(memory_read.MemoryReadUnavailable):
                    memory_read.consistent(self.root, lambda: calls.append("published"))
            self.assertEqual([], calls)
        finally:
            if writer is not None:
                writer.close()

    def test_symlinked_lock_is_refused(self) -> None:
        self.state.mkdir()
        (self.state / "lock").symlink_to(self.root / "a.md")
        with self.assertRaises(memory_read.MemoryReadUnavailable):
            memory_read.consistent(self.root, lambda: "must not be published")
