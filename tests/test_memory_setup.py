"""Ticket M2: explicit vault setup and config-based resolution."""

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
from src import memory_setup as setup
from tests.support import run_cli_main
from tests.test_memory import v1_vault, v2_vault

IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


class SetupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = self.tmp / "config" / "agentbot"
        # Fixtures live under the system temp directory, which setup otherwise
        # refuses as a vault location.
        patcher = patch.object(setup, "VOLATILE_ROOTS", ())
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, IDENTITY_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.vault = v2_vault(self.tmp / "vault")
        self.vault.commit()

    def resolve(self) -> Path | None:
        found, _ = memory.find_vault(environ={}, config_home=self.config)
        return found


class PathSetupTests(SetupTestCase):
    def test_preview_then_apply_writes_a_private_config(self) -> None:
        preview = setup.setup_path(self.vault.root, self.config)
        self.assertEqual("preview", preview.state)
        self.assertFalse(setup.config_path(self.config).exists())
        self.assertIsNone(self.resolve())
        result = setup.setup_path(self.vault.root, self.config, apply=True)
        self.assertEqual("configured", result.state)
        self.assertEqual(0o700, stat.S_IMODE(os.stat(self.config).st_mode))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(setup.config_path(self.config)).st_mode))
        data = json.loads(setup.config_path(self.config).read_text())
        self.assertEqual(str(self.vault.root.resolve()), data["vault"]["path"])
        self.assertEqual(setup.identity(self.vault.root), data["vault"]["identity"])
        self.assertEqual(self.vault.root.resolve(), self.resolve())

    def test_a_replaced_checkout_fails_closed(self) -> None:
        setup.setup_path(self.vault.root, self.config, apply=True)
        subprocess.run(["rm", "-rf", str(self.vault.root)], check=True)
        other = v1_vault(self.vault.root)
        other.commit()
        with self.assertRaises(memory.MemoryVaultError) as raised:
            self.resolve()
        self.assertIn("different repository", str(raised.exception))

    def test_a_missing_configured_vault_fails_closed(self) -> None:
        setup.setup_path(self.vault.root, self.config, apply=True)
        subprocess.run(["rm", "-rf", str(self.vault.root)], check=True)
        with self.assertRaises(memory.MemoryVaultError):
            self.resolve()

    def test_only_a_real_vault_checkout_top_is_accepted(self) -> None:
        (self.tmp / "link").symlink_to(self.vault.root)
        plain = self.tmp / "plain"
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        for bad in (
            self.vault.root / "lessons",
            self.tmp / "link",
            plain,
            self.tmp / "missing",
            Path("relative"),
        ):
            with self.subTest(path=str(bad)), self.assertRaises(memory.MemoryVaultError):
                setup.setup_path(bad, self.config, apply=True)
        self.assertFalse(setup.config_path(self.config).exists())

    def test_environment_overrides_still_win(self) -> None:
        setup.setup_path(self.vault.root, self.config, apply=True)
        other = v2_vault(self.tmp / "other")
        found, _ = memory.find_vault(
            environ={"AGENTBOT_MEMORY_DIR": str(other.root)}, config_home=self.config
        )
        self.assertEqual(other.root, found)

    def test_remove_forgets_the_config_and_keeps_the_vault(self) -> None:
        setup.setup_path(self.vault.root, self.config, apply=True)
        self.assertEqual("preview", setup.remove(self.config).state)
        self.assertTrue(setup.config_path(self.config).exists())
        self.assertEqual("removed", setup.remove(self.config, apply=True).state)
        self.assertFalse(setup.config_path(self.config).exists())
        self.assertTrue((self.vault.root / ".meta/vault.json").exists())
        self.assertIsNone(self.resolve())
        self.assertEqual("unconfigured", setup.remove(self.config).state)

    def test_a_symlinked_or_corrupt_config_is_refused(self) -> None:
        self.config.mkdir(parents=True)
        setup.config_path(self.config).write_text("{not json")
        with self.assertRaises(memory.MemoryVaultError):
            self.resolve()
        setup.config_path(self.config).unlink()
        (self.tmp / "elsewhere.json").write_text("{}")
        setup.config_path(self.config).symlink_to(self.tmp / "elsewhere.json")
        with self.assertRaises(memory.MemoryVaultError):
            self.resolve()


