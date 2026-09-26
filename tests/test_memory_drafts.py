"""Proposing and reviewing drafts against sanitized vault fixtures."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import memory
from src import memory_drafts as drafts
from tests.support import run_cli_main
from tests.test_memory import (
    FAKE_GITHUB_TOKEN,
    FAKE_PASSWORD_LINE,
    ID_C,
    SENTINEL,
    _tree_digest,
    note,
    v1_vault,
    v2,
    v2_vault,
)

TODAY = date(2026, 9, 26)
BODY = f"## What happened\n\nEvidence {SENTINEL}.\n"


def lesson(title: str = "Verify the build", **overrides: object) -> drafts.Proposal:
    fields: dict[str, object] = {"kind": "lesson", "title": title, "scope": "global"}
    fields.update(overrides)
    return drafts.Proposal(**fields)  # type: ignore[arg-type]


class DraftTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.vault = v2_vault(self.tmp / "v")
        self.vault.commit()


class ProposeTests(DraftTestCase):
    def test_a_proposal_is_one_ignored_valid_v2_draft(self) -> None:
        result = drafts.propose(self.vault.root, lesson(), BODY, today=TODAY)
        self.assertTrue(result.created, result.findings)
        assert result.path is not None and result.id is not None
        self.assertRegex(result.path, r"^drafts/2026-09-26-verify-the-build-[0-9a-f]{8}\.md$")
        self.assertRegex(result.id, memory.UUID4)
        self.assertEqual("lessons/2026-09-26-verify-the-build.md", result.destination)
        report = memory.validate(self.vault.root)
        self.assertEqual([], report.findings)
        draft = [record for record in report.records if record.draft]
        self.assertEqual(
            [(result.path, result.id, "draft")], [(d.path, d.id, d.status) for d in draft]
        )
        # Ignored: Git sees nothing, so a draft is never tracked or retrieved.
        porcelain = subprocess.run(
            ["git", "-C", str(self.vault.root), "status", "--porcelain"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(b"", porcelain.stdout)
        self.assertEqual(0o600, os.stat(self.vault.root / result.path).st_mode & 0o777)
        self.assertTrue((self.vault.root / result.path).read_text().endswith(BODY))

    def test_duplicate_titles_get_distinct_ids_and_files(self) -> None:
        first = drafts.propose(self.vault.root, lesson(), BODY, today=TODAY)
        second = drafts.propose(self.vault.root, lesson(), BODY, today=TODAY)
        self.assertTrue(first.created and second.created)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual([], memory.validate(self.vault.root).findings)

    def test_a_hostile_title_stays_a_string(self) -> None:
        title = 'Colon: "quotes" [brackets] #hash & *star'
        result = drafts.propose(self.vault.root, lesson(title), BODY, today=TODAY)
        self.assertTrue(result.created, result.findings)
        records = [r for r in memory.validate(self.vault.root).records if r.draft]
        self.assertEqual(title, records[0].title)

    def test_schema_failures_write_nothing(self) -> None:
        before = _tree_digest(self.vault.root)
        cases = {
            "project needs one project": lesson(kind="project", scope="project"),
            "global lists no projects": lesson(projects=("alpha",)),
            "bad tag": lesson(tags=("Not A Slug",)),
            "untrimmed title": lesson(" padded"),
            "no slug": lesson("!!!"),
        }
        for label, proposal in cases.items():
            with self.subTest(label):
                result = drafts.propose(self.vault.root, proposal, BODY, today=TODAY)
                self.assertFalse(result.created)
                self.assertTrue(result.findings)
        self.assertEqual(before, _tree_digest(self.vault.root))

    def test_a_project_draft_derives_its_project_destination(self) -> None:
        result = drafts.propose(
            self.vault.root,
            lesson(kind="project", scope="project", projects=("alpha",)),
            BODY,
            today=TODAY,
        )
        self.assertTrue(result.created, result.findings)
        self.assertEqual("projects/alpha/2026-09-26-verify-the-build.md", result.destination)

    def test_secrets_fail_closed(self) -> None:
        before = _tree_digest(self.vault.root)
        blocked = drafts.propose(
            self.vault.root, lesson(), f"key {FAKE_GITHUB_TOKEN}\n", today=TODAY
        )
        self.assertFalse(blocked.created)
        self.assertEqual(["SECRET_GITHUB_CLASSIC_TOKEN"], [item.rule for item in blocked.findings])
        in_title = drafts.propose(
            self.vault.root, lesson(f"Token {FAKE_GITHUB_TOKEN}"), BODY, today=TODAY
        )
        self.assertFalse(in_title.created)
        warned = drafts.propose(self.vault.root, lesson(), FAKE_PASSWORD_LINE + "\n", today=TODAY)
        self.assertFalse(warned.created)
        self.assertEqual(before, _tree_digest(self.vault.root))
        for result in (blocked, in_title, warned):
            dumped = json.dumps(drafts.proposal_json(result))
            self.assertNotIn(FAKE_GITHUB_TOKEN, dumped)
            self.assertNotIn("hunter2", dumped)
        acknowledged = drafts.propose(
            self.vault.root,
            lesson(),
            FAKE_PASSWORD_LINE + "\n",
            acknowledge=["WARN_PASSWORD_ASSIGNMENT", "SECRET_GITHUB_CLASSIC_TOKEN"],
            today=TODAY,
        )
        self.assertTrue(acknowledged.created)

    def test_a_v1_vault_refuses_proposals(self) -> None:
        vault = v1_vault(self.tmp / "old")
        before = _tree_digest(vault.root)
        with self.assertRaises(memory.MemoryVaultError) as raised:
            drafts.propose(vault.root, lesson(), BODY, today=TODAY)
        self.assertIn("schema 2", str(raised.exception))
        self.assertEqual(before, _tree_digest(vault.root))

    def test_a_symlinked_drafts_directory_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        for child in (self.vault.root / "drafts").iterdir():
            child.unlink()
        (self.vault.root / "drafts").rmdir()
        (self.vault.root / "drafts").symlink_to(outside, target_is_directory=True)
        # Git refuses to look beyond the symlink, or the install does; either
        # way nothing is written through it.
        try:
            result = drafts.propose(self.vault.root, lesson(), BODY, today=TODAY)
        except memory.MemoryVaultError:
            pass
        else:
            self.assertFalse(result.created)
        self.assertEqual([], list(outside.iterdir()))

    def test_drafts_must_be_ignored(self) -> None:
        (self.vault.root / ".gitignore").write_text("exports/*\n")
        result = drafts.propose(self.vault.root, lesson(), BODY, today=TODAY)
        self.assertFalse(result.created)
        self.assertEqual(["MEMORY_DRAFT_TRACKED"], [item.rule for item in result.findings])
        self.assertEqual(
            ["README.md"], sorted(p.name for p in (self.vault.root / "drafts").iterdir())
        )

    def test_read_body_is_bounded_and_refuses_symlinks(self) -> None:
        real = self.tmp / "body.md"
        real.write_text(BODY)
        self.assertEqual(BODY, drafts.read_body(real))
        link = self.tmp / "link.md"
        link.symlink_to(real)
        big = self.tmp / "big.md"
        big.write_text("x" * (memory.MAX_FILE_BYTES + 1))
        binary = self.tmp / "bin.md"
        binary.write_bytes(b"\xff\x00")
        for path in (link, big, binary, self.tmp):
            with self.subTest(path=path.name), self.assertRaises(memory.MemoryVaultError):
                drafts.read_body(path)
        self.assertEqual(BODY, drafts.read_body(None, io.BytesIO(BODY.encode())))


class ReviewTests(DraftTestCase):
    def test_review_lists_newest_first_without_bodies_and_changes_nothing(self) -> None:
        old = drafts.propose(self.vault.root, lesson("Older"), BODY, today=date(2026, 9, 1))
        new = drafts.propose(self.vault.root, lesson("Newer"), BODY, today=TODAY)
        self.vault.write("drafts/broken.md", f"no front matter {SENTINEL}\n")
        before = _tree_digest(self.vault.root)
        summaries, total = drafts.list_drafts(self.vault.root)
        self.assertEqual(3, total)
        self.assertEqual(
            [new.path, old.path, "drafts/broken.md"], [item.path for item in summaries]
        )
        self.assertEqual((0, 0), (summaries[0].blocking, summaries[0].warnings))
        self.assertEqual(1, summaries[2].blocking)
        self.assertNotIn(SENTINEL, json.dumps(drafts.draft_list_json(summaries, total)))
        self.assertEqual(before, _tree_digest(self.vault.root))

    def test_review_caps_the_list(self) -> None:
        for index in range(drafts.REVIEW_LIST_MAX + 2):
            self.vault.write(
                f"drafts/d{index:03}.md", note(v2("lesson", _uuid(index), "global", status="draft"))
            )
        summaries, total = drafts.list_drafts(self.vault.root)
        self.assertEqual(
            (drafts.REVIEW_LIST_MAX, drafts.REVIEW_LIST_MAX + 2), (len(summaries), total)
        )

    def test_one_draft_review(self) -> None:
        created = drafts.propose(self.vault.root, lesson("Verify the build"), BODY, today=TODAY)
        drafts.propose(self.vault.root, lesson("Verify the build"), BODY, today=TODAY)
        assert created.path is not None
        before = _tree_digest(self.vault.root)
        review = drafts.review_draft(self.vault.root, created.path)
        self.assertEqual(before, _tree_digest(self.vault.root))
        self.assertEqual(created.destination, review.destination)
        self.assertFalse(review.destination_exists)
        self.assertEqual(1, len(review.duplicate_titles))
        self.assertEqual([], review.findings)
        self.assertEqual("Verify the build", review.fields["title"])
        self.assertNotIn(SENTINEL, json.dumps(drafts.draft_review_json(review)))

    def test_review_reports_an_existing_destination_and_empty_body(self) -> None:
        created = drafts.propose(self.vault.root, lesson("Verify the build"), "", today=TODAY)
        assert created.path is not None and created.destination is not None
        self.vault.write(created.destination, "occupied\n")
        review = drafts.review_draft(self.vault.root, created.path)
        self.assertTrue(review.destination_exists)
        self.assertIn("MEMORY_EVIDENCE_EMPTY", [item.rule for item in review.findings])

    def test_review_redacts_metadata_when_a_secret_is_found(self) -> None:
        self.vault.write(
            "drafts/leak.md",
            note(
                v2(
                    "lesson",
                    "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8",
                    "global",
                    status="draft",
                    title=f"Token {FAKE_GITHUB_TOKEN}",
                )
            ),
        )
        review = drafts.review_draft(self.vault.root, "drafts/leak.md")
        dumped = json.dumps(drafts.draft_review_json(review))
        self.assertNotIn(FAKE_GITHUB_TOKEN, dumped)
        summaries, _ = drafts.list_drafts(self.vault.root)
        self.assertNotIn(FAKE_GITHUB_TOKEN, json.dumps(drafts.draft_list_json(summaries, 1)))

    def test_review_takes_only_a_draft_path(self) -> None:
        for bad in (
            "lessons/2026-09-23-new.md",
            "drafts/README.md",
            "drafts/../preferences.md",
            "drafts/none.md",
        ):
            with self.subTest(path=bad), self.assertRaises(memory.MemoryVaultError):
                drafts.review_draft(self.vault.root, bad)

    def test_a_draft_may_supersede_and_review_shows_the_edge(self) -> None:
        self.vault.write(
            "drafts/replace.md",
            note(
                v2(
                    "lesson",
                    "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8",
                    "global",
                    status="draft",
                    supersedes=[ID_C],
                )
            ),
        )
        review = drafts.review_draft(self.vault.root, "drafts/replace.md")
        self.assertEqual([ID_C], review.fields["supersedes"])


def _uuid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-000000000000"


class DraftCliTests(DraftTestCase):
    def _cli(self, *argv: str, stdin: bytes = b"") -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"), NO_COLOR="1", AGENTBOT_MEMORY_DIR=str(self.vault.root)
        )
        stream = io.TextIOWrapper(io.BytesIO(stdin))
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin", stream):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_propose_from_stdin_then_review(self) -> None:
        rc, stdout = self._cli(
            "memory",
            "propose",
            "--type",
            "lesson",
            "--title",
            "From stdin",
            "--scope",
            "global",
            "--stdin",
            "--json",
            stdin=BODY.encode(),
        )
        self.assertEqual(0, rc, stdout)
        created = json.loads(stdout)
        self.assertEqual("created", created["state"])
        rc, stdout = self._cli("memory", "review")
        self.assertEqual(0, rc)
        self.assertIn(created["path"], stdout)
        self.assertNotIn(SENTINEL, stdout)
        rc, stdout = self._cli("memory", "review", created["path"])
        self.assertEqual(0, rc)
        self.assertIn("lessons/", stdout)
        self.assertNotIn(SENTINEL, stdout)

    def test_propose_from_a_caller_relative_file(self) -> None:
        (self.tmp / "body.md").write_text(BODY)
        with patch.dict(os.environ, {"AGENTBOT_CALLER_PWD": str(self.tmp)}):
            rc, stdout = self._cli(
                "memory",
                "propose",
                "--type",
                "decision",
                "--title",
                "A decision",
                "--scope",
                "shared",
                "--project",
                "alpha",
                "--from-file",
                "body.md",
            )
        self.assertEqual(0, rc, stdout)

    def test_refusals_exit_one(self) -> None:
        rc, stdout = self._cli(
            "memory",
            "propose",
            "--type",
            "lesson",
            "--title",
            "Leak",
            "--scope",
            "global",
            "--stdin",
            stdin=f"{FAKE_GITHUB_TOKEN}\n".encode(),
        )
        self.assertEqual(1, rc)
        self.assertIn("SECRET_GITHUB_CLASSIC_TOKEN", stdout)
        self.assertNotIn(FAKE_GITHUB_TOKEN, stdout)
        rc, _ = self._cli("memory", "review", "lessons/2026-09-23-new.md")
        self.assertEqual(1, rc)

    def test_a_source_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            self._cli("memory", "propose", "--type", "lesson", "--title", "T", "--scope", "global")


if __name__ == "__main__":
    unittest.main()
