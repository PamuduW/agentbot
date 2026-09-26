"""A sanitized v1-to-v2 migration rehearsal."""

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
from src import memory_migrate as migrate
from src import memory_retrieve as retrieve
from tests.support import run_cli_main
from tests.test_memory import SENTINEL, _tree_digest, note, v1, v1_vault


class MigrateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.vault = v1_vault(self.tmp / "v")
        # A realistic template, which migration updates too.
        self.vault.write(
            "templates/decision.md",
            "---\nschema: 1\ntype: decision\ntitle: {{title}}\ndate: {{date}}\nstatus: accepted\n"
            "projects: []\ntags: []\n---\n\n# {{title}}\n",
        )
        self.vault.commit()
        self.config = self.tmp / "config"
        self.root = self.vault.root

    def chosen(self, **scopes: str) -> dict:
        mapping = migrate.plan(self.root)
        defaults = {
            "active-context.md": "global",
            "decisions/2026-09-23-a-decision.md": "project",
            "lessons/2026-09-23-a-lesson.md": "shared",
            "drafts/2026-09-23-a-draft.md": "global",
        }
        defaults.update(scopes)
        for entry in mapping["entries"]:
            if entry["scope"] is None:
                entry["scope"] = defaults[entry["path"]]
        return mapping

    def apply(self, mapping: dict, **kwargs: object) -> migrate.MigrationReport:
        return migrate.apply(
            self.root,
            mapping,
            snapshot=self.tmp / "snap",
            config_home=self.config,
            **kwargs,  # type: ignore[arg-type]
        )


