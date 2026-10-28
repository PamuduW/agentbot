from __future__ import annotations

import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import CatalogError

from . import test_catalog_context as contexts
from . import test_software_catalog as fixtures


class ProposalTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write
    sources = contexts.QualificationContextTests.sources

    def test_seed_extension_exception_cannot_run_in_daily_proposals(self):
        from catalog_proposal import propose

        self.write()
        output = self.directory / "proposal"
        with self.assertRaisesRegex(CatalogError, "seed-only"):
            propose(self.directory, {}, output, seed_compatible_pr_extension=True)
        self.assertFalse(output.exists())

    def test_seed_extension_exception_selects_only_the_approved_version(self):
        from unittest.mock import patch

        from catalog_proposal import _seed_extension_release

        with patch("src.catalog_extension_discovery.query_extension_release") as latest:
            latest.return_value = ("9.0.0", "linux-x64")
            self.assertEqual(
                ("0.172.0", None),
                _seed_extension_release("github.vscode-pull-request-github", "linux-x64"),
            )
            latest.assert_not_called()
            self.assertEqual(
                ("9.0.0", "linux-x64"),
                _seed_extension_release("publisher.another", "linux-x64"),
            )
            latest.assert_called_once_with("publisher.another", "linux-x64")

    def test_seed_exception_still_downloads_and_verifies_the_exact_vsix_closure(self):
        import hashlib
        import json

        from catalog_proposal import _seed_extension_release

        from src.catalog_extension_discovery import ExtensionCandidateResolver

        from .test_catalog_extension_discovery import ExtensionDiscoveryTests

        identity = "github.vscode-pull-request-github"
        raw = ExtensionDiscoveryTests.archive(identity, "0.172.0", platform="")
        calls = []

        def download(url, destination, *, publisher):
            calls.append((url, publisher))
            destination.write_bytes(raw)

        locks = {}
        resolver = ExtensionCandidateResolver(
            locks, download=download, release_resolver=_seed_extension_release
        )
        artifact = resolver.resolve(identity, "linux-amd64")
        self.assertEqual("0.172.0", artifact["version"])
        entry = json.loads(locks[artifact["lock"]])["extensions"][identity]
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), entry["digest"])
        self.assertEqual(
            [(entry["url"], "vscode-marketplace")],
            calls,
        )
        self.assertIn("/0.172.0/vspackage", entry["url"])

    def test_cli_refuses_seed_exception_for_daily_proposals(self):
        import subprocess

        self.write()
        output = self.directory / "proposal"
        shared = shared_python_path(Path(__file__).resolve().parents[1]).parents[3]
        result = subprocess.run(
            [
                sys.executable,
                str(shared / "scripts/catalog/propose.py"),
                str(self.directory),
                "--source",
                "agentbot=/missing",
                "--output",
                str(output),
                "--seed-compatible-pr-extension",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("seed-only", result.stderr)
        self.assertFalse(output.exists())

    def test_unapproved_daily_catalog_requires_explicit_initial_seed_without_mutation(self):
        from catalog_proposal import propose

        self.write(approve=False)
        output = self.directory / "proposal"
        with self.assertRaisesRegex(CatalogError, "initial seed"):
            propose(self.directory, {}, output, seed=False)
        self.assertFalse(output.exists())
        self.assertFalse((self.directory / "approval").exists())

    def test_changed_source_or_existing_output_stops_before_installing_tools(self):
        from catalog_proposal import propose

        roots = self.sources()
        self.write(approve=False)
        (roots["agentbot"] / "source.txt").write_text("dirty source")
        output = self.directory / "proposal"
        with self.assertRaisesRegex(CatalogError, "clean"):
            propose(self.directory, roots, output, seed=True)
        self.assertFalse(output.exists())
        output.mkdir()
        (output / "keep").write_text("user-owned")
        with self.assertRaisesRegex(CatalogError, "exists"):
            propose(self.directory, roots, output, seed=True)
        self.assertEqual("user-owned", (output / "keep").read_text())

    def test_bootstrap_failure_keeps_its_bounded_diagnostic_without_publishing(self):
        from types import SimpleNamespace

        from catalog_proposal import _install_tools

        root = self.directory / "dotfiles"
        script = root / "scripts/catalog/bootstrap-tools.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\nprintf 'fixture artifact download failed\\n'\nexit 27\n")
        temporary = self.directory / "proposal-work"
        temporary.mkdir()
        with self.assertRaisesRegex(CatalogError, "fixture artifact download failed"):
            _install_tools(
                SimpleNamespace(directory=self.directory, digest="a" * 64),
                {"dotfiles": root},
                temporary,
            )
        self.assertFalse((self.directory / "approval.json").exists())
