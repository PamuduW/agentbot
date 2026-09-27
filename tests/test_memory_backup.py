"""Backup to a verified local mirror and restore into a fresh directory."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory
from src import memory_backup as backups
from src import memory_setup as setup
from tests.support import run_cli_main
from tests.test_memory import FAKE_GITHUB_TOKEN, ID_A, _tree_digest, note, v3, v3_vault


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.vault = v3_vault(self.tmp / "v")
        self.vault.commit()
        self.dest = self.tmp / "backups" / "vault"

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()


class BackupTests(BackupTestCase):
    def test_preview_writes_nothing(self) -> None:
        before = _tree_digest(self.vault.root)
        result = backups.backup(self.vault.root, self.dest)
        self.assertEqual("preview", result.state)
        self.assertFalse(self.dest.exists())
        self.assertEqual(before, _tree_digest(self.vault.root))

    def test_backup_then_restore_round_trips_committed_history(self) -> None:
        before = _tree_digest(self.vault.root)
        result = backups.backup(self.vault.root, self.dest, apply=True)
        self.assertEqual("completed", result.state)
        self.assertEqual(before, _tree_digest(self.vault.root))
        self.assertEqual(0o700, stat.S_IMODE(os.stat(self.dest).st_mode))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.dest / "manifest.json").st_mode))
        manifest = backups.read_manifest(self.dest)
        self.assertEqual(self.git(self.vault.root, "rev-parse", "HEAD"), manifest["head"])
        self.assertEqual("unknown", manifest["encryption_assurance"])
        target = self.tmp / "restored"
        preview = backups.restore(self.dest, target, active=self.vault.root)
        self.assertEqual("preview", preview.state)
        self.assertFalse(target.exists())
        restored = backups.restore(self.dest, target, active=self.vault.root, apply=True)
        self.assertEqual("restored", restored.state)
        self.assertEqual(0o700, stat.S_IMODE(os.stat(target).st_mode))
        self.assertEqual("", self.git(target, "remote"))
        self.assertEqual(manifest["head"], self.git(target, "rev-parse", "HEAD"))
        self.assertEqual(3, memory.read_marker(target))
        self.assertEqual([], memory.validate(target).findings)
        for relative in ("core/user/preferences.md", "core/lessons/2026-09-27-core.md"):
            self.assertEqual(
                (self.vault.root / relative).read_bytes(), (target / relative).read_bytes()
            )
        self.assertEqual(before, _tree_digest(self.vault.root))

    def test_a_dirty_tree_warns_and_is_not_included(self) -> None:
        self.vault.write("core/lessons/uncommitted.md", note(v3("lesson", ID_A, "global")))
        result = backups.backup(self.vault.root, self.dest, apply=True)
        self.assertEqual("completed-with-warnings", result.state)
        self.assertEqual(1, result.changed_paths)
        self.assertIn("1 uncommitted path(s) are not included", result.notes)
        target = self.tmp / "restored"
        backups.restore(self.dest, target, apply=True)
        self.assertFalse((target / "core/lessons/uncommitted.md").exists())

    def test_refresh_replaces_the_snapshot_only_after_verification(self) -> None:
        backups.backup(self.vault.root, self.dest, apply=True)
        first = backups.read_manifest(self.dest)["head"]
        self.vault.write("core/lessons/second.md", "---\n")  # committed but invalid
        self.vault.commit()
        second = backups.backup(self.vault.root, self.dest, apply=True)
        self.assertEqual("completed-with-warnings", second.state)
        self.assertGreater(second.findings, 0)
        self.assertNotEqual(first, backups.read_manifest(self.dest)["head"])
        with patch.object(backups, "_verify", side_effect=memory.MemoryVaultError("fsck failed")):
            self.vault.write("core/lessons/third.md", "x\n")
            self.vault.commit()
            with self.assertRaises(memory.MemoryVaultError):
                backups.backup(self.vault.root, self.dest, apply=True)
        self.assertEqual(second.head, backups.read_manifest(self.dest)["head"])
        self.assertEqual(second.head, self.git(self.dest / "local.git", "rev-parse", "HEAD"))
        self.assertEqual([], [p.name for p in self.dest.iterdir() if p.name.startswith(".")])

    def test_a_backup_of_another_vault_is_refused(self) -> None:
        backups.backup(self.vault.root, self.dest, apply=True)
        other = v3_vault(self.tmp / "other")
        other.write("templates/other.md", "a different repository\n")
        other.commit()
        with self.assertRaises(memory.MemoryVaultError) as raised:
            backups.backup(other.root, self.dest, apply=True)
        self.assertIn("different source", str(raised.exception))

    def test_unsafe_destinations_are_refused(self) -> None:
        link = self.tmp / "link"
        link.symlink_to(self.tmp / "elsewhere")
        (self.tmp / "occupied").mkdir()
        (self.tmp / "occupied" / "file").write_text("not a backup")
        for destination in (
            self.vault.root / "backup",
            self.vault.root,
            self.tmp,
            link,
            self.tmp / "occupied",
        ):
            with (
                self.subTest(destination=destination.name),
                self.assertRaises(memory.MemoryVaultError),
            ):
                backups.backup(self.vault.root, destination, apply=True)
        self.assertEqual("not a backup", (self.tmp / "occupied" / "file").read_text())
        with self.assertRaises(memory.MemoryVaultError):
            backups.backup(self.vault.root, self.dest, apply=True, assurance="verified")

    def test_committed_secrets_are_reported_by_the_trial_restore(self) -> None:
        self.vault.write(
            "core/lessons/leak.md", note(v3("lesson", ID_A, "global"), body=FAKE_GITHUB_TOKEN)
        )
        self.vault.commit()
        result = backups.backup(self.vault.root, self.dest, apply=True)
        self.assertEqual("completed-with-warnings", result.state)
        self.assertGreater(result.findings, 0)
        self.assertNotIn(FAKE_GITHUB_TOKEN, json.dumps(backups.backup_json(result)))
        restored = backups.restore(self.dest, self.tmp / "r", apply=True)
        self.assertEqual("restored-with-findings", restored.state)


class RestoreTests(BackupTestCase):
    def setUp(self) -> None:
        super().setUp()
        backups.backup(self.vault.root, self.dest, apply=True)

    def test_missing_or_broken_backups_are_refused(self) -> None:
        for source in (self.tmp / "nowhere", self.vault.root):
            with self.subTest(source=source.name), self.assertRaises(memory.MemoryVaultError):
                backups.restore(source, self.tmp / "r", apply=True)
        (self.dest / "manifest.json").write_text('{"version": 99}')
        with self.assertRaises(memory.MemoryVaultError):
            backups.restore(self.dest, self.tmp / "r", apply=True)
        self.assertFalse((self.tmp / "r").exists())

    def test_restore_never_overwrites(self) -> None:
        occupied = self.tmp / "occupied"
        occupied.mkdir()
        (occupied / "keep").write_text("mine")
        before = _tree_digest(self.vault.root)
        for destination in (
            occupied,
            self.vault.root,
            self.vault.root / "inside",
            self.dest / "inside",
        ):
            with (
                self.subTest(destination=destination.name),
                self.assertRaises(memory.MemoryVaultError),
            ):
                backups.restore(self.dest, destination, active=self.vault.root, apply=True)
        self.assertEqual("mine", (occupied / "keep").read_text())
        self.assertEqual(before, _tree_digest(self.vault.root))

    def test_an_empty_existing_directory_is_accepted(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual("restored", backups.restore(self.dest, empty, apply=True).state)

    def test_a_failed_restore_removes_only_its_own_output(self) -> None:
        target = self.tmp / "failed"
        with patch.object(backups, "read_marker", side_effect=memory.MemoryVaultError("no marker")):
            with self.assertRaises(memory.MemoryVaultError):
                backups.restore(self.dest, target, apply=True)
        self.assertFalse(target.exists())
        empty = self.tmp / "empty"
        empty.mkdir()
        with patch.object(backups, "read_marker", side_effect=memory.MemoryVaultError("no marker")):
            with self.assertRaises(memory.MemoryVaultError):
                backups.restore(self.dest, empty, apply=True)
        self.assertTrue(empty.is_dir())
        self.assertEqual([], list(empty.iterdir()))


class BackupCliTests(BackupTestCase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            AGENTBOT_MEMORY_DIR=str(self.vault.root),
            AGENTBOT_CALLER_PWD=str(self.tmp),
        )
        with patch.dict(os.environ, env, clear=True):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_cli_backup_and_restore(self) -> None:
        rc, stdout = self._cli("memory", "backup", "--destination", "backups/vault")
        self.assertEqual(0, rc, stdout)
        self.assertIn("Preview only", stdout)
        rc, stdout = self._cli(
            "memory", "backup", "--destination", "backups/vault", "--yes", "--json"
        )
        self.assertEqual("completed", json.loads(stdout)["state"])
        rc, stdout = self._cli(
            "memory", "restore", "--source", "backups/vault", "--destination", "r", "--yes"
        )
        self.assertEqual(0, rc, stdout)
        self.assertTrue((self.tmp / "r" / ".meta" / "vault.json").exists())
        rc, _ = self._cli(
            "memory", "restore", "--source", "backups/vault", "--destination", "r", "--yes"
        )
        self.assertEqual(1, rc)


class RecordedBackupTests(BackupTestCase):
    """The converge pass keeps the backup a person made once current."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.tmp / "config" / "agentbot"
        for patcher in (
            patch.object(setup, "VOLATILE_ROOTS", ()),
            patch.dict(
                os.environ,
                {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")},
                clear=True,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        setup.setup_path(self.vault.root, self.config, apply=True)

    def refresh(self) -> tuple[str, str]:
        return backups.refresh(self.tmp / "agentbot", self.config)

    def test_nothing_is_refreshed_until_a_backup_is_recorded(self) -> None:
        self.assertEqual("skipped", self.refresh()[1])
        self.assertFalse(self.dest.exists())

    def test_a_recorded_backup_follows_new_commits_and_survives_setup_again(self) -> None:
        setup.record_backup(self.config, self.dest, "user-attested")
        self.assertEqual(
            {"destination": str(self.dest), "assurance": "user-attested"},
            setup.recorded_backup(self.config),
        )
        self.assertEqual(("refreshed " + str(self.dest), "ok"), self.refresh())
        self.vault.write("core/lessons/later.md", note(v3("lesson", ID_A, "global")))
        self.vault.commit()
        self.assertEqual("ok", self.refresh()[1])
        manifest = backups.read_manifest(self.dest)
        self.assertEqual(self.git(self.vault.root, "rev-parse", "HEAD"), manifest["head"])
        self.assertEqual("user-attested", manifest["encryption_assurance"])
        setup.setup_path(self.vault.root, self.config, apply=True)
        self.assertIsNotNone(setup.recorded_backup(self.config))

    def test_recording_a_backup_keeps_the_sync_settings(self) -> None:
        from src import memory_autosync as autosync

        autosync.set_settings(self.config, mode="manual", fetch_interval=900)
        setup.record_backup(self.config, self.dest, "unknown")
        setup.setup_path(self.vault.root, self.config, apply=True)
        self.assertEqual(
            ("manual", 900),
            (
                autosync.settings(self.config)["mode"],
                autosync.settings(self.config)["fetch_interval"],
            ),
        )
        self.assertIsNotNone(setup.recorded_backup(self.config))

    def test_a_backup_that_cannot_be_refreshed_is_reported_not_raised(self) -> None:
        setup.record_backup(self.config, self.vault.root / "inside", "unknown")
        detail, result = self.refresh()
        self.assertEqual("check", result)
        self.assertIn("not refreshed", detail)

    def test_the_cli_records_the_destination_and_reuses_it(self) -> None:
        env = {
            "HOME": str(self.tmp / "home"),
            "NO_COLOR": "1",
            "AGENTBOT_CALLER_PWD": str(self.tmp),
        }
        argv = ["agentbot", "--root", str(self.tmp / "agentbot"), "memory", "backup"]
        with patch.dict(os.environ, {**env, "XDG_CONFIG_HOME": str(self.config.parent)}):
            rc, stdout, _ = run_cli_main([*argv, "--yes"])
            self.assertEqual(1, rc)  # nothing recorded and no --destination
            rc, stdout, _ = run_cli_main([*argv, "--destination", "backups/vault"])
            self.assertEqual(0, rc, stdout)
            self.assertIsNone(setup.recorded_backup(self.config))  # a preview records nothing
            rc, stdout, _ = run_cli_main([*argv, "--destination", "backups/vault", "--yes"])
            self.assertEqual(0, rc, stdout)
            self.assertEqual(str(self.dest), setup.recorded_backup(self.config)["destination"])
            rc, stdout, _ = run_cli_main([*argv, "--yes", "--json"])
            self.assertEqual(0, rc, stdout)
            self.assertEqual(str(self.dest), json.loads(stdout)["destination"])


if __name__ == "__main__":
    unittest.main()
