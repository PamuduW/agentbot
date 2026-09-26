"""Review and validity dates: derived flags, never status changes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import memory
from tests.support import run_cli_main
from tests.test_memory import _tree_digest, note, rules, v1_vault, v2, v2_vault

IDS = [f"{index:08x}-0000-4000-8000-000000000000" for index in range(1, 20)]


class DueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.vault = v2_vault(self.tmp / "v")

    def write(self, name: str, record_id: str, **fields: object) -> None:
        self.vault.write(
            f"decisions/{name}.md", note(v2("decision", record_id, "global", **fields))
        )


class DueFlagTests(DueTestCase):
    def test_boundaries_are_utc_calendar_days(self) -> None:
        record = memory.Record(
            path="x",
            type="lesson",
            status="accepted",
            draft=False,
            review_after="2026-10-01",
            valid_until="2026-10-10",
        )
        self.assertFalse(record.due(date(2026, 9, 30)))
        self.assertTrue(record.due(date(2026, 10, 1)))
        self.assertFalse(record.expired(date(2026, 10, 10)))  # valid through that day
        self.assertTrue(record.expired(date(2026, 10, 11)))
        undated = memory.Record(path="y", type="lesson", status="accepted", draft=False)
        self.assertFalse(undated.due(date(2099, 1, 1)) or undated.expired(date(2099, 1, 1)))

    def test_utc_today_uses_utc(self) -> None:
        from datetime import datetime, timezone

        late = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
        with patch.object(memory, "datetime") as clock:
            clock.now.return_value = late
            self.assertEqual(date(2026, 9, 30), memory.utc_today())
            clock.now.assert_called_once_with(timezone.utc)


class DueQueueTests(DueTestCase):
    def test_queue_lists_due_and_expired_accepted_records_oldest_first(self) -> None:
        self.write("a", IDS[0], review_after="2026-09-30")
        self.write("b", IDS[1], review_after="2026-09-24")
        self.write("c", IDS[2], review_after="2026-10-02")  # not yet due
        self.write("d", IDS[3], valid_until="2026-09-29")  # expired
        self.write("e", IDS[4], valid_until="2026-09-30")  # valid through today
        self.write("f", IDS[5], status="retired", review_after="2026-09-24")
        self.write("g", IDS[6], status="superseded", valid_until="2026-09-24")
        self.vault.write(
            "drafts/h.md",
            note(v2("decision", IDS[7], "global", status="draft", review_after="2026-09-24")),
        )
        before = _tree_digest(self.vault.root)
        items, total, schema = memory.due_queue(self.vault.root, today=date(2026, 9, 30))
        self.assertEqual(before, _tree_digest(self.vault.root))
        # The v2 fixture's own lesson is due from 2026-09-24 and expired after 09-25.
        self.assertEqual((2, 4), (schema, total))
        self.assertEqual(
            ["decisions/b.md", "lessons/2026-09-23-new.md", "decisions/d.md", "decisions/a.md"],
            [item.record.path for item in items],
        )
        by_path = {item.record.path: item for item in items}
        self.assertTrue(by_path["decisions/d.md"].expired)
        self.assertFalse(by_path["decisions/d.md"].due)
        # An overdue record is still accepted, on disk and to the validator.
        self.assertIn("status: accepted", (self.vault.root / "decisions/b.md").read_text())
        self.assertEqual([], memory.validate(self.vault.root).findings)

    def test_queue_is_bounded(self) -> None:
        for index in range(12):
            self.write(f"r{index:02}", IDS[index], review_after="2026-09-24")
        items, total, _ = memory.due_queue(self.vault.root, today=date(2026, 9, 30), limit=5)
        self.assertEqual((5, 13), (len(items), total))  # 12 plus the fixture's lesson
        items, _, _ = memory.due_queue(self.vault.root, today=date(2026, 9, 30), limit=10_000)
        self.assertEqual(13, len(items))
        items, _, _ = memory.due_queue(self.vault.root, today=date(2026, 9, 30), limit=0)
        self.assertEqual(1, len(items))

    def test_malformed_and_impossible_dates_are_validation_errors_not_queue_items(self) -> None:
        self.write("bad", IDS[0], review_after="2026-02-30")
        self.write("order", IDS[1], review_after="2026-12-01", valid_until="2026-11-01")
        self.write("early", IDS[2], valid_until="2026-01-01")
        self.write("text", IDS[3], review_after="soon")
        found = rules(memory.validate(self.vault.root))
        for name in ("bad", "order", "early", "text"):
            self.assertIn("MEMORY_DATE", found[f"decisions/{name}.md"])
        _, total, _ = memory.due_queue(self.vault.root, today=date(2030, 1, 1))
        self.assertEqual(1, total)  # only the fixture's own expired lesson

    def test_a_v1_vault_has_an_empty_queue(self) -> None:
        vault = v1_vault(self.tmp / "old")
        self.assertEqual(([], 0, 1), memory.due_queue(vault.root, today=date(2030, 1, 1)))


class DueCliTests(DueTestCase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"), NO_COLOR="1", AGENTBOT_MEMORY_DIR=str(self.vault.root)
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(memory, "utc_today", return_value=date(2026, 9, 30)),
        ):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_due_and_status(self) -> None:
        self.write("a", IDS[0], review_after="2026-09-24")
        rc, stdout = self._cli("memory", "due", "--json")
        self.assertEqual(0, rc)
        payload = json.loads(stdout)
        self.assertEqual("2026-09-30", payload["today"])
        # The v2 fixture's lesson is valid until 2026-09-25, so it has expired.
        self.assertEqual(
            {("decisions/a.md", True, False), ("lessons/2026-09-23-new.md", True, True)},
            {(r["path"], r["due"], r["expired"]) for r in payload["records"]},
        )
        rc, stdout = self._cli("memory", "due")
        self.assertEqual(0, rc)
        self.assertIn("no record's status", stdout)
        rc, stdout = self._cli("memory", "status", "--json")
        self.assertEqual((2, 1), (json.loads(stdout)["due"], json.loads(stdout)["expired"]))


if __name__ == "__main__":
    unittest.main()