class PlanTests(MigrateTestCase):
    def test_plan_fixes_rule_scopes_and_leaves_the_rest_to_a_human(self) -> None:
        before = _tree_digest(self.root)
        mapping = migrate.plan(self.root)
        self.assertEqual(before, _tree_digest(self.root))
        scopes = {entry["path"]: entry["scope"] for entry in mapping["entries"]}
        self.assertEqual("global", scopes["preferences.md"])
        self.assertEqual("project", scopes["projects/alpha/2026-09-23-a-project.md"])
        for path in (
            "active-context.md",
            "lessons/2026-09-23-a-lesson.md",
            "drafts/2026-09-23-a-draft.md",
        ):
            self.assertIsNone(scopes[path])  # never inferred from an empty project list
        self.assertTrue(any(entry["draft"] for entry in mapping["entries"]))
        ids = [entry["id"] for entry in mapping["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            ["templates/decision.md", "templates/lesson.md"],
            [t["path"] for t in mapping["templates"]],
        )
        self.assertNotIn(SENTINEL, json.dumps(mapping))

    def test_the_mapping_file_is_private_and_never_overwritten(self) -> None:
        path = self.tmp / "private" / "mapping.json"
        migrate.write_mapping(migrate.plan(self.root), path)
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        with self.assertRaises(memory.MemoryVaultError):
            migrate.write_mapping(migrate.plan(self.root), path)

    def test_malformed_records_block_planning(self) -> None:
        self.vault.write("lessons/bad.md", note(v1("lesson", date="2026-02-30")))
        with self.assertRaises(memory.MemoryVaultError) as raised:
            migrate.plan(self.root)
        self.assertIn("finding", str(raised.exception))

    def test_a_v2_vault_is_not_migrated(self) -> None:
        mapping = self.chosen()
        self.assertEqual("applied", self.apply(mapping, confirm=True).state)
        with self.assertRaises(memory.MemoryVaultError):
            migrate.plan(self.root)


class CheckTests(MigrateTestCase):
    def test_unchosen_scopes_are_unresolved(self) -> None:
        report, _ = migrate.check(self.root, migrate.plan(self.root))
        self.assertEqual("unresolved", report.state)
        self.assertEqual(4, len(report.unresolved))

    def test_a_scope_the_schema_rejects_fails_the_candidate(self) -> None:
        # The v1 decision lists one project, so "global" is not allowed for it.
        report, _ = migrate.check(
            self.root, self.chosen(**{"decisions/2026-09-23-a-decision.md": "global"})
        )
        self.assertEqual("invalid", report.state)
        self.assertTrue(any("MEMORY_SCOPE" in problem for problem in report.problems))
        self.assertNotIn(SENTINEL, json.dumps(migrate.report_json(report)))

    def test_a_ready_candidate_leaves_the_vault_alone(self) -> None:
        before = _tree_digest(self.root)
        report, converted = migrate.check(self.root, self.chosen())
        self.assertEqual("ready", report.state, report.problems)
        self.assertEqual(before, _tree_digest(self.root))
        self.assertIn("templates/decision.md", converted)

    def test_edits_and_new_records_make_the_mapping_stale(self) -> None:
        mapping = self.chosen()
        with open(self.root / "lessons/2026-09-23-a-lesson.md", "a", encoding="utf-8") as stream:
            stream.write("A later edit.\n")
        self.vault.write("lessons/new.md", note(v1("lesson")))
        report, _ = migrate.check(self.root, mapping)
        self.assertEqual("stale", report.state)
        self.assertTrue(any("changed since" in p for p in report.problems))
        self.assertTrue(any("not in the mapping" in p for p in report.problems))


class ApplyTests(MigrateTestCase):
    def test_apply_converts_only_front_matter_and_changes_the_marker_last(self) -> None:
        mapping = self.chosen()
        body_before = (self.root / "lessons/2026-09-23-a-lesson.md").read_text().split("---", 2)[2]
        preview = self.apply(mapping)
        self.assertEqual("ready", preview.state)
        self.assertFalse((self.tmp / "snap").exists())
        report = self.apply(mapping, confirm=True)
        self.assertEqual("applied", report.state, report.problems)
        self.assertEqual(memory.MARKER, report.changed[-1])
        self.assertEqual(2, memory.read_marker(self.root))
        result = memory.validate(self.root)
        self.assertEqual([], result.findings)
        by_path = {record.path: record for record in result.records}
        entry = {e["path"]: e for e in mapping["entries"]}
        for path, record in by_path.items():
            self.assertEqual((entry[path]["id"], entry[path]["scope"]), (record.id, record.scope))
        lesson = (self.root / "lessons/2026-09-23-a-lesson.md").read_text()
        self.assertEqual(body_before, lesson.split("---", 2)[2])
        self.assertIn("schema: 2", (self.root / "templates/decision.md").read_text())
        self.assertFalse((self.root / memory.MIGRATION_IN_PROGRESS).exists())
        staged = subprocess.run(
            ["git", "-C", str(self.root), "diff", "--cached", "--name-only"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(b"", staged.stdout)
        # Retrieval works on the migrated tree, with v2 scope.
        hits = retrieve.search(self.root, "record", retrieve.Request(), limit=50).hits
        self.assertIn("lessons/2026-09-23-a-lesson.md", [hit.record.path for hit in hits])

    def test_apply_refuses_a_dirty_or_detached_checkout(self) -> None:
        self.vault.write("lessons/uncommitted.md", "x")
        with self.assertRaises(memory.MemoryVaultError):
            self.apply(self.chosen(), confirm=True)
        (self.root / "lessons/uncommitted.md").unlink()
        head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.root), "checkout", "-q", "--detach", head], check=True
        )
        with self.assertRaises(memory.MemoryVaultError):
            self.apply(self.chosen(), confirm=True)

    def test_a_concurrent_edit_mid_apply_rolls_back_without_losing_it(self) -> None:
        mapping = self.chosen()
        before = _tree_digest(self.root)
        victim = self.root / "preferences.md"

        def edit(path: str) -> None:
            if path == "active-context.md":
                with open(victim, "a", encoding="utf-8") as stream:
                    stream.write("A person's edit mid-migration.\n")

        report = self.apply(mapping, confirm=True, after_file=edit)
        self.assertEqual("rolled-back", report.state)
        self.assertIn("changed during migration", report.problems[0])
        self.assertIn("A person's edit mid-migration.", victim.read_text())
        self.assertEqual(1, memory.read_marker(self.root))
        self.assertEqual([], memory.validate(self.root).findings)
        victim.write_text(victim.read_text().replace("A person's edit mid-migration.\n", ""))
        self.assertEqual(before, _tree_digest(self.root))

    def test_an_interruption_rolls_back_and_reads_fail_closed_meanwhile(self) -> None:
        mapping = self.chosen()
        before = _tree_digest(self.root)
        seen: list[str] = []

        def interrupt(path: str) -> None:
            with self.assertRaises(memory.MemoryVaultError) as raised:
                memory.validate(self.root)
            seen.append(str(raised.exception))
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.apply(mapping, confirm=True, after_file=interrupt)
        self.assertIn("migration is in progress", seen[0])
        self.assertEqual(before, _tree_digest(self.root))
        self.assertEqual([], memory.validate(self.root).findings)

    def test_a_hard_crash_leaves_reads_closed_until_rollback(self) -> None:
        mapping = self.chosen()
        before = _tree_digest(self.root)

        # A process killed mid-apply runs no cleanup: its first file stays
        # converted and the in-progress guard stays behind.
        def killed(path: str) -> None:
            raise KeyboardInterrupt

        with patch.object(migrate, "_undo", return_value=[]), self.assertRaises(KeyboardInterrupt):
            self.apply(mapping, confirm=True, after_file=killed)
        (self.root / memory.MIGRATION_IN_PROGRESS).touch()
        self.assertNotEqual(before, _tree_digest(self.root))
        self.assertTrue((self.root / memory.MIGRATION_IN_PROGRESS).exists())
        with self.assertRaises(memory.MemoryVaultError):
            retrieve.search(self.root, "record", retrieve.Request())
        preview = migrate.rollback(self.root, self.tmp / "snap", config_home=self.config)
        self.assertEqual("ready", preview.state)
        done = migrate.rollback(self.root, self.tmp / "snap", config_home=self.config, confirm=True)
        self.assertEqual("rolled-back", done.state, done.problems)
        self.assertEqual([], done.problems)
        self.assertEqual(before, _tree_digest(self.root))


