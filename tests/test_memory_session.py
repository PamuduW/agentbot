"""Session reminders are delivery attempts, never fabricated saved facts."""

import json
import stat
from unittest.mock import patch

from src import memory, memory_project_writes, memory_session
from tests.test_memory_project_writes import WritesTestCase


class MemorySessionTests(WritesTestCase):
    def recall(self, **kwargs):
        return memory_session.recall(
            self.a, self.code, self.config, client="codex", session_id="session-1", **kwargs
        )

    def checkpoint(self, event_id="event-1", **kwargs):
        return memory_session.checkpoint(
            self.a,
            self.code,
            self.config,
            client="codex",
            session_id="session-1",
            event_id=event_id,
            **kwargs,
        )

    def test_recall_prepares_bounded_data_without_claiming_model_delivery(self):
        memory_project_writes.add(
            self.a,
            self.code,
            self.config,
            kind="note",
            title="Fixture workaround",
            body="Use synthetic retry mode.",
        )
        head = self.head(self.a)
        data = self.recall()
        self.assertEqual("prepared", data["state"])
        self.assertFalse(data["model_use_verified"])
        self.assertIn("synthetic retry mode", json.dumps(data["memory"]))
        self.assertEqual(head, self.head(self.a))
        private = self.config / "memory-sessions/receipts.json"
        self.assertNotIn("synthetic retry mode", private.read_text())
        self.assertNotIn("session-1", private.read_text())
        self.assertEqual(0o600, stat.S_IMODE(private.stat().st_mode))

    def test_private_state_failure_does_not_invalidate_read(self):
        with patch.object(memory_session, "_record", side_effect=PermissionError("private")):
            data = self.recall()
        self.assertEqual("prepared", data["state"])
        self.assertEqual("unrecorded", data["receipt"]["state"])
        self.assertNotIn("private", json.dumps(data))

    def test_checkpoint_deduplicates_reminder_and_never_saves_a_fact(self):
        head = self.head(self.a)
        first, second = self.checkpoint(), self.checkpoint()
        self.assertTrue(first["reminder"])
        self.assertFalse(first["saved"])
        self.assertTrue(second["duplicate"])
        self.assertEqual("", second["reminder"])
        self.assertEqual(head, self.head(self.a))
        self.assertTrue(self.checkpoint("event-2")["reminder"])

    def test_compaction_recall_is_not_suppressed_by_previous_recall(self):
        first, second = self.recall(), self.recall()
        self.assertEqual(first["memory"], second["memory"])
        self.assertTrue(second["receipt"]["duplicate"])

    def test_invalid_client_or_identifier_is_refused(self):
        for client, session in (("other", "ok"), ("codex", "../bad"), ("codex", "")):
            with self.subTest(client=client), self.assertRaises(memory.MemoryVaultError):
                memory_session.recall(
                    self.a, self.code, self.config, client=client, session_id=session
                )

    def test_malformed_receipts_are_not_rewritten_as_valid_state(self):
        self.recall()
        private = self.config / "memory-sessions/receipts.json"
        invalid = '{"untrusted": "PRIVATE FIXTURE"}'
        private.write_text(invalid)
        data = self.recall()
        self.assertEqual("unrecorded", data["receipt"]["state"])
        self.assertEqual(invalid, private.read_text())
        self.assertNotIn("PRIVATE FIXTURE", json.dumps(data))

    def test_receipt_retention_is_bounded(self):
        for index in range(memory_session.MAX_RECEIPTS + 1):
            memory_session._record(self.config, f"{index:064x}", "codex", "checkpoint")
        items = json.loads((self.config / "memory-sessions/receipts.json").read_text())
        self.assertEqual(memory_session.MAX_RECEIPTS, len(items))
        self.assertNotIn("0" * 64, items)

    def test_symlink_receipt_store_does_not_write_elsewhere(self):
        other = self.tmp / "other"
        other.mkdir()
        self.config.mkdir(exist_ok=True)
        (self.config / "memory-sessions").symlink_to(other, target_is_directory=True)
        data = self.recall()
        self.assertEqual("unrecorded", data["receipt"]["state"])
        self.assertEqual([], list(other.iterdir()))
