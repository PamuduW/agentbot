"""Obsidian links derived from `projects` and `supersedes` (schema 3)."""

from __future__ import annotations

from pathlib import Path

from src import memory
from src import memory_autosync as autosync
from src import memory_project_writes as writes
from src import memory_proposals as proposals
from src import memory_sync as sync
from src.memory import project_hub_link, record_link
from tests.test_memory_autosync import AutosyncTestCase
from tests.test_memory_project_writes import LESSON, WritesTestCase
from tests.test_memory_v3 import uid

HUB = "[[projects/alpha/project]]"


def fields(root: Path, relative: str) -> dict:
    return memory._front_matter((root / relative).read_text())


class ProjectLinkTests(WritesTestCase):
    def lesson_id(self) -> str:
        return next(r.id for r in memory.validate(self.a).records if r.path == LESSON)

    def test_every_new_project_record_links_its_project_hub(self) -> None:
        added = writes.add(self.a, self.code, self.config, kind="decision", title="D", body="x")
        writes.set_context(self.a, self.code, self.config, "now: testing links")
        self.assertEqual(HUB, fields(self.a, added["path"])["up"])
        self.assertEqual(HUB, fields(self.a, "projects/alpha/active-context.md")["up"])
        self.assertNotIn("up", fields(self.a, "projects/alpha/project.md"))
        self.assertEqual([], memory.validate(self.a).findings)

    def test_a_superseding_record_links_what_it_replaces_and_follows_a_move(self) -> None:
        added = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Newer lesson",
            body="better",
            supersedes=self.lesson_id(),
        )
        self.assertEqual([record_link(LESSON)], fields(self.a, added["path"])["replaces"])
        writes.move(
            self.a, self.code, self.config, "lessons/2026-09-27-l.md", "lessons/renamed-l.md"
        )
        self.assertEqual(
            ["[[projects/alpha/lessons/renamed-l]]"], fields(self.a, added["path"])["replaces"]
        )
        self.assertEqual([], memory.validate(self.a).findings)

    def test_a_block_list_written_by_obsidian_is_relinked_too(self) -> None:
        added = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Newer",
            body="n",
            supersedes=self.lesson_id(),
        )
        path = self.a / added["path"]
        link = record_link(LESSON)
        block = f'replaces:\n  - "{link}"'
        rewritten = path.read_text().replace(f'replaces: ["{link}"]', block)
        self.assertIn(block, rewritten)
        sync.change_project(self.a, "alpha", [(added["path"], rewritten.encode())])
        writes.move(self.a, self.code, self.config, "lessons/2026-09-27-l.md", "lessons/moved.md")
        self.assertEqual(
            ["[[projects/alpha/lessons/moved]]"], fields(self.a, added["path"])["replaces"]
        )

    def test_an_older_record_gains_its_link_when_next_written(self) -> None:
        self.assertNotIn("up", fields(self.a, LESSON))
        writes.edit(self.a, self.code, self.config, "lessons/2026-09-27-l.md", body="revised")
        self.assertEqual(HUB, fields(self.a, LESSON)["up"])
        self.assertEqual([], memory.validate(self.a).findings)


class LinkValidationTests(WritesTestCase):
    def rules_after(self, relative: str, extra: str) -> set[str]:
        path = self.a / relative
        text = path.read_text()
        path.write_text(text.replace("\n---\n", f"\n{extra}\n---\n", 1))
        return {f.rule for f in memory.validate(self.a).findings if f.path == relative}

    def test_links_that_do_not_match_the_record_are_refused(self) -> None:
        cases = [
            (LESSON, 'up: "[[projects/other/project]]"'),
            ("projects/alpha/project.md", f"up: {HUB!r}".replace("'", '"')),
            ("core/user/preferences.md", 'up: "[[projects/alpha/project]]"'),
            (LESSON, 'replaces: ["[[projects/alpha/notes/n]]"]'),
            (LESSON, f"supersedes: [{uid(9)}]\nreplaces: [not-a-link]"),
        ]
        for relative, extra in cases:
            with self.subTest(extra=extra):
                self.setUp()
                self.assertIn("MEMORY_LINK", self.rules_after(relative, extra))

    def test_the_helpers_write_vault_paths(self) -> None:
        self.assertEqual(HUB, project_hub_link("alpha"))
        self.assertEqual("[[core/lessons/x]]", record_link("core/lessons/x.md"))


class CoreLinkTests(AutosyncTestCase):
    def test_an_approved_supersession_links_the_record_it_replaces(self) -> None:
        proposals.reject(self.a, "proposals/core/lessons/p.md")
        proposed = proposals.propose(
            self.a, kind="lesson", title="Newer core lesson", body="x", supersedes=[uid(3)]
        )
        result = proposals.approve(self.a, proposed["path"])
        self.assertEqual(
            ["[[core/lessons/2026-09-27-core]]"], fields(self.a, result["path"])["replaces"]
        )
        self.assertEqual([], memory.validate(self.a).findings)
        self.assertEqual("synced", autosync.after_write(self.a, self.config)["state"])
        self.assertEqual([], sync.pending(self.a))
