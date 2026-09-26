"""Ticket M4: the schema 3 validator and the v2-to-v3 migration."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory
from src import memory_migrate as migrate
from src import memory_migrate3 as migrate3
from src import memory_retrieve as retrieve
from tests.support import run_cli_main
from tests.test_memory import SENTINEL, VaultFixture, _tree_digest, note, rules

P_ALPHA = "a1a1a1a1-0000-4000-8000-000000000001"
VAULT_ID = "b2b2b2b2-0000-4000-8000-000000000002"


def uid(n: int) -> str:
    return f"{n:08x}-1111-4000-8000-000000000000"


def v3(kind: str, record_id: str, scope: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "schema": 3,
        "id": record_id,
        "type": kind,
        "title": f"A {kind}",
        "date": "2026-09-27",
        "status": "accepted",
        "scope": scope,
        "projects": [],
        "tags": [],
    }
    fields.update(overrides)
    return {k: v for k, v in fields.items() if v is not None}


def v3_vault(root: Path) -> VaultFixture:
    vault = VaultFixture(root, 2)
    for readme in ("decisions/README.md", "lessons/README.md", "drafts/README.md"):
        (root / readme).unlink()
    vault.write(".meta/vault.json", json.dumps({"agentbot_memory_schema": 3, "vault_id": VAULT_ID}))
    vault.write(
        ".meta/projects.json",
        json.dumps(
            {"projects": [{"id": P_ALPHA, "origin": "github.com/me/alpha", "folder": "alpha"}]}
        ),
    )
    vault.write("core/user/preferences.md", note(v3("preference", uid(1), "global")))
    vault.write("core/user/profile.md", note(v3("profile", uid(2), "global")))
    vault.write("core/lessons/2026-09-27-core.md", note(v3("lesson", uid(3), "global")))
    vault.write(
        "core/decisions/2026-09-27-shared.md",
        note(v3("decision", uid(4), "shared", projects=["alpha"])),
    )
    project = {"scope": "project", "projects": ["alpha"]}
    vault.write("projects/alpha/project.md", note(v3("project", P_ALPHA, **project)))
    vault.write("projects/alpha/active-context.md", note(v3("context", uid(5), **project)))
    vault.write(
        "projects/alpha/lessons/2026-09-27-l.md",
        note(v3("lesson", uid(6), **project, title="Alpha build lesson")),
    )
    vault.write("projects/alpha/notes/n.md", note(v3("project", uid(7), **project)))
    vault.write("proposals/core/lessons/p.md", note(v3("lesson", uid(8), "global", status="draft")))
    return vault


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


def v2_fixture(root: Path) -> VaultFixture:
    vault = VaultFixture(root, 2)
    vault.write("templates/decision.md", "---\nschema: 2\ntype: decision\ntitle: {{title}}\n---\n")
    vault.write("preferences.md", note(_v2("preference", uid(101), "global")))
    vault.write("active-context.md", note(_v2("context", uid(102), "global")))
    vault.write("decisions/2026-09-01-d.md", note(_v2("decision", uid(103), "global")))
    vault.write(
        "lessons/2026-09-02-l.md", note(_v2("lesson", uid(104), "project", projects=["alpha"]))
    )
    vault.write(
        "projects/alpha/2026-09-03-p.md",
        note(_v2("project", uid(105), "project", projects=["alpha"])),
    )
    vault.write(
        "drafts/2026-09-04-draft.md", note(_v2("lesson", uid(106), "global", status="draft"))
    )
    vault.commit()
    return vault


def _v2(kind: str, record_id: str, scope: str, **overrides: object) -> dict[str, object]:
    fields = v3(kind, record_id, scope, **overrides)
    fields["schema"] = 2
    return fields


class MigrationBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.vault = v2_fixture(self.tmp / "v")
        self.root = self.vault.root
        self.config = self.tmp / "config"

    def chosen(self) -> dict:
        mapping = migrate3.plan(self.root)
        for entry in mapping["entries"]:
            if entry["path"] == "active-context.md":
                entry["destination"] = "projects/alpha/active-context.md"
        for project in mapping["projects"]:
            project["origin"] = "git@github.com:Me/Alpha.git"
        return mapping

    def apply(self, mapping: dict, **kwargs: object) -> migrate.MigrationReport:
        return migrate3.apply(
            self.root, mapping, snapshot=self.tmp / "snap", config_home=self.config, **kwargs
        )  # type: ignore[arg-type]


class MigrationV3Tests(MigrationBase):
    def test_plan_fixes_what_it_can_and_asks_for_the_rest(self) -> None:
        mapping = migrate3.plan(self.root)
        destinations = {e["path"]: e["destination"] for e in mapping["entries"]}
        self.assertEqual("core/user/preferences.md", destinations["preferences.md"])
        self.assertEqual(
            "core/decisions/2026-09-01-d.md", destinations["decisions/2026-09-01-d.md"]
        )
        self.assertEqual(
            "projects/alpha/lessons/2026-09-02-l.md", destinations["lessons/2026-09-02-l.md"]
        )
        self.assertEqual(
            "projects/alpha/notes/2026-09-03-p.md", destinations["projects/alpha/2026-09-03-p.md"]
        )
        self.assertEqual(
            "proposals/core/lessons/2026-09-04-draft.md", destinations["drafts/2026-09-04-draft.md"]
        )
        self.assertIsNone(destinations["active-context.md"])
        self.assertEqual(
            ["alpha"], [p["folder"] for p in mapping["projects"] if p["origin"] is None]
        )
        self.assertNotIn(SENTINEL, json.dumps(mapping))

    def test_check_waits_for_choices_then_builds_a_valid_v3_candidate(self) -> None:
        before = _tree_digest(self.root)
        report, _ = migrate3.check(self.root, migrate3.plan(self.root))
        self.assertEqual("unresolved", report.state)
        report, _ = migrate3.check(self.root, self.chosen())
        self.assertEqual("ready", report.state, report.problems)
        self.assertEqual(before, _tree_digest(self.root))

    def test_apply_moves_records_into_tiers_and_keeps_bodies_and_ids(self) -> None:
        body_before = (self.root / "lessons/2026-09-02-l.md").read_text().split("---", 2)[2]
        mapping = self.chosen()
        report = self.apply(mapping, confirm=True)
        self.assertEqual("applied", report.state, report.problems)
        self.assertEqual(memory.MARKER, report.changed[-1])
        self.assertEqual(3, memory.read_marker(self.root))
        result = memory.validate(self.root)
        self.assertEqual([], result.findings)
        records = {r.path: r for r in result.records}
        moved = records["projects/alpha/lessons/2026-09-02-l.md"]
        self.assertEqual((uid(104), "project", ("alpha",)), (moved.id, moved.scope, moved.projects))
        self.assertEqual(body_before, (self.root / moved.path).read_text().split("---", 2)[2])
        self.assertEqual("project", records["projects/alpha/active-context.md"].scope)
        self.assertTrue(records["proposals/core/lessons/2026-09-04-draft.md"].draft)
        self.assertFalse((self.root / "lessons/2026-09-02-l.md").exists())
        self.assertFalse((self.root / "drafts").exists())
        registry = json.loads((self.root / ".meta/projects.json").read_text())["projects"]
        self.assertEqual(
            [("github.com/me/alpha", "alpha")], [(p["origin"], p["folder"]) for p in registry]
        )
        self.assertEqual(registry[0]["id"], records["projects/alpha/project.md"].id)
        self.assertIn("schema: 3", (self.root / "templates/decision.md").read_text())
        staged = subprocess.run(
            ["git", "-C", str(self.root), "diff", "--cached", "--name-only"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(b"", staged.stdout)
        self.assertFalse((self.root / memory.MIGRATION_IN_PROGRESS).exists())

    def test_rollback_restores_v2_byte_for_byte(self) -> None:
        before = _tree_digest(self.root)
        self.assertEqual("applied", self.apply(self.chosen(), confirm=True).state)
        preview = migrate3.rollback(self.root, self.tmp / "snap", config_home=self.config)
        self.assertEqual("ready", preview.state)
        done = migrate3.rollback(
            self.root, self.tmp / "snap", config_home=self.config, confirm=True
        )
        self.assertEqual([], done.problems)
        self.assertEqual(2, memory.read_marker(self.root))
        self.assertEqual(before, _tree_digest(self.root))

    def test_a_failure_mid_apply_undoes_everything(self) -> None:
        before = _tree_digest(self.root)
        with patch.object(migrate3, "_remove", side_effect=memory.MemoryVaultError("disk trouble")):
            report = self.apply(self.chosen(), confirm=True)
        self.assertEqual("rolled-back", report.state)
        self.assertEqual(before, _tree_digest(self.root))
        self.assertEqual(2, memory.read_marker(self.root))

    def test_retiring_a_record_keeps_it_in_git_history(self) -> None:
        mapping = self.chosen()
        for entry in mapping["entries"]:
            if entry["path"] == "active-context.md":
                entry["destination"] = "retire"
        self.assertEqual("applied", self.apply(mapping, confirm=True).state)
        self.assertFalse((self.root / "active-context.md").exists())
        shown = subprocess.run(
            ["git", "-C", str(self.root), "show", "HEAD:active-context.md"],
            capture_output=True,
            check=True,
        )
        self.assertIn(SENTINEL.encode(), shown.stdout)

    def test_a_cross_tier_supersession_is_caught_before_apply(self) -> None:
        self.vault.write(
            "decisions/2026-09-01-d.md",
            note(_v2("decision", uid(103), "global", status="superseded")),
        )
        self.vault.write(
            "decisions/2026-09-05-x.md",
            note(_v2("decision", uid(107), "project", projects=["alpha"], supersedes=[uid(103)])),
        )
        self.vault.commit()
        report, _ = migrate3.check(self.root, self.chosen())
        self.assertEqual("invalid", report.state)
        self.assertTrue(
            any("MEMORY_SUPERSEDES_TIER" in p for p in report.problems), report.problems
        )

    def test_a_stale_mapping_or_bad_origin_is_refused(self) -> None:
        mapping = self.chosen()
        with open(self.root / "preferences.md", "a", encoding="utf-8") as stream:
            stream.write("edited\n")
        report, _ = migrate3.check(self.root, mapping)
        self.assertEqual("stale", report.state)
        subprocess.run(
            ["git", "-C", str(self.root), "checkout", "--", "preferences.md"], check=True
        )
        mapping = self.chosen()
        mapping["projects"][0]["origin"] = "not a url"
        self.assertEqual("stale", migrate3.check(self.root, mapping)[0].state)


class MigrationV3CliTests(MigrationBase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            AGENTBOT_MEMORY_DIR=str(self.root),
            AGENTBOT_CALLER_PWD=str(self.tmp),
            XDG_CONFIG_HOME=str(self.config),
        )
        with patch.dict(os.environ, env, clear=True):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_cli_routes_a_v2_vault_to_the_v3_migration(self) -> None:
        rc, stdout = self._cli("memory", "migrate", "plan", "--json", "--write", "m.json")
        self.assertEqual(0, rc, stdout)
        self.assertEqual(["alpha"], json.loads(stdout)["projects_needing_origin"])
        mapping = json.loads((self.tmp / "m.json").read_text())
        self.assertEqual(3, mapping["target"])
        for entry in mapping["entries"]:
            entry["destination"] = entry["destination"] or "projects/alpha/active-context.md"
        mapping["projects"][0]["origin"] = "local"
        (self.tmp / "m.json").write_text(json.dumps(mapping))
        rc, stdout = self._cli(
            "memory", "migrate", "apply", "--mapping", "m.json", "--snapshot", "snap", "--yes"
        )
        self.assertEqual(0, rc, stdout)
        self.assertEqual(3, memory.read_marker(self.root))
        rc, stdout = self._cli("memory", "migrate", "rollback", "--snapshot", "snap", "--yes")
        self.assertEqual(0, rc, stdout)
        self.assertEqual(2, memory.read_marker(self.root))


if __name__ == "__main__":
    unittest.main()
