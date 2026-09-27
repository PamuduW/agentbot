"""Review 1, M-R1 and M-R3: one operation at a time, no lost queued writes, and
nothing committed that would make the vault invalid.

Each test drives the engine on real Git clones from the M1 fleet.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory_autosync as autosync
from src import memory_sync as sync
from src.memory import MemoryVaultError, validate
from src.memory_projects import project_md
from tests.test_memory_sync import Fleet, at, git, record

ROOT = Path(__file__).resolve().parents[1]


class SafetyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fleet = Fleet(Path(self._tmp.name))
        self.a, self.b = self.fleet.a, self.fleet.b

    def put(self, root: Path, project: str, rel: str, title: str) -> sync.Op:
        return sync.put_project(root, project, rel, record(at(project, rel), title))

    def remote_has(self, path: str) -> bool:
        listed = git(self.fleet.origin, "ls-tree", "-r", "--name-only", "main")
        return path in listed.split()


class LockTests(SafetyTestCase):
    def test_a_second_process_waits_for_the_lock_then_gives_up(self) -> None:
        # A real second process holds the clone's lock.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time\n"
                "from pathlib import Path\n"
                "from src import memory_sync as sync\n"
                "with sync._locked(Path(sys.argv[1])):\n"
                "    print('held', flush=True)\n"
                "    time.sleep(30)\n",
                str(self.a),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.kill)
        self.assertEqual("held", holder.stdout.readline().strip())
        with patch.object(sync, "LOCK_WAIT", 0.3), self.assertRaises(MemoryVaultError) as raised:
            self.put(self.a, "hot", "lessons/blocked.md", "blocked")
        self.assertIn("still running", str(raised.exception))
        self.assertFalse((self.a / "projects/hot/lessons/blocked.md").exists())
        holder.kill()
        holder.wait()
        self.put(self.a, "hot", "lessons/after.md", "after")  # the lock is free again

    def test_a_write_during_a_sync_is_neither_lost_nor_published_half_done(self) -> None:
        # Review 1's scenario: sync reads the queue, a write lands, sync resets,
        # replays its older snapshot, pushes, and clears the queue. With the
        # lock, the write waits for the sync and is queued after it.
        self.put(self.a, "hot", "lessons/first.md", "first")
        fetched = threading.Event()
        release = threading.Event()
        real_git = sync._git

        def slow_fetch(root: Path, *args: str, **kwargs):
            if args[:1] == ("fetch",):
                fetched.set()
                release.wait(10)
            return real_git(root, *args, **kwargs)

        with patch.object(sync, "_git", side_effect=slow_fetch):
            syncing = threading.Thread(target=sync.sync, args=(self.a,))
            syncing.start()
            self.assertTrue(fetched.wait(10))
            writer = threading.Thread(
                target=self.put, args=(self.a, "hot", "lessons/second.md", "second")
            )
            writer.start()
            time.sleep(0.3)
            self.assertTrue(writer.is_alive())  # waiting for the lock
            release.set()
            syncing.join(20)
            writer.join(20)
        self.assertEqual(1, len(sync.pending(self.a)))  # the second write, still queued
        sync.sync(self.a)
        self.assertEqual([], sync.pending(self.a))
        for name in ("first", "second"):
            self.assertTrue(self.remote_has(f"projects/hot/lessons/{name}.md"))


class CrashRecoveryTests(SafetyTestCase):
    def test_a_write_queued_but_not_committed_is_replayed_not_lost(self) -> None:
        with (
            patch.object(sync, "_commit", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.put(self.a, "hot", "lessons/crash.md", "crash")
        (queued,) = sync.pending(self.a)
        self.assertIsNone(queued["commit"])
        # The next operation finishes the job: the half-applied file is put
        # back, and the queued write lands on the next sync.
        sync.sync(self.a)
        self.assertTrue(self.remote_has("projects/hot/lessons/crash.md"))
        self.assertEqual([], sync.pending(self.a))
        self.assertEqual("", git(self.a, "status", "--porcelain"))

    def test_a_commit_that_reached_head_is_adopted(self) -> None:
        with (
            patch.object(sync, "_refresh_index", side_effect=[KeyboardInterrupt, None, None]),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.put(self.a, "hot", "lessons/moved.md", "moved")
        head = git(self.a, "rev-parse", "HEAD").strip()
        with sync._locked(self.a):
            pass
        (queued,) = sync.pending(self.a)
        self.assertEqual(head, queued["commit"])
        self.assertEqual("", git(self.a, "status", "--porcelain"))

    def test_operations_already_on_the_remote_are_not_replayed_again(self) -> None:
        # A run that pushed but stopped before clearing its queue: two edits
        # of one record would otherwise conflict with themselves.
        self.put(self.a, "hot", "lessons/l1.md", "edit one")
        self.put(self.a, "hot", "lessons/l1.md", "edit two")
        queued = sync.pending(self.a)
        sync.sync(self.a)
        sync._save(self.a, "pending.json", queued)
        result = sync.sync(self.a)
        self.assertEqual([], result.conflicts)
        self.assertEqual([], sync.pending(self.a))


class CandidateValidationTests(SafetyTestCase):
    def test_the_users_staging_never_enters_an_engine_commit(self) -> None:
        # An Obsidian layout file the user staged: device state, so it does
        # not pause automation, and it must not ride along in a memory commit.
        layout = self.a / ".obsidian/workspace.json"
        layout.parent.mkdir()
        layout.write_text("{}\n")
        git(self.a, "add", ".obsidian/workspace.json")
        op = self.put(self.a, "hot", "lessons/clean.md", "clean")
        changed = git(self.a, "diff-tree", "--no-commit-id", "--name-only", "-r", op.commit or "")
        self.assertEqual(["projects/hot/lessons/clean.md"], changed.split())
        self.assertIn(".obsidian/workspace.json", git(self.a, "diff", "--cached", "--name-only"))

    def test_deleting_a_record_another_record_supersedes_is_refused(self) -> None:
        target = validate(self.a).records
        lesson = next(r for r in target if r.path == "projects/hot/lessons/l0.md")
        text = record(at("hot", "lessons/newer.md"), "newer").decode()
        text = text.replace("tags: []", f"tags: []\nsupersedes: [{lesson.id}]")
        superseded = (
            (self.a / lesson.path).read_text().replace("status: accepted", "status: superseded")
        )
        sync.change_project(
            self.a,
            "hot",
            [
                ("projects/hot/lessons/newer.md", text.encode()),
                (lesson.path, superseded.encode()),
            ],
        )
        with self.assertRaises(MemoryVaultError) as raised:
            sync.delete_project(self.a, "hot", "lessons/l0.md")
        self.assertIn("MEMORY_SUPERSEDES_TARGET", str(raised.exception))
        self.assertTrue((self.a / lesson.path).exists())
        self.assertEqual("", git(self.a, "status", "--porcelain"))

    def test_writing_into_a_forgotten_project_is_refused(self) -> None:
        sync.forget_project(self.a, "alpha")
        with self.assertRaises(MemoryVaultError) as raised:
            self.put(self.a, "alpha", "lessons/again.md", "again")
        self.assertIn("MEMORY_PROJECT_IDENTITY", str(raised.exception))
        self.assertFalse((self.a / "projects/alpha").exists())

    def test_a_replayed_write_that_would_break_the_vault_becomes_a_conflict(self) -> None:
        sync.forget_project(self.a, "alpha")
        sync.sync(self.a)
        # B had not seen the forget; its write is valid where it was made.
        op = self.put(self.b, "alpha", "lessons/late.md", "late")
        result = sync.sync(self.b)
        self.assertEqual([op.id], [c["op"] for c in result.conflicts])
        self.assertIn("invalid", result.conflicts[0]["reason"])
        self.assertFalse(self.remote_has("projects/alpha/lessons/late.md"))
        self.assertEqual([], [f for f in validate(self.b).findings if f.severity == "error"])
        ref = result.conflicts[0]["recovery_ref"]
        self.assertEqual(0, subprocess.run(["git", "-C", str(self.b), "rev-parse", ref]).returncode)


class TimeoutTests(SafetyTestCase):
    def test_a_hung_fetch_is_offline_not_a_crash(self) -> None:
        real_run = subprocess.run

        def hang_on_fetch(command, *args, **kwargs):
            if "fetch" in command:
                raise subprocess.TimeoutExpired(command, 120)
            return real_run(command, *args, **kwargs)

        self.put(self.a, "hot", "lessons/offline.md", "offline")
        with patch.object(sync.subprocess, "run", side_effect=hang_on_fetch):
            result = sync.sync(self.a)
        self.assertEqual("offline", result.state)
        self.assertEqual(1, len(sync.pending(self.a)))


class ProjectIdentityTests(SafetyTestCase):
    """M-R2: a folder name is not an identity; the registry ID is."""

    def register(self, root: Path, origin: str, project_id: str) -> None:
        sync.register_origin(root, origin, "app", project_id)
        sync.put_project(root, "app", "project.md", project_md(project_id, "app", origin))

    def test_a_losing_registration_writes_nothing_into_the_winners_project(self) -> None:
        winner, loser = (
            "a0a0a0a0-0000-4000-8000-000000000001",
            "b0b0b0b0-0000-4000-8000-000000000002",
        )
        self.register(self.a, "https://github.com/me/app", winner)
        # B, offline, registers a different repository with the same name.
        self.register(self.b, "https://github.com/other/app", loser)
        note = self.put(self.b, "app", "lessons/secret-plan.md", "the other repository's note")
        sync.sync(self.a)
        result = sync.sync(self.b)
        self.assertIn(note.id, [c["op"] for c in result.conflicts])
        self.assertEqual(3, len(result.conflicts))  # the registry entry, project.md, and the note
        self.assertFalse(self.remote_has("projects/app/lessons/secret-plan.md"))
        hub = git(self.fleet.origin, "show", "main:projects/app/project.md")
        self.assertIn(winner, hub)
        self.assertEqual([], [f for f in validate(self.b).findings if f.severity == "error"])


class ConflictScopeTests(SafetyTestCase):
    def repo(self, origin: str) -> Path:
        path = Path(self._tmp.name) / origin.rsplit("/", 1)[-1]
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
        return path

    def alpha_conflict(self) -> dict:
        self.put(self.a, "alpha", "lessons/l0.md", "A's edit")
        sync.sync(self.a)
        self.put(self.b, "alpha", "lessons/l0.md", "B's edit")
        (conflict,) = sync.sync(self.b).conflicts
        return conflict

    def test_a_conflict_is_resolved_only_from_its_own_project(self) -> None:
        conflict = self.alpha_conflict()
        config = Path(self._tmp.name) / "config"
        hot = self.repo("https://github.com/fixture/hot.git")
        for keep in ("mine", "theirs"):
            with self.subTest(keep=keep), self.assertRaises(MemoryVaultError) as raised:
                autosync.resolve(self.b, config, conflict["op"], keep, cwd=hot)
            self.assertIn("belongs to project alpha", str(raised.exception))
        alpha = self.repo("https://github.com/fixture/alpha.git")
        resolved = autosync.resolve(self.b, config, conflict["op"], "mine", cwd=alpha)
        self.assertIsNotNone(resolved["resolution_op"])

    def test_keep_mine_for_a_core_approval_uses_the_core_path(self) -> None:
        # M-R8: approvals are core multi-operations with no project.
        core = "core/lessons/c1.md"
        sync.change_core(self.a, [(core, record(core, "A's approval"))], tier="core")
        sync.sync(self.a)
        sync.change_core(self.b, [(core, record(core, "B's approval"))], tier="core")
        (conflict,) = sync.sync(self.b).conflicts
        self.assertEqual("human", conflict["resolver"])
        autosync.resolve(self.b, Path(self._tmp.name) / "config", conflict["op"], "mine")
        self.assertEqual(record(core, "B's approval"), (self.b / core).read_bytes())


if __name__ == "__main__":
    unittest.main()
