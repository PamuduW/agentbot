"""Obsidian's per-device files never pause memory sync and leave Git once, on their own."""

from __future__ import annotations

from pathlib import Path

from src import memory
from src import memory_autosync as autosync
from src import memory_obsidian as obsidian
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

    def test_trash_that_old_history_tracks_keeps_this_machines_copy(self) -> None:
        # Review 1, M-R12: the reset to the remote tip wrote the remote's copy.
        git(self.a, "pull", "-q", "--ff-only", "origin", "main")
        (self.a / ".trash").mkdir()
        (self.a / ".trash/old.md").write_text("committed trash\n")
        git(self.a, "add", "-f", ".trash/old.md")
        git(self.a, "commit", "-q", "-m", "Trash committed by hand")
        git(self.a, "push", "-q", "origin", "HEAD:main")
        # B has that history too, then Obsidian changes its trash before B syncs.
        git(self.b, "pull", "-q", "--ff-only", "origin", "main")
        (self.b / ".trash/old.md").write_text("this machine's trash\n")
        self.assertEqual("synced", autosync.run(self.b)["state"])
        self.assertEqual("this machine's trash\n", (self.b / ".trash/old.md").read_text())

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
        self.assertIn("exports/*", ignore)  # the existing rules stay
        self.assertEqual("", git(self.a, "status", "--porcelain"))
        self.assertIn(
            "memory(meta): set up Obsidian views and settings",
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
        sync.setup_obsidian(self.a)
        sync.setup_obsidian(self.b)
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


class ObsidianKitTests(AutosyncTestCase):
    """The vault's Obsidian setup: dashboard, settings, and nothing the user owns."""

    def setUp(self) -> None:
        super().setUp()
        obsidian = self.a / ".obsidian"
        obsidian.mkdir()
        (obsidian / "core-plugins.json").write_text('{"graph": true, "bases": false}\n')
        (obsidian / "app.json").write_text('{"userIgnoreFilters": ["mine/"], "vimMode": true}\n')
        (obsidian / "graph.json").write_text('{"colorGroups": [], "scale": 1.5}\n')
        git(self.a, "add", ".obsidian")
        git(self.a, "commit", "-q", "-m", "Obsidian config")
        git(self.a, "push", "-q", "origin", "HEAD:main")

    def json(self, relative: str) -> dict:
        import json

        return json.loads((self.a / relative).read_text())

    def test_sync_sets_up_views_and_only_the_owned_settings(self) -> None:
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertTrue((self.a / obsidian.VIEW_PATH).is_file())
        self.assertEqual({"graph": True, "bases": True}, self.json(".obsidian/core-plugins.json"))
        app = self.json(".obsidian/app.json")
        self.assertEqual(
            (True, "absolute", True, ["mine/", "templates/", "exports/"]),
            (
                app["vimMode"],
                app["newLinkFormat"],
                app["alwaysUpdateLinks"],
                app["userIgnoreFilters"],
            ),
        )
        graph = self.json(".obsidian/graph.json")
        self.assertEqual((1.5, 3), (graph["scale"], len(graph["colorGroups"])))
        self.assertEqual("", git(self.a, "status", "--porcelain"))
        self.assertEqual([], memory.validate(self.a).findings)
        # Once only; the other machine receives it.
        head = git(self.a, "rev-parse", "HEAD")
        autosync.run(self.a)
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))
        autosync.run(self.b)
        self.assertTrue((self.b / obsidian.VIEW_PATH).is_file())

    def test_obsidians_settings_are_per_machine_and_never_pause_sync(self) -> None:
        # Obsidian reformats its settings on open and saves the graph zoom as
        # you zoom; in Git each of those read as a hand edit and paused sync.
        import json

        self.assertEqual("synced", autosync.run(self.a)["state"])
        settings = [".obsidian/app.json", ".obsidian/core-plugins.json", ".obsidian/graph.json"]
        tracked = git(self.a, "ls-files", "--", *settings, obsidian.DEVICE_MARKER).split()
        self.assertEqual([], tracked)
        self.assertTrue((self.a / obsidian.DEVICE_MARKER).is_file())
        graph = self.json(".obsidian/graph.json")
        (self.a / ".obsidian/graph.json").write_text(json.dumps({**graph, "scale": 3}, indent=2))
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertEqual(3, self.json(".obsidian/graph.json")["scale"])  # kept here, not pushed
        # The other machine opens the vault: it gets the settings on its own
        # disk, and nothing is committed for them.
        autosync.run(self.b)
        (self.b / ".obsidian").mkdir(exist_ok=True)
        (self.b / ".obsidian/app.json").write_text("{}")
        head = git(self.b, "rev-parse", "HEAD")
        self.assertEqual("synced", autosync.run(self.b)["state"])
        self.assertEqual(head, git(self.b, "rev-parse", "HEAD"))
        self.assertEqual(
            "absolute", json.loads((self.b / ".obsidian/app.json").read_text())["newLinkFormat"]
        )

    def test_the_users_dashboard_and_colour_groups_are_never_overwritten(self) -> None:
        (self.a / "views").mkdir()
        (self.a / obsidian.VIEW_PATH).write_text("views: []\n")
        (self.a / ".obsidian/graph.json").write_text('{"colorGroups": [{"query": "tag:x"}]}\n')
        git(self.a, "add", "-A")
        git(self.a, "commit", "-q", "-m", "mine")
        git(self.a, "push", "-q", "origin", "HEAD:main")
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertEqual("views: []\n", (self.a / obsidian.VIEW_PATH).read_text())
        self.assertEqual([{"query": "tag:x"}], self.json(".obsidian/graph.json")["colorGroups"])

    def test_a_broken_settings_file_is_left_alone(self) -> None:
        (self.a / ".obsidian/app.json").write_text("{not json")
        git(self.a, "commit", "-q", "-am", "broken")
        git(self.a, "push", "-q", "origin", "HEAD:main")
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertEqual("{not json", (self.a / ".obsidian/app.json").read_text())
        self.assertTrue(self.json(".obsidian/core-plugins.json")["bases"])

    def test_a_vault_never_opened_in_obsidian_is_untouched(self) -> None:
        head = git(self.b, "rev-parse", "HEAD")
        self.assertIsNone(sync.setup_obsidian(self.b))
        self.assertEqual(head, git(self.b, "rev-parse", "HEAD"))

    def test_it_sets_up_once_and_then_respects_the_users_choices(self) -> None:
        autosync.run(self.a)
        self.assertEqual({"kit": obsidian.KIT_VERSION}, self.json(obsidian.MARKER))
        (self.a / obsidian.VIEW_PATH).unlink()
        (self.a / ".obsidian/core-plugins.json").write_text('{"graph": true, "bases": false}\n')
        git(self.a, "add", "-A")
        git(self.a, "commit", "-q", "-m", "I prefer it this way")
        git(self.a, "push", "-q", "origin", "HEAD:main")
        head = git(self.a, "rev-parse", "HEAD")
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))
        self.assertFalse((self.a / obsidian.VIEW_PATH).exists())
        self.assertFalse(self.json(".obsidian/core-plugins.json")["bases"])

    def test_it_never_writes_through_a_symlink_or_invents_core_plugins(self) -> None:
        import tempfile

        outside = Path(tempfile.mkdtemp(dir=self.tmp))
        (self.a / "views").symlink_to(outside, target_is_directory=True)
        (self.a / ".obsidian/core-plugins.json").unlink()
        git(self.a, "add", "-A")
        git(self.a, "commit", "-q", "-m", "symlinked views, no core plugins")
        git(self.a, "push", "-q", "origin", "HEAD:main")
        wanted = obsidian.changes(self.a)
        self.assertNotIn(obsidian.VIEW_PATH, wanted)
        self.assertNotIn(".obsidian/core-plugins.json", obsidian.device_changes(self.a))
        obsidian.apply(self.a)
        self.assertEqual([], list(outside.iterdir()))

    def test_the_dashboard_is_scanned_like_any_vault_file(self) -> None:
        from tests.test_memory import FAKE_GITHUB_TOKEN

        (self.a / "views").mkdir()
        (self.a / "views/leak.base").write_text(f"# {FAKE_GITHUB_TOKEN}\nviews: []\n")
        (self.a / "views/Bad Name.base").write_text("views: []\n")
        rules = {(f.path, f.rule) for f in memory.validate(self.a).findings}
        self.assertTrue(any(path == "views/leak.base" for path, _ in rules))
        self.assertIn(("views/Bad Name.base", "MEMORY_FILE_TYPE"), rules)
