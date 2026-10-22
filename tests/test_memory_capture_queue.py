"""A private pending capture must survive denial and restart without claiming a save."""

import errno
import json
import stat
from datetime import datetime, timezone
from unittest.mock import patch

from src import memory, memory_capture, memory_capture_queue, memory_project_writes, memory_projects
from tests.test_memory import FAKE_GITHUB_TOKEN
from tests.test_memory_project_writes import WritesTestCase
from tests.test_memory_projects import repo

EVENT = "5d82633f-bcc5-45b6-a0e6-086cc429aa71"


class CaptureQueueTests(WritesTestCase):
    def test_raw_secret_boundaries_are_checked_before_queue_serialization(self):
        for target, kind in (("project", "note"), ("proposal", "preference")):
            for prefix in ("Fixture:\n", "Fixture:\t"):
                with (
                    self.subTest(target=target, prefix=repr(prefix)),
                    self.assertRaises(memory.MemoryVaultError),
                ):
                    self.enqueue(target=target, kind=kind, body=prefix + FAKE_GITHUB_TOKEN)
        self.assertFalse((self.config / "memory-capture" / "queue.json").exists())

    def test_duplicate_from_another_checkout_cannot_claim_the_first_checkout_queue(self):
        self.enqueue()
        other = repo(self.tmp / "other-alpha", "git@github.com:me/alpha.git")
        request = {
            "event_id": EVENT,
            "client": "antigravity",
            "target": "project",
            "kind": "note",
            "title": "Queue fixture",
            "body": "Fixture queue endpoint.",
        }
        with self.assertRaises(memory.MemoryVaultError):
            memory_capture_queue.enqueue(self.a, other, self.config, request)
        self.assertEqual(1, memory_capture_queue.status(self.a, self.code, self.config)["pending"])

    def test_next_day_retry_finds_the_committed_receipt_before_date_validation(self):
        original = memory_capture_queue._write

        def fail_dequeue(path, entries):
            if not entries:
                raise OSError("synthetic lost dequeue")
            return original(path, entries)

        with (
            patch.object(memory_capture_queue, "datetime") as queue_clock,
            patch.object(memory_project_writes, "datetime") as write_clock,
        ):
            queue_clock.now.return_value = write_clock.now.return_value = datetime(
                2026, 10, 22, tzinfo=timezone.utc
            )
            self.enqueue(review_after="2026-10-22")
            with patch.object(memory_capture_queue, "_write", side_effect=fail_dequeue):
                with self.assertRaises(OSError):
                    self.drain()
            head = self.head(self.a)
            queue_clock.now.return_value = write_clock.now.return_value = datetime(
                2026, 10, 23, tzinfo=timezone.utc
            )
            self.assertEqual("queued", self.enqueue(review_after="2026-10-22")["state"])
            self.assertEqual((1, head), (self.drain()["committed"], self.head(self.a)))

    def test_discard_is_preview_first_and_scoped_to_the_current_checkout(self):
        queued = self.enqueue()
        preview = memory_capture_queue.discard(self.a, self.code, self.config, queued["queue_id"])
        self.assertEqual("preview", preview["state"])
        self.assertEqual(1, memory_capture_queue.status(self.a, self.code, self.config)["pending"])
        beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")
        memory_projects.register(self.a, beta, self.config)
        with self.assertRaises(memory.MemoryVaultError):
            memory_capture_queue.discard(
                self.a, beta, self.config, queued["queue_id"], confirm=True
            )
        result = memory_capture_queue.discard(
            self.a, self.code, self.config, queued["queue_id"], confirm=True
        )
        self.assertEqual("discarded", result["state"])
        self.assertEqual(0, memory_capture_queue.status(self.a, self.code, self.config)["pending"])

    def test_queued_preference_remains_a_draft_with_validated_expiry(self):
        self.enqueue(target="proposal", kind="preference", valid_until="2099-01-01")
        self.assertEqual(1, self.drain()["committed"])
        records = memory.validate(self.a).records
        record = next(r for r in records if r.title == "Queue fixture")
        self.assertTrue(record.draft)
        self.assertEqual("2099-01-01", record.valid_until)

    def test_queue_lock_symlink_is_refused_without_touching_the_target(self):
        directory = self.config / "memory-capture"
        directory.mkdir(parents=True, mode=0o700)
        target = self.tmp / "unrelated.txt"
        target.write_text("untouched")
        (directory / "lock").symlink_to(target)
        with self.assertRaises((OSError, memory.MemoryVaultError)):
            self.enqueue()
        self.assertEqual("untouched", target.read_text())

    def test_nested_directory_submission_can_be_drained_from_the_repository_root(self):
        nested = self.code / "nested"
        nested.mkdir()
        request = {
            "event_id": EVENT,
            "client": "codex",
            "target": "project",
            "kind": "note",
            "title": "Nested fixture",
            "body": "Nested queue fixture.",
        }
        memory_capture_queue.enqueue(self.a, nested, self.config, request)
        self.assertEqual(1, self.drain()["committed"])

    def test_malformed_queue_is_not_reported_as_empty(self):
        self.enqueue()
        (self.config / "memory-capture" / "queue.json").write_text("[{}]")
        with self.assertRaises(memory.MemoryVaultError):
            memory_capture_queue.status(self.a, self.code, self.config)

    def enqueue(self, **changes):
        request = {
            "event_id": EVENT,
            "client": "antigravity",
            "target": "project",
            "kind": "note",
            "title": "Queue fixture",
            "body": "Fixture queue endpoint.",
        }
        request.update(changes)
        return memory_capture_queue.enqueue(self.a, self.code, self.config, request)

    def drain(self, **changes):
        return memory_capture_queue.drain(self.a, self.code, self.config, **changes)

    def test_enqueue_is_private_durable_and_not_a_saved_record(self):
        head = self.head(self.a)
        result = self.enqueue()
        self.assertEqual("queued", result["state"])
        self.assertEqual(head, self.head(self.a))
        queue = self.config / "memory-capture"
        self.assertEqual(0o700, stat.S_IMODE(queue.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((queue / "queue.json").stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((queue / "lock").stat().st_mode))
        stored = json.loads((queue / "queue.json").read_text())
        self.assertEqual(1, len(stored))
        drained = self.drain()
        self.assertEqual((1, 0), (drained["committed"], drained["pending"]))
        self.valid(self.a)

    def test_duplicate_enqueue_is_idempotent_and_changed_payload_is_refused(self):
        first = self.enqueue()
        self.assertEqual(first["queue_id"], self.enqueue()["queue_id"])
        with self.assertRaisesRegex(memory.MemoryVaultError, "different payload"):
            self.enqueue(body="Different queue fixture.")
        self.assertEqual(1, self.drain()["committed"])

    def test_denied_write_stays_pending_with_content_free_status(self):
        self.enqueue()
        head = self.head(self.a)
        with patch.object(memory_capture, "save", side_effect=OSError(errno.EROFS, "private text")):
            result = self.drain()
        self.assertEqual((0, 1), (result["committed"], result["pending"]))
        self.assertNotIn("private text", json.dumps(result))
        self.assertNotIn("Fixture queue endpoint", json.dumps(result))
        self.assertEqual(head, self.head(self.a))
        self.assertEqual(1, self.drain()["committed"])

    def test_crash_after_commit_before_dequeue_retries_the_original_event(self):
        self.enqueue()
        original = memory_capture_queue._write

        def fail_dequeue(path, entries):
            if not entries:
                raise OSError("synthetic dequeue failure")
            return original(path, entries)

        with patch.object(memory_capture_queue, "_write", side_effect=fail_dequeue):
            with self.assertRaises(OSError):
                self.drain()
        head = self.head(self.a)
        self.assertEqual(1, self.drain()["committed"])
        self.assertEqual(head, self.head(self.a))

    def test_drain_excludes_other_projects_and_rebound_origins(self):
        self.enqueue()
        beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")
        memory_projects.register(self.a, beta, self.config)
        result = memory_capture_queue.drain(self.a, beta, self.config)
        self.assertEqual((0, 0), (result["attempted"], result["pending"]))
        self.assertEqual(1, self.drain()["committed"])

    def test_invalid_and_secret_requests_never_enter_private_queue(self):
        for change in (
            {"body": FAKE_GITHUB_TOKEN},
            {"valid_until": "2020-01-01"},
            {"target": "core"},
            {"client": "unknown"},
            {"event_id": "bad"},
            {"body": "x" * 20_000},
            {"project": "foreign"},
        ):
            with self.subTest(change=list(change)), self.assertRaises(memory.MemoryVaultError):
                self.enqueue(**change)
        self.assertEqual(0, memory_capture_queue.status(self.a, self.code, self.config)["pending"])

    def test_queue_bounds_and_drain_limit_preserve_remaining_entries(self):
        self.enqueue()
        self.enqueue(event_id="81ff7bb9-b4ca-4195-9179-d68d3c577d20", body="Second queue fixture.")
        with patch.object(memory_capture_queue, "MAX_ENTRIES", 2):
            with self.assertRaisesRegex(memory.MemoryVaultError, "full"):
                self.enqueue(event_id="41ef5d14-afda-4c52-a17e-a611128ea58c", body="Third.")
        result = self.drain(limit=1)
        self.assertEqual((1, 1), (result["attempted"], result["pending"]))
        for limit in (0, 9):
            with self.assertRaises(memory.MemoryVaultError):
                self.drain(limit=limit)

    def test_unsafe_queue_symlink_is_refused_without_writing_through_it(self):
        self.config.mkdir()
        target = self.tmp / "foreign"
        target.mkdir()
        (self.config / "memory-capture").symlink_to(target, target_is_directory=True)
        with self.assertRaises(memory.MemoryVaultError):
            self.enqueue()
        self.assertEqual([], list(target.iterdir()))
