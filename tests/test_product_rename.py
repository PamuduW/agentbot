from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.shared_paths import shared_root

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROOTS = (
    ROOT / "install.sh",
    ROOT / "bin",
    ROOT / "src",
    ROOT / "tests",
    ROOT / "base",
    ROOT / "global",
    ROOT / "README.md",
    ROOT / "QUICKSTART.md",
    ROOT / "AGENTS.md",
)


def active_files() -> list[Path]:
    files: list[Path] = []
    for root in ACTIVE_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return [path for path in files if path != Path(__file__) and "__pycache__" not in path.parts]


class ProductRenameTests(unittest.TestCase):
    def test_active_environment_and_python_product_identifiers_are_agentbot(self) -> None:
        forbidden = ("AGENT_BOOTSTRAP_", "BootstrapPaths", "BootstrapService")
        hits = []
        for path in active_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(ROOT)}: {token}")
        self.assertEqual([], hits)

    def test_old_public_command_and_display_identity_are_absent(self) -> None:
        allowed_old_link_files = {ROOT / "install.sh"}
        hits = []
        for path in active_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "agentbot ›" in text and "Agentbot ›" not in text:
                hits.append(str(path.relative_to(ROOT)))
            if "agentboot" in text and path not in allowed_old_link_files:
                hits.append(f"{path.relative_to(ROOT)}: agentboot")
        self.assertEqual([], hits)
        self.assertFalse((ROOT / "bin/agentboot").exists())
        self.assertTrue(os.access(ROOT / "bin/agentbot", os.X_OK))

    def test_legacy_config_reference_is_limited_to_migration_contract(self) -> None:
        allowed = {
            ROOT / "scripts/lib/github_token.sh",
            ROOT / "tests/test_github_token.sh",
            ROOT / "tests/test_token_consumers.sh",
        }
        hits = []
        for path in active_files():
            if (
                "agent_bootstrap/github.env" in path.read_text(encoding="utf-8", errors="replace")
                and path not in allowed
            ):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual([], hits)

    def test_repository_identity_and_origin_allowlist_are_agentbot(self) -> None:
        """Agentbot must keep trusting exactly PamuduW/agentbot.

        The URL matching moved into the shared state machine, which is written
        once against an expected-slug argument. The identity is therefore
        asserted in two halves: the adapter supplies the slug, and the shared
        core builds only github.com URLs from it.
        """
        self.assertEqual("agentbot", ROOT.name)

        adapter = (ROOT / "scripts/lib/repo_update.sh").read_text(encoding="utf-8")
        self.assertIn('repository="${5:-agentbot}"', adapter)
        self.assertIn('slug="PamuduW/${repository}"', adapter)
        self.assertNotIn("agent_bootstrap", adapter)

        core = (shared_root(ROOT) / "scripts/lib/shared/repo_update.sh").read_text(encoding="utf-8")
        for form in (
            '"git@github.com:${expected_slug}"',
            '"git@github.com:${expected_slug}.git"',
            '"https://github.com/${expected_slug}"',
            '"https://github.com/${expected_slug}.git"',
        ):
            self.assertIn(form, core)
        # An origin carrying embedded credentials is never trusted.
        self.assertIn("*://*@*) return 1", core)

    def test_cli_config_and_types_use_agentbot_contract(self) -> None:
        from src.cli import build_parser
        from src.lifecycle import Lifecycle
        from src.paths import AgentbotPaths, default_paths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agentbot"
            config = Path(temporary) / "config"
            previous_home = os.environ.get("AGENTBOT_HOME")
            previous_config = os.environ.get("XDG_CONFIG_HOME")
            os.environ["AGENTBOT_HOME"] = str(root)
            os.environ["XDG_CONFIG_HOME"] = str(config)
            try:
                paths = default_paths()
            finally:
                if previous_home is None:
                    os.environ.pop("AGENTBOT_HOME", None)
                else:
                    os.environ["AGENTBOT_HOME"] = previous_home
                if previous_config is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = previous_config

        self.assertEqual("agentbot", build_parser().prog)
        self.assertIsInstance(paths, AgentbotPaths)
        self.assertEqual(root, paths.root)
        self.assertEqual(config / "agentbot", paths.config_home)
        self.assertEqual("Lifecycle", Lifecycle.__name__)

    def test_public_dispatch_has_no_bootstrap_alias_or_old_shim(self) -> None:
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        dispatcher = (ROOT / "bin/agentbot").read_text(encoding="utf-8")
        self.assertNotIn("bootstrap)", install)
        self.assertNotIn("bootstrap)", dispatcher)
        self.assertNotIn("link-agentboot", install)
        self.assertNotIn("Usage: agentboot", dispatcher)

    def test_owned_old_symlink_cleanup_is_exact_and_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            bin_dir = home / "bin"
            bin_dir.mkdir(parents=True)
            old_link = bin_dir / "agentboot"
            historical = ROOT / "bin/agentboot"

            def cleanup() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        "bash",
                        "-c",
                        'AGENTBOT_SOURCE_ONLY=1 source "$1/install.sh"; cleanup_owned_old_agentboot_link',
                        "_",
                        str(ROOT),
                    ],
                    cwd=ROOT,
                    env={**os.environ, "HOME": str(home)},
                    text=True,
                    capture_output=True,
                    check=False,
                )

            old_link.symlink_to(historical)
            self.assertEqual(0, cleanup().returncode)
            self.assertFalse(old_link.is_symlink())

            relative = os.path.relpath(historical, bin_dir)
            old_link.symlink_to(relative)
            self.assertEqual(0, cleanup().returncode)
            self.assertFalse(old_link.is_symlink())

            old_link.write_text("keep", encoding="utf-8")
            self.assertEqual(0, cleanup().returncode)
            self.assertEqual("keep", old_link.read_text(encoding="utf-8"))
            old_link.unlink()

            for target in (
                Path(temporary) / "foreign/agentboot",
                Path(temporary) / "other/agentbot-checkout/bin/agentboot",
            ):
                old_link.symlink_to(target)
                self.assertEqual(0, cleanup().returncode)
                self.assertTrue(old_link.is_symlink())
                old_link.unlink()


if __name__ == "__main__":
    unittest.main()
