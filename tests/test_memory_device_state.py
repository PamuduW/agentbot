"""Obsidian's per-device files never pause memory sync and leave Git once, on their own."""

from __future__ import annotations

from src import memory
from src import memory_autosync as autosync
from src import memory_project_writes as writes
from src import memory_sync as sync
from tests.test_memory_autosync import AutosyncTestCase
from tests.test_memory_sync import git

LAYOUT = ".obsidian/workspace.json"


class DeviceStateTests(AutosyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The live vault as it was: Obsidian's layout committed by hand and pushed.
        (self.a / ".obsidian").mkdir()
        (self.a / LAYOUT).write_text('{"layout": "committed"}\n')
        git(self.a, "add", LAYOUT)
        git(self.a, "commit", "-q", "-m", "Add Obsidian config")
        git(self.a, "push", "-q", "origin", "HEAD:main")
        autosync.run(self.b)

    def tracked(self, root) -> list[str]:
        return git(root, "ls-files", "--", LAYOUT, ".trash").split()

    def test_obsidian_rewriting_its_layout_does_not_pause_sync(self) -> None:
        (self.a / LAYOUT).write_text('{"layout": "open notes on this machine"}\n')
        (self.a / ".trash").mkdir()
        (self.a / ".trash" / "old.md").write_text("deleted in Obsidian\n")
        outcome = autosync.run(self.a)
        self.assertEqual("synced", outcome["state"], outcome)
        # Untracked and ignored, with this machine's layout kept as it was.
        self.assertEqual([], self.tracked(self.a))
        self.assertEqual(
            '{"layout": "open notes on this machine"}\n', (self.a / LAYOUT).read_text()
        )
        ignore = (self.a / ".gitignore").read_text()
        self.assertIn(sync.DEVICE_STATE_BLOCK, ignore)
        self.assertIn("drafts/*", ignore)  # the existing rules stay
        self.assertEqual("", git(self.a, "status", "--porcelain"))
        self.assertIn(
            "memory(meta): untrack Obsidian per-device state",
            git(self.origin, "log", "-3", "--format=%s", "main"),
        )
        self.assertEqual([], memory.validate(self.a).findings)

    def test_it_happens_once_and_every_machine_converges(self) -> None:
        autosync.run(self.a)
        head = git(self.a, "rev-parse", "HEAD")
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))
        # The other machine has its own layout open while it catches up.
        (self.b / LAYOUT).write_text('{"layout": "machine b"}\n')
        self.assertEqual("synced", autosync.run(self.b)["state"])
        self.assertEqual([], self.tracked(self.b))
        self.assertEqual(
            git(self.a, "rev-parse", "HEAD^{tree}"), git(self.b, "rev-parse", "HEAD^{tree}")
        )

    def test_both_machines_untracking_at_once_converge_without_conflict(self) -> None:
        sync.untrack_device_state(self.a)
        sync.untrack_device_state(self.b)
        self.assertEqual("synced", autosync.run(self.a)["state"])
        outcome = autosync.run(self.b)
        self.assertEqual(("synced", 0), (outcome["state"], outcome["conflicts"]))
        self.assertEqual(
            1, git(self.b, "cat-file", "-p", "HEAD:.gitignore").count(sync.DEVICE_STATE_BLOCK)
        )

    def test_a_real_manual_edit_still_pauses_sync(self) -> None:
        note = self.a / "projects/alpha/notes/n.md"
        note.write_text(note.read_text() + "\nedited by hand in Obsidian\n")
        (self.a / LAYOUT).write_text("{}\n")
        self.assertEqual("paused", autosync.run(self.a)["state"])
        # Writes stay refused too, rather than sweeping the edit into a commit.
        with self.assertRaises(memory.MemoryVaultError):
            writes.add(self.a, self.code, self.config, kind="lesson", title="Blocked", body="x")


if __name__ == "__main__":
    import unittest

    unittest.main()
