"""Event retries must not duplicate or falsely acknowledge durable memory."""

import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src import (
    memory,
    memory_autosync,
    memory_capture,
    memory_project_writes,
    memory_projects,
    memory_retrieve,
    memory_sync,
)
from tests.test_memory import FAKE_GITHUB_TOKEN
from tests.test_memory_project_writes import LESSON, WritesTestCase
from tests.test_memory_projects import repo
from tests.test_memory_sync import git

EVENT = "5d82633f-bcc5-45b6-a0e6-086cc429aa71"


class CaptureTests(WritesTestCase):
    def test_raw_titles_cannot_hide_secrets_in_serialized_metadata(self):
        head = self.head(self.a)
        for target, kind in (("project", "note"), ("proposal", "preference")):
            with self.subTest(target=target), self.assertRaises(memory.MemoryVaultError):
                self.capture(
                    event_id=str(uuid.uuid4()),
                    target=target,
                    kind=kind,
                    title="Fixture\n" + FAKE_GITHUB_TOKEN,
                )
        self.assertEqual(head, self.head(self.a))

    def test_kept_conflict_retains_original_receipt_on_another_clone(self):
        source = next(r for r in memory.validate(self.a).records if r.path == LESSON)
        first = self.capture(supersedes=source.id)
        memory_project_writes.edit(
            self.b, self.code, self.config, LESSON, body="Other machine edited the source."
        )
        memory_sync.sync(self.b)
        memory_sync.sync(self.a)
        memory_autosync.resolve(self.a, self.config, first["receipt"]["op"], "mine", cwd=self.code)
        memory_sync.sync(self.b)
        second = memory_capture.save(
            self.b,
            self.code,
            self.config,
            event_id=EVENT,
            client="antigravity",
            target="project",
            kind="note",
            title="Capture fixture",
            body="Use fixture port 9443.",
            supersedes=source.id,
        )
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual("committed-locally", second["state"])
        self.assertTrue(second["replayed"])

    def test_legacy_operations_keep_their_existing_queue_shape(self):
        memory_project_writes.add(
            self.a,
            self.code,
            self.config,
            kind="note",
            title="Legacy fixture",
            body="Legacy writer fixture.",
        )
        self.assertNotIn("capture", memory_sync.pending(self.a)[0])
        self.assertNotIn("Agentbot-Capture:", git(self.a, "log", "-1", "--format=%B"))

    def test_conflicted_event_is_not_silently_recreated(self):
        source = next(r for r in memory.validate(self.a).records if r.path == LESSON)
        first = self.capture(supersedes=source.id)
        memory_project_writes.edit(
            self.b, self.code, self.config, LESSON, body="Other machine changed fixture source."
        )
        memory_sync.sync(self.b)
        memory_sync.sync(self.a)
        head = self.head(self.a)
        result = self.capture(supersedes=source.id)
        self.assertEqual("conflict", result["state"])
        self.assertEqual(first["receipt"], result["receipt"])
        self.assertEqual(head, self.head(self.a))

    def capture(self, **changes):
        args = {
            "event_id": EVENT,
            "client": "antigravity",
            "target": "project",
            "kind": "note",
            "title": "Capture fixture",
            "body": "Use fixture port 9443.",
        }
        args.update(changes)
        return memory_capture.save(self.a, self.code, self.config, **args)

    def test_retry_returns_original_receipt_without_a_new_commit(self):
        first = self.capture()
        head = self.head(self.a)
        second = self.capture()
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual((False, True), (first["replayed"], second["replayed"]))
        self.assertEqual("committed-locally", second["state"])
        self.assertEqual(head, self.head(self.a))
        self.assertEqual(1, len(memory_sync.pending(self.a)))

    def test_changed_payload_for_same_event_is_refused_without_writes(self):
        self.capture()
        head = self.head(self.a)
        with self.assertRaisesRegex(memory.MemoryVaultError, "different payload"):
            self.capture(body="Different fixture.")
        self.assertEqual(head, self.head(self.a))

    def test_correction_retry_precedes_supersession_validation(self):
        first = self.capture()
        args = {
            "event_id": "81ff7bb9-b4ca-4195-9179-d68d3c577d20",
            "title": "Corrected fixture",
            "body": "Use fixture port 9553.",
            "supersedes": first["receipt"]["id"],
        }
        corrected = self.capture(**args)
        head = self.head(self.a)
        self.assertEqual(corrected["receipt"], self.capture(**args)["receipt"])
        self.assertEqual(head, self.head(self.a))
        self.valid(self.a)

    def test_core_capture_is_only_a_draft_and_retries_after_restart(self):
        first = self.capture(target="proposal", kind="preference", body="Prefer fixture tabs.")
        restarted = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                "--root",
                str(self.tmp / "agentbot"),
                "memory",
                "capture",
                "--event-id",
                EVENT,
                "--client",
                "antigravity",
                "--target",
                "proposal",
                "--kind",
                "preference",
                "--title",
                "Capture fixture",
                "--stdin",
                "--json",
            ],
            input="Prefer fixture tabs.",
            text=True,
            capture_output=True,
            timeout=30,
            cwd=Path(__file__).resolve().parents[1],
            env={
                "PATH": os.defpath,
                "HOME": str(self.tmp / "fresh-home"),
                "XDG_CONFIG_HOME": str(self.config),
                "NO_COLOR": "1",
                "AGENTBOT_MEMORY_DIR": str(self.a),
                "AGENTBOT_CALLER_PWD": str(self.code),
                "AGENTBOT_MEMORY_NO_SYNC": "1",
            },
        )
        self.assertEqual(0, restarted.returncode, restarted.stderr)
        second = json.loads(restarted.stdout)
        self.assertEqual(first["receipt"], second["receipt"])
        record = next(r for r in memory.validate(self.a).records if r.id == first["receipt"]["id"])
        self.assertTrue(record.draft)
        hits = memory_retrieve.search(
            self.a, "fixture tabs", memory_retrieve.Request(project="alpha")
        )
        self.assertFalse(hits.hits)

    def test_event_is_bound_to_registered_project_identity(self):
        first = self.capture()
        beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")
        memory_projects.register(self.a, beta, self.config)
        second = memory_capture.save(
            self.a,
            beta,
            self.config,
            event_id=EVENT,
            client="antigravity",
            target="project",
            kind="note",
            title="Beta fixture",
            body="Use beta fixture port 9553.",
        )
        self.assertNotEqual(first["receipt"]["op"], second["receipt"]["op"])
        self.assertTrue(second["receipt"]["path"].startswith("projects/beta/"))

    def test_secrets_are_refused_without_consuming_the_event(self):
        head = self.head(self.a)
        with self.assertRaises(memory.MemoryVaultError):
            self.capture(body=f"Fixture credential {FAKE_GITHUB_TOKEN}")
        self.assertEqual(head, self.head(self.a))
        self.assertEqual("committed-locally", self.capture()["state"])

    def test_response_loss_after_commit_is_recovered_from_history(self):
        original = memory_sync._save

        def fail_after_commit(root, name, items):
            if name == "pending.json" and any(i.get("commit") for i in items):
                raise OSError("synthetic response loss")
            return original(root, name, items)

        with patch.object(memory_sync, "_save", side_effect=fail_after_commit):
            with self.assertRaises(OSError):
                self.capture()
        head = self.head(self.a)
        result = self.capture()
        self.assertTrue(result["replayed"])
        self.assertEqual("committed-locally", result["state"])
        self.assertEqual(head, self.head(self.a))

    def test_uncommitted_event_is_pending_until_engine_replay(self):
        head = self.head(self.a)
        with patch.object(memory_sync, "_commit", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.capture()
        pending = self.capture()
        self.assertEqual("pending", pending["state"])
        self.assertEqual(head, self.head(self.a))
        self.assertEqual(1, len(memory_sync.pending(self.a)))
        memory_sync.sync(self.a)
        saved = self.capture()
        self.assertEqual("committed-locally", saved["state"])
        self.assertEqual(pending["receipt"], saved["receipt"])

    def test_synced_clone_returns_the_same_operation_receipt(self):
        first = self.capture()
        memory_sync.sync(self.a)
        memory_sync.sync(self.b)
        head = self.head(self.b)
        second = memory_capture.save(
            self.b,
            self.code,
            self.config,
            event_id=EVENT,
            client="antigravity",
            target="project",
            kind="note",
            title="Capture fixture",
            body="Use fixture port 9443.",
        )
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual(head, self.head(self.b))
        self.assertNotIn("Use fixture port", git(self.a, "log", "-1", "--format=%B"))

    def test_simultaneous_delivery_serializes_receipt_lookup_and_write(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.capture(), range(2)))
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])
        self.assertEqual([False, True], sorted(r["replayed"] for r in results))
        self.assertEqual(1, len(memory_sync.pending(self.a)))

    def test_unknown_client_event_or_accepted_core_target_is_refused(self):
        head = self.head(self.a)
        for args in ({"client": "unknown"}, {"event_id": "not-a-uuid"}, {"target": "core"}):
            with self.subTest(args=args), self.assertRaises(memory.MemoryVaultError):
                self.capture(**args)
        self.assertEqual(head, self.head(self.a))