class CloneAndNewTests(SetupTestCase):
    def bare_from_vault(self) -> Path:
        bare = self.tmp / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.vault.root), str(bare)], check=True
        )
        return bare

    def test_clone_configures_the_new_checkout(self) -> None:
        bare = self.bare_from_vault()
        dest = self.tmp / "machine-b" / "agent-memory"
        preview = setup.setup_clone(str(bare), dest, self.config)
        self.assertEqual("preview", preview.state)
        self.assertFalse(dest.exists())
        result = setup.setup_clone(str(bare), dest, self.config, apply=True)
        self.assertEqual("configured", result.state)
        self.assertEqual(0o700, stat.S_IMODE(os.stat(dest).st_mode))
        self.assertEqual(dest.resolve(), self.resolve())
        self.assertEqual(setup.identity(self.vault.root), setup.identity(dest))

    def test_clone_of_a_non_vault_leaves_nothing_behind(self) -> None:
        plain = self.tmp / "plain"
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        (plain / "x.txt").write_text("x")
        subprocess.run(["git", "-C", str(plain), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(plain), "commit", "-qm", "x"], check=True, capture_output=True
        )
        dest = self.tmp / "dest"
        with self.assertRaises(memory.MemoryVaultError):
            setup.setup_clone(str(plain), dest, self.config, apply=True)
        self.assertFalse(dest.exists())
        self.assertFalse(setup.config_path(self.config).exists())

    def test_destinations_are_checked(self) -> None:
        occupied = self.tmp / "occupied"
        occupied.mkdir()
        (occupied / "keep").write_text("mine")
        (self.tmp / "linkdir").symlink_to(self.tmp)
        bad = [
            occupied,
            self.vault.root / "nested",  # inside another repository
            self.tmp / "linkdir" / "vault",  # through a symlink
            Path("relative/vault"),
            Path("/mnt/c/Users/vault"),
        ]
        for dest in bad:
            with self.subTest(dest=str(dest)), self.assertRaises(memory.MemoryVaultError):
                setup.check_destination(dest)
        self.assertEqual("mine", (occupied / "keep").read_text())
        with (
            patch.object(setup, "VOLATILE_ROOTS", ("/tmp",)),
            self.assertRaises(memory.MemoryVaultError),
        ):
            setup.check_destination(Path("/tmp/agentbot-vault-check"))

    def test_new_creates_a_valid_empty_vault_and_never_pushes(self) -> None:
        bare = self.tmp / "empty-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        dest = self.tmp / "fresh"
        result = setup.setup_new(dest, self.config, remote=str(bare), apply=True)
        self.assertEqual("configured", result.state)
        self.assertEqual(2, memory.read_marker(dest))
        self.assertEqual([], memory.validate(dest).findings)
        self.assertEqual(dest.resolve(), self.resolve())
        self.assertEqual(str(bare), result.vault["remote"])
        refs = subprocess.run(
            ["git", "-C", str(bare), "for-each-ref"], capture_output=True, text=True, check=True
        )
        self.assertEqual("", refs.stdout)  # nothing pushed

    def test_urls_with_credentials_are_refused(self) -> None:
        refused = [
            "https://user:secret@github.com/o/r.git",
            "https://token@github.com/o/r.git",
            "https://github.com/o/r.git?token=x",
            "https://github.com/o/r .git",
            "ftp://github.com/o/r.git",
            "",
        ]
        for url in refused:
            with self.subTest(url=url), self.assertRaises(memory.MemoryVaultError):
                setup.sanitize_url(url)
        for url in (
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r.git",
            "https://github.com/o/r.git",
            "/srv/git/r.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(url, setup.sanitize_url(url))


class SetupCliTests(SetupTestCase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"), NO_COLOR="1", XDG_CONFIG_HOME=str(self.tmp / "config")
        )
        with patch.dict(os.environ, env, clear=True):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_cli_setup_then_status(self) -> None:
        rc, stdout = self._cli("memory", "status")
        self.assertEqual(2, rc)
        self.assertIn("memory setup", stdout)
        rc, stdout = self._cli("memory", "setup", "--path", str(self.vault.root))
        self.assertEqual(0, rc)
        self.assertIn("Preview only", stdout)
        rc, _ = self._cli("memory", "setup", "--path", str(self.vault.root), "--yes")
        self.assertEqual(0, rc)
        rc, stdout = self._cli("memory", "status", "--json")
        self.assertEqual(0, rc, stdout)
        self.assertEqual(str(self.vault.root.resolve()), json.loads(stdout)["root"])
        rc, stdout = self._cli("memory", "setup", "--json")
        self.assertEqual("shown", json.loads(stdout)["state"])

    def test_cli_reports_a_mismatched_vault_as_broken(self) -> None:
        self._cli("memory", "setup", "--path", str(self.vault.root), "--yes")
        subprocess.run(["rm", "-rf", str(self.vault.root)], check=True)
        v1_vault(self.vault.root).commit()
        for command in (("status",), ("validate",), ("search", "x"), ("brief",)):
            with self.subTest(command=command):
                rc, stdout = self._cli("memory", *command, "--json")
                self.assertEqual(1, rc)
                self.assertEqual("broken", json.loads(stdout)["state"])


if __name__ == "__main__":
    unittest.main()
