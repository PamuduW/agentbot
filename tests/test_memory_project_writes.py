"""Ticket M5: autonomous project-memory writes on a schema 3 vault."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory
from src import memory_project_writes as writes
from src import memory_retrieve as retrieve
from src import memory_sync as sync
from tests.support import run_cli_main
from tests.test_memory import FAKE_GITHUB_TOKEN, P_ALPHA, old_schema_vault, uid, v3_vault
from tests.test_memory_projects import repo
from tests.test_memory_sync import GIT, git

LESSON = "projects/alpha/lessons/2026-09-27-l.md"


class WritesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        seed = v3_vault(self.tmp / "seed")
        seed.commit()
        self.origin = self.tmp / "origin.git"
        subprocess.run(
            [*GIT, "clone", "-q", "--bare", str(seed.root), str(self.origin)],
            check=True,
            capture_output=True,
        )
        self.a, self.b = self.tmp / "a", self.tmp / "b"
        for clone in (self.a, self.b):
            subprocess.run(
                [*GIT, "clone", "-q", str(self.origin), str(clone)], check=True, capture_output=True
            )
        self.config = self.tmp / "config"
        self.code = repo(self.tmp / "code/alpha", "git@github.com:me/alpha.git")

    def head(self, root: Path) -> str:
        return git(root, "rev-parse", "HEAD").strip()

    def valid(self, root: Path) -> None:
        self.assertEqual([], memory.validate(root).findings)


class AddAndEditTests(WritesTestCase):
    def test_add_writes_a_valid_record_committed_locally(self) -> None:
        result = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Cache the build",
            body="Use ccache.",
            tags=["build"],
        )
        path = result["path"]
        self.assertTrue(
            path.startswith("projects/alpha/lessons/") and path.endswith("-cache-the-build.md")
        )
        self.valid(self.a)
        record = next(r for r in memory.validate(self.a).records if r.path == path)
        self.assertEqual(
            (result["id"], "project", ("alpha",), ("build",)),
            (record.id, record.scope, record.projects, record.tags),
        )
        self.assertEqual(1, len(sync.pending(self.a)))
        self.assertIn(f"Agentbot-Op: {result['op']}", git(self.a, "log", "-1", "--format=%B"))
        sync.sync(self.a)
        sync.sync(self.b)
        self.assertTrue((self.b / path).exists())

    def test_edit_keeps_identity_and_front_matter(self) -> None:
        before = next(r for r in memory.validate(self.a).records if r.path == LESSON)
        writes.edit(
            self.a,
            self.code,
            self.config,
            "lessons/2026-09-27-l.md",
            body="New body.",
            title="Renamed lesson",
        )
        after = next(r for r in memory.validate(self.a).records if r.path == LESSON)
        self.assertEqual((before.id, before.date), (after.id, after.date))
        self.assertEqual("Renamed lesson", after.title)
        self.assertIn("New body.", (self.a / LESSON).read_text())
        self.valid(self.a)

    def test_context_is_created_then_replaced_with_a_stable_id(self) -> None:
        first = next(
            r for r in memory.validate(self.a).records if r.path.endswith("active-context.md")
        )
        writes.set_context(self.a, self.code, self.config, "## Now\nShipping M5.\n")
        second = next(
            r for r in memory.validate(self.a).records if r.path.endswith("active-context.md")
        )
        self.assertEqual(first.id, second.id)
        self.assertIn("Shipping M5.", (self.a / "projects/alpha/active-context.md").read_text())
        self.valid(self.a)

    def test_supersede_is_one_all_or_nothing_change(self) -> None:
        result = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Better lesson",
            body="b",
            supersedes=uid(6),
        )
        records = {r.path: r for r in memory.validate(self.a).records}
        self.assertEqual("superseded", records[LESSON].status)
        self.assertEqual((uid(6),), records[result["path"]].supersedes)
        self.valid(self.a)

    def test_supersession_never_leaves_the_project(self) -> None:
        head = self.head(self.a)
        for target in (uid(3), uid(99)):  # a core lesson; an unknown ID
            with self.subTest(target=target), self.assertRaises(memory.MemoryVaultError):
                writes.add(
                    self.a,
                    self.code,
                    self.config,
                    kind="lesson",
                    title="x",
                    body="x",
                    supersedes=target,
                )
        self.assertEqual(head, self.head(self.a))


class ContainmentTests(WritesTestCase):
    def test_paths_never_leave_the_current_project(self) -> None:
        head = self.head(self.a)
        attempts = [
            lambda: writes.edit(
                self.a, self.code, self.config, "core/lessons/2026-09-27-core.md", body="x"
            ),
            lambda: writes.edit(
                self.a, self.code, self.config, "../../core/user/preferences.md", body="x"
            ),
            lambda: writes.delete(self.a, self.code, self.config, "projects/beta/x.md"),
            lambda: writes.move(
                self.a, self.code, self.config, "lessons/2026-09-27-l.md", "../../core/lessons/x.md"
            ),
            lambda: writes.move(self.a, self.code, self.config, "project.md", "notes/p.md"),
            lambda: writes.delete(self.a, self.code, self.config, "project.md"),
            lambda: writes.move(
                self.a, self.code, self.config, "lessons/2026-09-27-l.md", "active-context.md"
            ),
        ]
        for index, attempt in enumerate(attempts):
            with self.subTest(index=index), self.assertRaises(memory.MemoryVaultError):
                attempt()
        self.assertEqual(head, self.head(self.a))

    def test_move_and_delete_inside_the_project(self) -> None:
        writes.move(self.a, self.code, self.config, "lessons/2026-09-27-l.md", "lessons/renamed.md")
        self.assertFalse((self.a / LESSON).exists())
        self.assertTrue((self.a / "projects/alpha/lessons/renamed.md").exists())
        writes.delete(self.a, self.code, self.config, "notes/n.md")
        self.assertFalse((self.a / "projects/alpha/notes/n.md").exists())
        self.valid(self.a)

    def test_secrets_and_invalid_records_are_never_committed(self) -> None:
        head = self.head(self.a)
        with self.assertRaises(memory.MemoryVaultError):
            writes.add(
                self.a,
                self.code,
                self.config,
                kind="lesson",
                title="leak",
                body=f"key {FAKE_GITHUB_TOKEN}",
            )
        with self.assertRaises(memory.MemoryVaultError):
            writes.add(self.a, self.code, self.config, kind="lesson", title=" untrimmed", body="x")
        with self.assertRaises(memory.MemoryVaultError):
            writes.add(
                self.a, self.code, self.config, kind="lesson", title="t", body="x", tags=["Bad Tag"]
            )
        self.assertEqual(head, self.head(self.a))
        self.assertEqual([], sync.pending(self.a))

    def test_unresolved_repositories_and_other_vaults_are_refused(self) -> None:
        stranger = repo(self.tmp / "code/stranger", "https://github.com/me/stranger")
        outside = self.tmp / "plain"
        outside.mkdir()
        old = old_schema_vault(self.tmp / "v2")
        for root, cwd in ((self.a, stranger), (self.a, outside), (old.root, self.code)):
            with self.subTest(cwd=str(cwd)), self.assertRaises(memory.MemoryVaultError):
                writes.add(root, cwd, self.config, kind="lesson", title="x", body="x")

    def test_the_off_switch_stops_writes_but_not_reads(self) -> None:
        with patch.dict(os.environ, {writes.FLAG: "off"}):
            with self.assertRaises(memory.MemoryVaultError) as raised:
                writes.add(self.a, self.code, self.config, kind="lesson", title="x", body="x")
            self.assertIn("turned off", str(raised.exception))
            hits = retrieve.search(
                self.a, "lesson", retrieve.Request(project="alpha"), limit=50
            ).hits
            self.assertTrue(hits)


class ForgetAndConcurrencyTests(WritesTestCase):
    def test_forget_removes_the_folder_keeps_identity_and_reverts(self) -> None:
        before = git(self.a, "ls-tree", "-r", "HEAD", "projects/alpha")
        result = writes.forget(self.a, self.code, self.config)
        self.assertFalse((self.a / "projects/alpha").exists())
        self.assertIn(P_ALPHA, (self.a / ".meta/projects.json").read_text())
        self.valid(self.a)
        subprocess.run(
            [*GIT, "-C", str(self.a), "revert", "--no-edit", result["commit"]],
            check=True,
            capture_output=True,
        )
        self.assertEqual(before, git(self.a, "ls-tree", "-r", "HEAD", "projects/alpha"))

    def test_concurrent_edits_to_one_record_conflict_instead_of_overwriting(self) -> None:
        writes.edit(self.a, self.code, self.config, "lessons/2026-09-27-l.md", body="A's version")
        op_b = writes.edit(
            self.b, self.code, self.tmp / "config-b", "lessons/2026-09-27-l.md", body="B's version"
        )
        sync.sync(self.a)
        result = sync.sync(self.b)
        self.assertEqual([op_b["op"]], [c["op"] for c in result.conflicts])
        self.assertIn("A's version", (self.b / LESSON).read_text())

    def test_a_contested_supersession_lands_whole_or_not_at_all(self) -> None:
        writes.edit(
            self.b, self.code, self.tmp / "config-b", "lessons/2026-09-27-l.md", body="edited on B"
        )
        sync.sync(self.b)
        result_a = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Replacement",
            body="r",
            supersedes=uid(6),
        )
        outcome = sync.sync(self.a)
        self.assertEqual([result_a["op"]], [c["op"] for c in outcome.conflicts])
        self.assertFalse((self.a / result_a["path"]).exists())
        self.assertIn("status: accepted", (self.a / LESSON).read_text())
        self.valid(self.a)


class ProjectWriteCliTests(WritesTestCase):
    def _cli(self, cwd: Path, *argv: str, stdin: bytes = b"") -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            AGENTBOT_MEMORY_DIR=str(self.a),
            AGENTBOT_CALLER_PWD=str(cwd),
            XDG_CONFIG_HOME=str(self.tmp / "xdg"),
        )
        stream = io.TextIOWrapper(io.BytesIO(stdin))
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin", stream):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_cli_add_register_and_forget_preview(self) -> None:
        rc, stdout = self._cli(
            self.code,
            "memory",
            "project",
            "add",
            "--kind",
            "decision",
            "--title",
            "Use Vite",
            "--stdin",
            "--json",
            stdin=b"Because it is fast.\n",
        )
        self.assertEqual(0, rc, stdout)
        self.assertTrue(json.loads(stdout)["path"].startswith("projects/alpha/decisions/"))
        fresh = repo(self.tmp / "code/fresh", "https://github.com/me/fresh")
        rc, stdout = self._cli(fresh, "memory", "project", "register")
        self.assertEqual(0, rc, stdout)
        self.assertTrue((self.a / "projects/fresh/project.md").exists())
        self.valid(self.a)
        rc, stdout = self._cli(fresh, "memory", "project", "--json")
        self.assertEqual("resolved", json.loads(stdout)["state"])
        rc, stdout = self._cli(self.code, "memory", "project", "forget", "--json")
        self.assertEqual("preview", json.loads(stdout)["state"])
        self.assertTrue((self.a / "projects/alpha").exists())


if __name__ == "__main__":
    unittest.main()


class CommitSubjectTests(WritesTestCase):
    def test_subjects_name_the_changed_record(self) -> None:
        added = writes.add(self.a, self.code, self.config, kind="lesson", title="Named", body="n")
        self.assertEqual(
            f"memory(project): update {added['path']}",
            git(self.a, "log", "-1", "--format=%s").strip(),
        )
        writes.delete(self.a, self.code, self.config, added["path"].split("/", 2)[2])
        self.assertEqual(
            f"memory(project): delete {added['path']}",
            git(self.a, "log", "-1", "--format=%s").strip(),
        )
