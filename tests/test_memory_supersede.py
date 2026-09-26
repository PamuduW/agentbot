"""Superseding records as one reviewed, locked transition."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory, memory_approve
from tests.test_memory import ID_A, ID_B, ID_C, VaultFixture, _tree_digest, note, rules, v2

OLD_1 = "10000000-0000-4000-8000-000000000001"
OLD_2 = "10000000-0000-4000-8000-000000000002"
NEW = "20000000-0000-4000-8000-000000000001"
BODY = "## What changed\n\nThe newer finding.\n"


class SupersedeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.vault = VaultFixture(self.tmp / "v", 2)
        self.vault.write("preferences.md", note(v2("preference", ID_A, "global")))
        self.vault.write("lessons/old-1.md", note(v2("lesson", OLD_1, "global", title="Old one")))
        self.vault.write("lessons/old-2.md", note(v2("lesson", OLD_2, "global", title="Old two")))
        self.vault.commit()
        self.config = self.tmp / "config"

    def draft(self, targets: list[str], **fields: object) -> str:
        values: dict[str, object] = {
            "status": "draft",
            "title": "Newer lesson",
            "supersedes": targets,
        }
        values.update(fields)
        self.vault.write("drafts/new.md", note(v2("lesson", NEW, "global", **values), body=BODY))
        return "drafts/new.md"

    def approve(self, relative: str, **kwargs: object) -> memory_approve.ApprovalResult:
        return memory_approve.approve(self.vault.root, relative, config_home=self.config, **kwargs)  # type: ignore[arg-type]

    def status_of(self, relative: str) -> str:
        record = next(r for r in memory.validate(self.vault.root).records if r.path == relative)
        return record.status


class SupersedeTests(SupersedeTestCase):
    def test_one_transition_installs_and_marks_every_target(self) -> None:
        relative = self.draft([OLD_1, OLD_2])
        old_body = (self.vault.root / "lessons/old-1.md").read_text()
        preview = self.approve(relative)
        self.assertEqual("preview", preview.state, preview.findings)
        self.assertIn("marks lessons/old-1.md superseded", preview.notes)
        self.assertEqual("accepted", self.status_of("lessons/old-1.md"))
        result = self.approve(relative, apply=True)
        self.assertEqual("applied", result.state, (result.findings, result.notes))
        self.assertEqual("superseded", self.status_of("lessons/old-1.md"))
        self.assertEqual("superseded", self.status_of("lessons/old-2.md"))
        self.assertEqual(
            old_body.replace("status: accepted", "status: superseded"),
            (self.vault.root / "lessons/old-1.md").read_text(),
        )
        self.assertEqual([], memory.validate(self.vault.root).findings)
        self.assertFalse((self.vault.root / relative).exists())
        # No record is deleted by supersession.
        self.assertTrue((self.vault.root / "lessons/old-2.md").exists())

    def test_an_already_superseded_target_is_left_as_is(self) -> None:
        self.vault.write(
            "lessons/old-2.md",
            note(v2("lesson", OLD_2, "global", title="Old two", status="superseded")),
        )
        before = (self.vault.root / "lessons/old-2.md").read_bytes()
        result = self.approve(self.draft([OLD_1, OLD_2]), apply=True)
        self.assertEqual("applied", result.state, (result.findings, result.notes))
        self.assertEqual(before, (self.vault.root / "lessons/old-2.md").read_bytes())

    def test_a_concurrent_target_edit_is_a_conflict_that_loses_nothing(self) -> None:
        relative = self.draft([OLD_1])
        before = _tree_digest(self.vault.root)

        def human() -> None:
            with open(self.vault.root / "lessons/old-1.md", "a", encoding="utf-8") as stream:
                stream.write("A person's late edit.\n")

        result = self.approve(relative, apply=True, before_install=human)
        self.assertEqual("conflict", result.state)
        self.assertIn("A person's late edit.", (self.vault.root / "lessons/old-1.md").read_text())
        self.assertEqual("accepted", self.status_of("lessons/old-1.md"))
        self.assertFalse((self.vault.root / "lessons/2026-09-23-newer-lesson.md").exists())
        self.assertTrue((self.vault.root / relative).exists())
        self.assertNotEqual(before, _tree_digest(self.vault.root))  # only the person's edit

    def test_a_failure_after_the_first_target_rolls_everything_back(self) -> None:
        relative = self.draft([OLD_1, OLD_2])
        before = _tree_digest(self.vault.root)
        original = memory_approve._rewrite
        calls = {"n": 0}

        def flaky(root: Path, path: str, digest: str, content: bytes) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            original(root, path, digest, content)

        with (
            patch.object(memory_approve, "_rewrite", side_effect=flaky),
            self.assertRaises(OSError),
        ):
            self.approve(relative, apply=True)
        self.assertEqual(before, _tree_digest(self.vault.root))
        self.assertEqual(
            [], [p for p in (self.vault.root / "lessons").iterdir() if p.name.startswith(".")]
        )
        self.assertEqual([], list((self.config / "memory-locks").iterdir()))

    def test_a_second_target_edited_mid_transition_rolls_back_the_first(self) -> None:
        relative = self.draft([OLD_1, OLD_2])
        original = memory_approve._rewrite
        calls = {"n": 0}

        def edit_second_first(root: Path, path: str, digest: str, content: bytes) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                with open(root / path, "a", encoding="utf-8") as stream:
                    stream.write("Edited mid-approval.\n")
            original(root, path, digest, content)

        with patch.object(memory_approve, "_rewrite", side_effect=edit_second_first):
            result = self.approve(relative, apply=True)
        self.assertEqual("conflict", result.state)
        self.assertEqual("accepted", self.status_of("lessons/old-1.md"))
        self.assertIn("Edited mid-approval.", (self.vault.root / "lessons/old-2.md").read_text())
        self.assertFalse((self.vault.root / "lessons/2026-09-23-newer-lesson.md").exists())
        self.assertTrue((self.vault.root / relative).exists())

    def test_cross_type_or_scope_needs_explicit_review(self) -> None:
        self.vault.write(
            "decisions/d.md",
            note(v2("decision", ID_B, "shared", title="A decision", projects=["alpha"])),
        )
        relative = self.draft([ID_B])
        refused = self.approve(relative, apply=True)
        self.assertEqual("refused", refused.state)
        self.assertEqual(["MEMORY_SUPERSEDES_SCOPE"], [item.rule for item in refused.findings])
        self.assertEqual("accepted", self.status_of("decisions/d.md"))
        allowed = self.approve(relative, apply=True, allow_cross_scope=True)
        self.assertEqual("applied", allowed.state, allowed.findings)
        self.assertEqual("superseded", self.status_of("decisions/d.md"))
        installed = (self.vault.root / "lessons/2026-09-23-newer-lesson.md").read_text()
        self.assertIn("scope: global", installed)  # the old scope is never copied

    def test_invalid_links_and_targets_are_refused(self) -> None:
        self.vault.write(
            "lessons/retired.md", note(v2("lesson", ID_C, "global", title="R", status="retired"))
        )
        cases = {
            "self": ([NEW], "MEMORY_SUPERSEDES"),
            "missing": (["30000000-0000-4000-8000-000000000001"], "MEMORY_SUPERSEDES_TARGET"),
            "retired": ([ID_C], "MEMORY_SUPERSEDES_STATUS"),
            "duplicate": ([OLD_1, OLD_1], "MEMORY_SUPERSEDES"),
            "draft target": (["40000000-0000-4000-8000-000000000001"], "MEMORY_SUPERSEDES_TARGET"),
        }
        self.vault.write(
            "drafts/other.md",
            note(
                v2(
                    "lesson",
                    "40000000-0000-4000-8000-000000000001",
                    "global",
                    status="draft",
                    title="O",
                )
            ),
        )
        before = _tree_digest(self.vault.root)
        for label, (targets, rule) in cases.items():
            with self.subTest(label):
                result = self.approve(self.draft(targets), apply=True)
                self.assertEqual("refused", result.state)
                self.assertIn(rule, [item.rule for item in result.findings])
        (self.vault.root / "drafts/new.md").unlink()
        self.assertEqual(before, _tree_digest(self.vault.root))

    def test_a_target_that_itself_supersedes_cannot_be_superseded(self) -> None:
        # Schema v2 allows supersedes only on accepted records, so a record that
        # already replaced another cannot itself become superseded.
        self.vault.write(
            "lessons/old-2.md",
            note(v2("lesson", OLD_2, "global", title="Old two", status="superseded")),
        )
        self.vault.write(
            "lessons/old-1.md",
            note(v2("lesson", OLD_1, "global", title="Old one", supersedes=[OLD_2])),
        )
        self.assertEqual([], memory.validate(self.vault.root).findings)
        result = self.approve(self.draft([OLD_1]), apply=True)
        self.assertEqual("refused", result.state)
        self.assertIn("MEMORY_SUPERSEDES", [item.rule for item in result.findings])
        self.assertEqual("accepted", self.status_of("lessons/old-1.md"))

    def test_an_incomplete_hand_made_transition_is_a_validation_error(self) -> None:
        self.vault.write(
            "lessons/new.md",
            note(v2("lesson", NEW, "global", title="Hand made", supersedes=[OLD_1])),
        )
        found = rules(memory.validate(self.vault.root))
        self.assertIn("MEMORY_SUPERSEDES_STATUS", found["lessons/new.md"])

    def test_the_lock_serializes_target_rewrites(self) -> None:
        relative = self.draft([OLD_1])
        lock = memory_approve.lock_path(self.config, self.vault.root)
        lock.parent.mkdir(parents=True)
        lock.write_text(f'{{"pid": {os.getpid()}, "acquired": "now"}}')
        result = self.approve(relative, apply=True, lock_wait=0.2)
        self.assertEqual("conflict", result.state)
        self.assertEqual("accepted", self.status_of("lessons/old-1.md"))


if __name__ == "__main__":
    unittest.main()
