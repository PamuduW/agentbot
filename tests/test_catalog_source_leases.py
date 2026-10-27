from __future__ import annotations

import importlib
import unittest
from dataclasses import replace

from src.command_runner import CommandRunner

from . import test_catalog_node_runtime as fixtures


class CatalogSourceLeaseTests(unittest.TestCase):
    setUp = fixtures.PrivateNodeRuntimeTests.setUp
    git = fixtures.PrivateNodeRuntimeTests.git

    def module(self):
        try:
            return importlib.import_module("catalog_source_lease")
        except ImportError:
            self.fail("canonical shared source leasing is missing")

    def test_named_owner_lease_supports_agentbot_without_requiring_installed_dotfiles(self):
        module = self.module()
        root = self.root / "agentbot"
        root.mkdir()
        self.git(root, "init", "-q", "-b", "main")
        self.git(root, "remote", "add", "origin", "https://github.com/PamuduW/agentbot.git")
        (root / "marker").write_text("owner source only")
        self.git(root, "add", ".")
        self.git(
            root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "owner source",
        )
        source = {
            "agentbot": self.git(root, "rev-parse", "HEAD"),
            **{
                key: value
                for key, value in self.snapshot.data["sources"].items()
                if key != "agentbot"
            },
        }
        snapshot = replace(self.snapshot, data={**self.snapshot.data, "sources": source})
        with module.lease_catalog_sources(
            snapshot,
            self.sources["dotfiles-shared"],
            CommandRunner(),
            names=("agentbot", "dotfiles-shared"),
        ) as roots:
            self.assertEqual({"agentbot", "dotfiles-shared"}, set(roots))
            self.assertEqual(root, roots["agentbot"])
            self.assertEqual(source["agentbot"], self.git(roots["agentbot"], "rev-parse", "HEAD"))
        self.assertFalse((self.snapshot_dir.parent / "sources").exists())

    def test_undeclared_or_duplicate_source_identity_is_rejected_before_cache_creation(self):
        module = self.module()
        for names in ((), ("agent-memory",), ("../agentbot",), ("agentbot", "agentbot")):
            with self.subTest(names=names), self.assertRaises(ValueError):
                with module.lease_catalog_sources(
                    self.snapshot, self.sources["dotfiles-shared"], CommandRunner(), names=names
                ):
                    self.fail("invalid source identity was leased")
        self.assertFalse((self.snapshot_dir.parent / "sources").exists())
