"""Ticket M9: deterministic maintenance, retire, promote, and the maintain report."""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src import memory
from src import memory_project_writes as writes
from src import memory_proposals as proposals
from src import memory_sync as sync
from src.memory import MemoryVaultError
from tests.test_memory_project_writes import LESSON, WritesTestCase
from tests.test_memory_sync import git


class PressureTests(WritesTestCase):
    def test_an_identical_body_is_refused(self) -> None:
        writes.add(
            self.a, self.code, self.config, kind="lesson", title="First", body="Exactly this text."
        )
        head = git(self.a, "rev-parse", "HEAD")
        with self.assertRaises(MemoryVaultError) as raised:
            writes.add(
                self.a,
                self.code,
                self.config,
                kind="decision",
                title="Second",
                body="Exactly this text.",
            )
        self.assertIn("already recorded", str(raised.exception))
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))

    def test_a_similar_title_is_a_warning_not_a_refusal(self) -> None:
        writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Cache the docker build",
            body="one",
        )
        result = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Cache the docker build",
            body="two",
        )
        self.assertTrue(any("similar title" in warning for warning in result["warnings"]))

    def test_the_active_context_is_kept_short(self) -> None:
        with self.assertRaises(MemoryVaultError):
            writes.set_context(self.a, self.code, self.config, "word " * 1100)
        result = writes.set_context(self.a, self.code, self.config, "word " * 700)
        self.assertTrue(any("compact" in warning for warning in result["warnings"]))
        self.assertEqual(
            [], writes.set_context(self.a, self.code, self.config, "short")["warnings"]
        )

    def test_the_hot_pool_warns_then_stops_new_records(self) -> None:
        with patch.object(writes, "POOL_SOFT", 5), patch.object(writes, "POOL_HARD", 6):
            # The fixture project holds 4 live records.
            first = writes.add(
                self.a, self.code, self.config, kind="lesson", title="Fifth", body="5"
            )
            self.assertEqual([], first["warnings"])
            second = writes.add(
                self.a, self.code, self.config, kind="lesson", title="Sixth", body="6"
            )
            self.assertTrue(any("maintenance is due" in w for w in second["warnings"]))
            with self.assertRaises(MemoryVaultError) as raised:
                writes.add(self.a, self.code, self.config, kind="lesson", title="Seventh", body="7")
            self.assertIn("maintain", str(raised.exception))
            # Superseding does not grow the pool, so it stays possible.
            writes.add(
                self.a,
                self.code,
                self.config,
                kind="lesson",
                title="Replacement",
                body="r",
                supersedes=first["id"],
            )


class RetirePromoteTests(WritesTestCase):
    def test_retire_keeps_the_record_but_takes_it_out_of_the_pool(self) -> None:
        writes.retire(self.a, self.code, self.config, "lessons/2026-09-27-l.md")
        record = next(r for r in memory.validate(self.a).records if r.path == LESSON)
        self.assertEqual("retired", record.status)
        self.assertTrue((self.a / LESSON).exists())
        self.assertEqual([], memory.validate(self.a).findings)
        with self.assertRaises(MemoryVaultError):
            writes.retire(self.a, self.code, self.config, "project.md")

    def test_promote_proposes_for_core_and_leaves_the_source(self) -> None:
        before = (self.a / LESSON).read_bytes()
        result = writes.promote(self.a, self.code, self.config, "lessons/2026-09-27-l.md")
        self.assertTrue(result["path"].startswith("proposals/core/lessons/"))
        proposal = next(r for r in memory.validate(self.a).records if r.path == result["path"])
        self.assertEqual(
            ("shared", ("alpha",), "draft"), (proposal.scope, proposal.projects, proposal.status)
        )
        self.assertIn(f"Promoted from {LESSON}", (self.a / result["path"]).read_text())
        self.assertEqual(before, (self.a / LESSON).read_bytes())
        self.assertIn(result["path"], [item["path"] for item in proposals.queue(self.a)])
        with self.assertRaises(MemoryVaultError):
            writes.promote(self.a, self.code, self.config, "active-context.md")

    def test_promote_as_global_drops_the_project(self) -> None:
        result = writes.promote(
            self.a, self.code, self.config, "lessons/2026-09-27-l.md", scope="global"
        )
        proposal = next(r for r in memory.validate(self.a).records if r.path == result["path"])
        self.assertEqual(("global", ()), (proposal.scope, proposal.projects))


class MaintainReportTests(WritesTestCase):
    def test_the_report_names_what_needs_attention_without_writing(self) -> None:
        writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Cache the docker build",
            body="one",
        )
        writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Cache the docker build",
            body="two",
        )
        expiring = memory.utc_today() - dt.timedelta(days=1)
        text = (
            (self.a / LESSON)
            .read_text()
            .replace("tags: []", f"tags: []\nvalid_until: {expiring.isoformat()}")
        )
        text = text.replace(
            "date: 2026-09-27", f"date: {(expiring - dt.timedelta(days=1)).isoformat()}"
        )
        sync.change_project(self.a, "alpha", [(LESSON, text.encode())])
        head = git(self.a, "rev-parse", "HEAD")
        report = writes.maintain(self.a, self.code, self.config)
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))
        self.assertEqual("alpha", report["project"])
        self.assertIn(LESSON, report["expired"])
        # The fixture's own project.md and notes/n.md share a title too; both are reported.
        docker = [
            pair
            for pair in report["similar_titles"]
            if all("cache-the-docker-build" in p for p in pair)
        ]
        self.assertEqual(1, len(docker))
        self.assertTrue(any("merge similar" in tip for tip in report["suggestions"]))
        self.assertEqual({"warn": writes.POOL_SOFT, "stop": writes.POOL_HARD}, report["limits"])
