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