class RollbackAndRecoveryTests(MigrateTestCase):
    def test_v1_rollback_after_a_completed_migration(self) -> None:
        before = _tree_digest(self.root)
        self.assertEqual("applied", self.apply(self.chosen(), confirm=True).state)
        report = migrate.rollback(
            self.root, self.tmp / "snap", config_home=self.config, confirm=True
        )
        self.assertEqual([], report.problems)
        self.assertEqual(1, memory.read_marker(self.root))
        self.assertEqual(before, _tree_digest(self.root))

    def test_rollback_never_overwrites_a_later_edit(self) -> None:
        self.apply(self.chosen(), confirm=True)
        lesson = self.root / "lessons/2026-09-23-a-lesson.md"
        with open(lesson, "a", encoding="utf-8") as stream:
            stream.write("Edited after migration.\n")
        report = migrate.rollback(
            self.root, self.tmp / "snap", config_home=self.config, confirm=True
        )
        self.assertTrue(any("lessons/2026-09-23-a-lesson.md" in p for p in report.problems))
        self.assertIn("Edited after migration.", lesson.read_text())
        self.assertIn("schema: 1", (self.root / "preferences.md").read_text())

    def test_the_snapshot_is_private_verified_and_outside_the_vault(self) -> None:
        self.apply(self.chosen(), confirm=True)
        snap = self.tmp / "snap"
        self.assertEqual(0o700, stat.S_IMODE(os.stat(snap).st_mode))
        self.assertTrue(all(stat.S_IMODE(os.stat(p).st_mode) == 0o600 for p in snap.iterdir()))
        fresh = v1_vault(self.tmp / "other")
        fresh.commit()
        mapping = migrate.plan(fresh.root)
        for entry in mapping["entries"]:
            entry["scope"] = entry["scope"] or entry["suggested"]
        with self.assertRaises(memory.MemoryVaultError) as raised:
            migrate.apply(
                fresh.root,
                mapping,
                snapshot=fresh.root / "snap",
                config_home=self.config,
                confirm=True,
            )
        self.assertIn("outside the vault", str(raised.exception))
        self.assertEqual(1, memory.read_marker(fresh.root))

    def test_a_pre_migration_backup_restores_v1_into_a_fresh_destination(self) -> None:
        backups.backup(self.root, self.tmp / "backup", apply=True)
        self.apply(self.chosen(), confirm=True)
        restored = backups.restore(
            self.tmp / "backup", self.tmp / "restored", active=self.root, apply=True
        )
        self.assertEqual("restored", restored.state)
        self.assertEqual(1, memory.read_marker(self.tmp / "restored"))
        self.assertEqual(2, memory.read_marker(self.root))


class MigrateCliTests(MigrateTestCase):
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

    def test_cli_flow(self) -> None:
        rc, stdout = self._cli("memory", "migrate", "plan", "--write", "mapping.json")
        self.assertEqual(0, rc, stdout)
        self.assertIn("choose a scope", stdout)
        rc, _ = self._cli("memory", "migrate", "check", "--mapping", "mapping.json")
        self.assertEqual(1, rc)
        mapping = json.loads((self.tmp / "mapping.json").read_text())
        choices = {
            "active-context.md": "global",
            "decisions/2026-09-23-a-decision.md": "project",
            "lessons/2026-09-23-a-lesson.md": "shared",
            "drafts/2026-09-23-a-draft.md": "global",
        }
        for entry in mapping["entries"]:
            entry["scope"] = entry["scope"] or choices[entry["path"]]
        (self.tmp / "mapping.json").write_text(json.dumps(mapping))
        rc, stdout = self._cli("memory", "migrate", "check", "--mapping", "mapping.json")
        self.assertEqual(0, rc, stdout)
        rc, stdout = self._cli(
            "memory", "migrate", "apply", "--mapping", "mapping.json", "--snapshot", "snap"
        )
        self.assertEqual(0, rc, stdout)
        self.assertIn("Preview only", stdout)
        self.assertEqual(1, memory.read_marker(self.root))
        rc, stdout = self._cli(
            "memory", "migrate", "apply", "--mapping", "mapping.json", "--snapshot", "snap", "--yes"
        )
        self.assertEqual(0, rc, stdout)
        self.assertEqual(2, memory.read_marker(self.root))
        rc, stdout = self._cli("memory", "migrate", "rollback", "--snapshot", "snap", "--yes")
        self.assertEqual(0, rc, stdout)
        self.assertEqual(1, memory.read_marker(self.root))


if __name__ == "__main__":
    unittest.main()
