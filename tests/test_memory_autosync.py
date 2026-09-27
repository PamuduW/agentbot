"""Ticket M6: automatic sync, sync settings, and conflict resolution."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from src import memory_autosync as autosync
from src import memory_project_writes as writes
from src import memory_setup
from src import memory_sync as sync
from src.memory import MemoryVaultError
from tests.support import run_cli_main
from tests.test_memory import v2_vault
from tests.test_memory_project_writes import LESSON, WritesTestCase
from tests.test_memory_sync import git
from tests.test_memory_v3 import uid


class AutosyncTestCase(WritesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_b = self.tmp / "config-b"
        for vault, config in ((self.a, self.config), (self.b, self.config_b)):
            memory_setup.setup_path(vault, config, apply=True)

    def remote_has(self, op_id: str) -> bool:
        return f"Agentbot-Op: {op_id}" in git(self.origin, "log", "--format=%B", "main")


class AutoModeTests(AutosyncTestCase):
    def test_a_write_is_pushed_and_another_clone_sees_it_on_read(self) -> None:
        result = writes.add(
            self.a, self.code, self.config, kind="lesson", title="Pushed at once", body="x"
        )
        outcome = autosync.after_write(self.a, self.config)
        self.assertEqual(("synced", 0), (outcome["state"], outcome["pending"]))
        self.assertTrue(self.remote_has(result["op"]))
        self.assertFalse((self.b / result["path"]).exists())
        self.assertEqual("synced", autosync.before_read(self.b, self.config_b)["state"])
        self.assertTrue((self.b / result["path"]).exists())

    def test_reads_fetch_at_most_once_per_interval(self) -> None:
        with patch.object(sync, "sync", wraps=sync.sync) as spy:
            self.assertIsNotNone(autosync.before_read(self.b, self.config_b))
            self.assertIsNone(autosync.before_read(self.b, self.config_b))
            self.assertEqual(1, spy.call_count)
            later = autosync.last_sync(self.b)["at"] + autosync.DEFAULT_INTERVAL + 1
            self.assertIsNotNone(autosync.before_read(self.b, self.config_b, now=later))
            self.assertEqual(2, spy.call_count)
        autosync.set_settings(self.config_b, fetch_interval=0)
        self.assertIsNotNone(autosync.before_read(self.b, self.config_b))

    def test_offline_writes_stay_pending_until_the_remote_returns(self) -> None:
        url = git(self.a, "remote", "get-url", "origin").strip()
        git(self.a, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        result = writes.add(
            self.a, self.code, self.config, kind="lesson", title="Offline", body="x"
        )
        outcome = autosync.after_write(self.a, self.config)
        self.assertEqual(("offline", 1), (outcome["state"], outcome["pending"]))
        self.assertFalse(self.remote_has(result["op"]))
        git(self.a, "remote", "set-url", "origin", url)
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertTrue(self.remote_has(result["op"]))

    def test_manual_edits_pause_automation_without_breaking_reads(self) -> None:
        (self.a / LESSON).write_text((self.a / LESSON).read_text() + "hand edit\n")
        outcome = autosync.run(self.a)
        self.assertEqual("paused", outcome["state"])
        autosync.set_settings(self.config, fetch_interval=0)
        self.assertEqual("paused", autosync.before_read(self.a, self.config)["state"])


class ModeTests(AutosyncTestCase):
    def test_manual_mode_commits_locally_and_pushes_only_on_request(self) -> None:
        autosync.set_settings(self.config, mode="manual")
        result = writes.add(self.a, self.code, self.config, kind="lesson", title="Held", body="x")
        outcome = autosync.after_write(self.a, self.config)
        self.assertEqual(("manual", 1), (outcome["state"], outcome["pending"]))
        self.assertIsNone(autosync.before_read(self.a, self.config))
        self.assertFalse(self.remote_has(result["op"]))
        self.assertEqual("synced", autosync.run(self.a)["state"])
        self.assertTrue(self.remote_has(result["op"]))

    def test_settings_are_validated_and_private(self) -> None:
        for bad in ({"mode": "sometimes"}, {"fetch_interval": -1}):
            with self.subTest(bad=bad), self.assertRaises(MemoryVaultError):
                autosync.set_settings(self.config, **bad)  # type: ignore[arg-type]
        autosync.set_settings(self.config, mode="manual", fetch_interval=60)
        self.assertEqual({"mode": "manual", "fetch_interval": 60}, autosync.settings(self.config))
        self.assertEqual(0o600, os.stat(memory_setup.config_path(self.config)).st_mode & 0o777)
        with self.assertRaises(MemoryVaultError):
            autosync.set_settings(self.tmp / "unconfigured", mode="auto")

    def test_older_schemas_keep_their_manual_git_workflow(self) -> None:
        old = v2_vault(self.tmp / "v2")
        old.commit()
        self.assertEqual("schema", autosync.after_write(old.root, self.config)["state"])
        self.assertIsNone(autosync.before_read(old.root, self.config))


class ConflictTests(AutosyncTestCase):
    def conflict(self) -> dict:
        writes.edit(self.a, self.code, self.config, "lessons/2026-09-27-l.md", body="A's version")
        autosync.after_write(self.a, self.config)
        writes.edit(self.b, self.code, self.config_b, "lessons/2026-09-27-l.md", body="B's version")
        outcome = autosync.after_write(self.b, self.config_b)
        self.assertEqual(1, outcome["conflicts"])
        (conflict,) = autosync.open_conflicts(self.b)
        return conflict

    def test_conflicts_are_listed_and_show_both_versions(self) -> None:
        conflict = self.conflict()
        self.assertNotIn("op_data", conflict)
        shown = autosync.show(self.b, conflict["op"])
        (item,) = shown["files"]
        self.assertIn("A's version", item["current"])
        self.assertIn("B's version", item["mine"])
        self.assertEqual("agent", shown["resolver"])
        self.assertEqual(1, autosync.status(self.b, self.config_b)["conflicts"])

    def test_keep_theirs_accepts_the_current_state(self) -> None:
        conflict = self.conflict()
        result = autosync.resolve(self.b, self.config_b, conflict["op"], "theirs")
        self.assertIsNone(result["resolution_op"])
        self.assertEqual([], autosync.open_conflicts(self.b))
        self.assertIn("A's version", (self.b / LESSON).read_text())

    def test_keep_mine_reapplies_and_pushes_the_lost_version(self) -> None:
        conflict = self.conflict()
        result = autosync.resolve(self.b, self.config_b, conflict["op"], "mine")
        self.assertEqual("synced", result["sync"]["state"])
        self.assertIn("B's version", (self.b / LESSON).read_text())
        autosync.run(self.a)
        self.assertIn("B's version", (self.a / LESSON).read_text())
        self.assertEqual([], autosync.open_conflicts(self.b))

    def test_a_lost_supersession_is_reapplied_whole(self) -> None:
        writes.edit(self.b, self.code, self.config_b, "lessons/2026-09-27-l.md", body="edited on B")
        autosync.after_write(self.b, self.config_b)
        added = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Replacement",
            body="r",
            supersedes=uid(6),
        )
        self.assertEqual(1, autosync.after_write(self.a, self.config)["conflicts"])
        (conflict,) = autosync.open_conflicts(self.a)
        self.assertEqual(
            {added["path"], LESSON},
            {f["path"] for f in autosync.show(self.a, conflict["op"])["files"]},
        )
        autosync.resolve(self.a, self.config, conflict["op"], "mine")
        self.assertTrue((self.a / added["path"]).exists())
        self.assertIn("status: superseded", (self.a / LESSON).read_text())

    def test_unknown_or_settled_conflicts_are_refused(self) -> None:
        conflict = self.conflict()
        autosync.resolve(self.b, self.config_b, conflict["op"], "theirs")
        for op in (conflict["op"], "no-such-op"):
            with self.subTest(op=op), self.assertRaises(MemoryVaultError):
                autosync.resolve(self.b, self.config_b, op, "mine")


class HookReentryTests(AutosyncTestCase):
    """Regression: a vault pre-push hook runs `memory validate`; that must never re-sync.

    Before the guard, sync pushed, the hook ran validate, validate synced and
    pushed again, and the chain never ended (found on the live vault, 2026-09-27).
    """

    def test_a_sync_through_real_hooks_finishes(self) -> None:
        import subprocess
        import sys

        from src import memory_hook

        home = self.tmp / "agentbot-home"
        home.mkdir()
        repo_root = Path(__file__).resolve().parents[1]
        (home / "install.sh").write_text(
            f'#!/bin/sh\ncd {repo_root} && exec {sys.executable} -m src.cli --root "$PWD" "$@"\n'
        )
        (home / "install.sh").chmod(0o755)
        memory_hook.install(self.a, home)
        writes.add(
            self.a, self.code, self.config, kind="lesson", title="Through the hook", body="x"
        )
        script = (
            "import sys; from pathlib import Path; from src import memory_autosync as a; "
            "print(a.run(Path(sys.argv[1]))['state'])"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        done = subprocess.run(
            [sys.executable, "-c", script, str(self.a)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        self.assertEqual("synced", done.stdout.strip().splitlines()[-1], done.stderr[-2000:])
        self.assertEqual(0, len(sync.pending(self.a)))

    def test_nothing_syncs_inside_the_engine_or_a_hook(self) -> None:
        for name in (sync.SYNCING_ENV, autosync.NO_SYNC_ENV):
            with self.subTest(env=name), patch.dict(os.environ, {name: "1"}):
                self.assertEqual("nested", autosync.run(self.a)["state"])
                self.assertEqual("nested", autosync.after_write(self.a, self.config)["state"])
                self.assertIsNone(autosync.before_read(self.a, self.config))

    def test_the_hook_script_disables_sync(self) -> None:
        from src import memory_hook

        self.assertIn("AGENTBOT_MEMORY_NO_SYNC=1", memory_hook.render_hook(self.tmp))


class ReapplyKindsTests(AutosyncTestCase):
    def lose(self, make_a, make_b) -> dict:
        make_a()
        autosync.after_write(self.a, self.config)
        make_b()
        autosync.after_write(self.b, self.config_b)
        (conflict,) = autosync.open_conflicts(self.b)
        return conflict

    def test_keep_mine_for_a_single_put(self) -> None:
        conflict = self.lose(
            lambda: sync.put_project(self.a, "alpha", "notes/n.md", b"---\nA\n---\n"),
            lambda: sync.put_project(self.b, "alpha", "notes/n.md", b"---\nB\n---\n"),
        )
        autosync.resolve(self.b, self.config_b, conflict["op"], "mine")
        self.assertEqual(b"---\nB\n---\n", (self.b / "projects/alpha/notes/n.md").read_bytes())

    def test_keep_mine_for_a_delete(self) -> None:
        conflict = self.lose(
            lambda: sync.put_project(self.a, "alpha", "notes/n.md", b"---\nA\n---\n"),
            lambda: sync.delete_project(self.b, "alpha", "notes/n.md"),
        )
        autosync.resolve(self.b, self.config_b, conflict["op"], "mine")
        self.assertFalse((self.b / "projects/alpha/notes/n.md").exists())

    def test_keep_mine_for_a_core_approval_is_the_humans_call(self) -> None:
        conflict = self.lose(
            lambda: sync.approve_core(self.a, "user/preferences.md", b"---\nA\n---\n"),
            lambda: sync.approve_core(self.b, "user/preferences.md", b"---\nB\n---\n"),
        )
        self.assertEqual("human", conflict["resolver"])
        autosync.resolve(self.b, self.config_b, conflict["op"], "mine")
        self.assertEqual(b"---\nB\n---\n", (self.b / "core/user/preferences.md").read_bytes())


class SyncCliTests(AutosyncTestCase):
    def _cli(self, cwd: Path, *argv: str, stdin: bytes = b"") -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            AGENTBOT_CALLER_PWD=str(cwd),
            XDG_CONFIG_HOME=str(self.config.parent),
        )
        stream = io.TextIOWrapper(io.BytesIO(stdin))
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin", stream):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def setUp(self) -> None:
        super().setUp()
        # The CLI reads ${XDG_CONFIG_HOME}/agentbot; point it at clone A's config.
        self.config = self.tmp / "xdg" / "agentbot"
        memory_setup.setup_path(self.a, self.config, apply=True)

    def test_cli_write_reports_the_sync_and_settings_round_trip(self) -> None:
        rc, stdout = self._cli(
            self.code,
            "memory",
            "project",
            "add",
            "--kind",
            "lesson",
            "--title",
            "Via CLI",
            "--stdin",
            "--json",
            stdin=b"body\n",
        )
        self.assertEqual(0, rc, stdout)
        payload = json.loads(stdout)
        self.assertEqual("synced", payload["sync"]["state"])
        self.assertTrue(self.remote_has(payload["op"]))
        rc, stdout = self._cli(self.code, "memory", "sync", "--mode", "manual", "--json")
        self.assertEqual("manual", json.loads(stdout)["mode"])
        rc, stdout = self._cli(self.code, "memory", "sync", "--status", "--json")
        self.assertEqual(0, json.loads(stdout)["pending"])
        rc, stdout = self._cli(self.code, "memory", "conflict", "--json")
        self.assertEqual({"conflicts": []}, json.loads(stdout))
