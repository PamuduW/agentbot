"""Ticket M4: the schema 3 validator: tiers, registry, and supersession."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src import memory
from src import memory_retrieve as retrieve
from tests.test_memory import P_ALPHA, VAULT_ID, note, rules, uid, v3, v3_vault


class V3ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.vault = v3_vault(self.tmp / "v")

    def test_a_v3_tree_is_valid(self) -> None:
        report = memory.validate(self.vault.root)
        self.assertEqual([], report.findings)
        self.assertEqual(3, report.schema)
        self.assertEqual(1, sum(1 for r in report.records if r.draft))

    def test_the_marker_needs_a_vault_id_only_at_v3(self) -> None:
        for text in (
            '{"agentbot_memory_schema": 3}',
            '{"agentbot_memory_schema": 3, "vault_id": "not-a-uuid"}',
            '{"agentbot_memory_schema": 2, "vault_id": "' + VAULT_ID + '"}',
        ):
            with self.subTest(text=text):
                self.vault.write(".meta/vault.json", text)
                with self.assertRaises(memory.MemoryVaultError):
                    memory.read_marker(self.vault.root)

    def test_scope_follows_the_tier(self) -> None:
        cases = {
            "core/lessons/project-scoped.md": note(
                v3("lesson", uid(20), "project", projects=["alpha"])
            ),
            "core/user/profile.md": note(v3("profile", uid(2), "shared")),
            "projects/alpha/lessons/global.md": note(v3("lesson", uid(21), "global")),
            "projects/alpha/lessons/wrong-folder.md": note(
                v3("lesson", uid(22), "project", projects=["beta"])
            ),
        }
        for path, text in cases.items():
            self.vault.write(path, text)
        found = rules(memory.validate(self.vault.root))
        for path in cases:
            with self.subTest(path=path):
                self.assertIn("MEMORY_SCOPE", found[path])

    def test_only_v3_locations_hold_records(self) -> None:
        project = {"scope": "project", "projects": ["alpha"]}
        cases = {
            "projects/alpha/random.md": note(v3("lesson", uid(30), **project)),
            "core/loose.md": note(v3("lesson", uid(31), "global")),
            "drafts/old.md": note(v3("lesson", uid(32), "global", status="draft")),
            "preferences.md": note(v3("preference", uid(33), "global")),
        }
        for path, text in cases.items():
            self.vault.write(path, text)
        found = rules(memory.validate(self.vault.root))
        for path in cases:
            with self.subTest(path=path):
                self.assertIn("MEMORY_LOCATION", found[path])

    def test_proposals_are_drafts_of_core_types(self) -> None:
        self.vault.write(
            "proposals/core/lessons/accepted.md", note(v3("lesson", uid(40), "global"))
        )
        self.vault.write(
            "proposals/core/x/context.md", note(v3("context", uid(41), "global", status="draft"))
        )
        found = rules(memory.validate(self.vault.root))
        self.assertIn("MEMORY_STATUS", found["proposals/core/lessons/accepted.md"])
        self.assertIn("MEMORY_TYPE", found["proposals/core/x/context.md"])

    def test_registry_and_project_folders_agree(self) -> None:
        project = {"scope": "project", "projects": ["beta"]}
        self.vault.write("projects/beta/lessons/l.md", note(v3("lesson", uid(50), **project)))
        found = rules(memory.validate(self.vault.root))
        self.assertIn("MEMORY_PROJECT_UNREGISTERED", found["projects/beta"])
        (self.vault.root / "projects/alpha/project.md").unlink()
        self.assertIn(
            "MEMORY_PROJECT_IDENTITY", rules(memory.validate(self.vault.root))["projects/alpha"]
        )
        self.vault.write(
            "projects/alpha/project.md", note(v3("project", uid(51), "project", projects=["alpha"]))
        )
        self.assertIn(
            "MEMORY_PROJECT_IDENTITY",
            rules(memory.validate(self.vault.root))["projects/alpha/project.md"],
        )

    def test_a_bad_or_colliding_registry_is_an_error(self) -> None:
        registry = {
            "projects": [
                {"id": P_ALPHA, "origin": "github.com/me/alpha", "folder": "alpha"},
                {"id": uid(60), "origin": "github.com/me/alpha", "folder": "other"},
            ]
        }
        self.vault.write(".meta/projects.json", json.dumps(registry))
        self.assertIn(
            "MEMORY_REGISTRY", rules(memory.validate(self.vault.root))[".meta/projects.json"]
        )
        self.vault.write(".meta/projects.json", json.dumps({"projects": [{"id": "x"}]}))
        self.assertIn(
            "MEMORY_REGISTRY", rules(memory.validate(self.vault.root))[".meta/projects.json"]
        )
        self.vault.write(".meta/other.json", "{}")
        self.assertIn(
            "MEMORY_FILE_TYPE", rules(memory.validate(self.vault.root))[".meta/other.json"]
        )

    def test_supersession_stays_in_its_tier_and_project(self) -> None:
        project = {"scope": "project", "projects": ["alpha"]}
        self.vault.write(
            "core/lessons/2026-09-27-core.md",
            note(v3("lesson", uid(3), "global", status="superseded")),
        )
        self.vault.write(
            "projects/alpha/lessons/cross.md",
            note(v3("lesson", uid(70), **project, supersedes=[uid(3)])),
        )
        self.assertIn(
            "MEMORY_SUPERSEDES_TIER",
            rules(memory.validate(self.vault.root))["projects/alpha/lessons/cross.md"],
        )
        (self.vault.root / "projects/alpha/lessons/cross.md").unlink()
        self.vault.write(
            "projects/alpha/lessons/2026-09-27-l.md",
            note(v3("lesson", uid(6), **project, status="superseded")),
        )
        self.vault.write(
            "projects/alpha/lessons/same.md",
            note(v3("lesson", uid(71), **project, supersedes=[uid(6)])),
        )
        self.assertEqual([], memory.validate(self.vault.root).findings)

    def test_retrieval_reads_v3_tiers(self) -> None:
        core_only = retrieve.search(self.vault.root, "lesson", retrieve.Request(), limit=50)
        paths = {hit.record.path for hit in core_only.hits}
        self.assertIn("core/lessons/2026-09-27-core.md", paths)
        self.assertFalse(any(p.startswith("projects/") for p in paths))
        self.assertFalse(any(p.startswith("proposals/") for p in paths))
        with_project = retrieve.search(
            self.vault.root, "lesson", retrieve.Request(project="alpha"), limit=50
        )
        self.assertIn(
            "projects/alpha/lessons/2026-09-27-l.md", {h.record.path for h in with_project.hits}
        )


if __name__ == "__main__":
    unittest.main()
