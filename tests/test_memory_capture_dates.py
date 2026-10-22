"""Capture dates must reach validated records without weakening approval."""

from datetime import date, datetime, timezone
from unittest.mock import patch

from src import memory
from src import memory_project_writes as writes
from src import memory_proposals as proposals
from src import memory_retrieve as retrieve
from tests.test_memory_project_writes import WritesTestCase
from tests.test_memory_proposals import ProposalCase
from tests.test_memory_sync import git


class ProjectCaptureDatesTests(WritesTestCase):
    def test_temporary_tool_workaround_can_be_replaced_by_an_undated_fact(self) -> None:
        first = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Temporary retry remedy",
            body="Use one retry until the patched transport arrives.",
            valid_until="2099-01-01",
        )
        writes.retire(self.a, self.code, self.config, first["path"])
        second = writes.add(
            self.a,
            self.code,
            self.config,
            kind="lesson",
            title="Permanent retry remedy",
            body=f"The verified patched transport avoids retries. Replaces [[{first['path'][:-3]}]].",
        )
        hits = retrieve.search(
            self.a, "retry remedy", retrieve.Request(project="alpha"), today=date(2099, 1, 2)
        ).hits
        self.assertNotIn(first["id"], [hit.record.id for hit in hits])
        replacement = next(hit.record for hit in hits if hit.record.id == second["id"])
        self.assertIsNone(replacement.valid_until)
        self.valid(self.a)

    def test_midnight_rollover_uses_one_creation_date_for_a_correction(self) -> None:
        with patch.object(writes, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 1, tzinfo=timezone.utc)
            first = writes.add(
                self.a,
                self.code,
                self.config,
                kind="note",
                title="Midnight source",
                body="Initial midnight fixture.",
                review_after="2026-10-02",
                valid_until="2099-01-01",
            )
        with patch.object(writes, "datetime") as clock:
            clock.now.side_effect = [
                datetime(2026, 10, 22, 23, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 10, 23, 0, 0, 0, tzinfo=timezone.utc),
            ]
            second = writes.add(
                self.a,
                self.code,
                self.config,
                kind="note",
                title="Midnight correction",
                body="Corrected midnight fixture.",
                supersedes=first["id"],
            )
        record = next(r for r in memory.validate(self.a).records if r.id == second["id"])
        self.assertEqual(("2026-10-22", "2026-10-22"), (record.date, record.review_after))

    def test_due_review_remains_due_after_a_later_correction(self) -> None:
        with patch.object(writes, "datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            first = writes.add(
                self.a,
                self.code,
                self.config,
                kind="note",
                title="Due fixture",
                body="Initial due fixture.",
                review_after="2026-01-02",
                valid_until="2099-01-01",
            )
        with patch.object(writes, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            second = writes.add(
                self.a,
                self.code,
                self.config,
                kind="note",
                title="Corrected due fixture",
                body="Corrected due fixture.",
                supersedes=first["id"],
            )
        record = next(r for r in memory.validate(self.a).records if r.id == second["id"])
        self.assertTrue(record.due(date(2026, 10, 22)))
        self.assertEqual("2099-01-01", record.valid_until)

    def test_writer_persists_dates_and_recall_obeys_expiry_boundary(self) -> None:
        result = writes.add(
            self.a,
            self.code,
            self.config,
            kind="note",
            title="Temporary endpoint",
            body="Fixture endpoint port 9443.",
            review_after="2098-12-01",
            valid_until="2099-01-01",
        )
        record = next(r for r in memory.validate(self.a).records if r.id == result["id"])
        self.assertEqual(("2098-12-01", "2099-01-01"), (record.review_after, record.valid_until))
        for day, visible in ((date(2099, 1, 1), True), (date(2099, 1, 2), False)):
            with self.subTest(day=day):
                hits = retrieve.search(
                    self.a, "endpoint", retrieve.Request(project="alpha"), today=day
                ).hits
                self.assertEqual(visible, any(hit.record.id == result["id"] for hit in hits))

    def test_bad_or_reversed_dates_write_nothing(self) -> None:
        before = git(self.a, "rev-parse", "HEAD")
        for dates in (
            {"valid_until": "2099-13-01"},
            {"valid_until": "2020-01-01"},
            {"review_after": "2099-01-02", "valid_until": "2099-01-01"},
            {"valid_until": "2099-01-01\nstatus: accepted"},
        ):
            with self.subTest(dates=dates), self.assertRaises(memory.MemoryVaultError):
                writes.add(
                    self.a,
                    self.code,
                    self.config,
                    kind="note",
                    title="Bad expiry",
                    body="x",
                    **dates,
                )
        self.assertEqual(before, git(self.a, "rev-parse", "HEAD"))

    def test_supersession_preserves_existing_expiry_without_explicit_replacement_date(self) -> None:
        first = writes.add(
            self.a,
            self.code,
            self.config,
            kind="note",
            title="Temporary setting",
            body="Initial fixture value.",
            valid_until="2099-01-01",
        )
        second = writes.add(
            self.a,
            self.code,
            self.config,
            kind="note",
            title="Corrected setting",
            body="Corrected fixture value.",
            supersedes=first["id"],
        )
        records = {r.id: r for r in memory.validate(self.a).records}
        self.assertEqual("superseded", records[first["id"]].status)
        self.assertEqual("2099-01-01", records[second["id"]].valid_until)


class ProposalCaptureDatesTests(ProposalCase):
    def test_dated_preference_remains_an_unretrievable_proposal(self) -> None:
        result = proposals.propose(
            self.a,
            kind="preference",
            title="Temporary preference",
            body="Synthetic only.",
            review_after="2098-12-01",
            valid_until="2099-01-01",
        )
        record = next(r for r in memory.validate(self.a).records if r.id == result["id"])
        self.assertEqual(
            ("draft", "2098-12-01", "2099-01-01"),
            (record.status, record.review_after, record.valid_until),
        )
        self.assertFalse(
            any(
                h.record.id == result["id"]
                for h in retrieve.search(
                    self.a, "preference", retrieve.Request(project="alpha", history=True)
                ).hits
            )
        )

    def test_invalid_proposal_expiry_writes_nothing(self) -> None:
        before = git(self.a, "rev-parse", "HEAD")
        with self.assertRaises(memory.MemoryVaultError):
            proposals.propose(
                self.a,
                kind="lesson",
                title="Invalid dates",
                body="Synthetic only.",
                valid_until="not-a-date",
            )
        self.assertEqual(before, git(self.a, "rev-parse", "HEAD"))
