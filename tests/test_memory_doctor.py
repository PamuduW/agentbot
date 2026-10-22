"""Doctor reports what the memory vault needs from the user, and nothing when it is healthy."""

from __future__ import annotations

import os
from unittest.mock import patch

from src import memory_autosync as autosync
from src import memory_capture_queue, memory_read, memory_sync
from src import memory_proposals as proposals
from src.diagnostics import Diagnostics
from tests.support import agentbot_paths
from tests.test_memory_autosync import AutosyncTestCase


class MemoryDoctorTests(AutosyncTestCase):
    def issues(self, vault=None) -> list[str]:
        paths = agentbot_paths(self.tmp / "agentbot")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        if vault is not None:
            env["AGENTBOT_MEMORY_DIR"] = str(vault)
        with patch.dict(os.environ, env, clear=True):
            return [
                f"{issue.level}: {issue.message}"
                for issue in Diagnostics(paths)._memory_doctor_issues()
            ]

    def test_pending_state_inspection_needs_no_directory_creation(self) -> None:
        with patch("src.memory_sync.Path.mkdir", side_effect=PermissionError("read only")):
            self.assertIsInstance(memory_sync.pending(self.a), list)
            self.assertIsInstance(memory_sync.conflicts(self.a), list)
            self.assertTrue(any("wait for your approval" in issue for issue in self.issues(self.a)))

    def test_private_pending_capture_is_reported_without_fact_text(self) -> None:
        paths = agentbot_paths(self.tmp / "agentbot")
        memory_capture_queue.enqueue(
            self.a,
            self.code,
            paths.config_home,
            {
                "event_id": "9f43bcf0-64f6-4a43-8bc5-a86daf7282bb",
                "client": "codex",
                "target": "project",
                "kind": "note",
                "title": "PRIVATE TITLE",
                "body": "PRIVATE BODY",
            },
        )
        issues = self.issues(self.a)
        self.assertTrue(any("capture" in issue and "pending" in issue for issue in issues))
        self.assertFalse(any("PRIVATE" in issue for issue in issues))

    def test_incomplete_snapshot_is_attention_without_a_broken_vault_claim(self) -> None:
        with patch.object(
            memory_read, "consistent", side_effect=memory_read.MemoryReadUnavailable("PRIVATE")
        ):
            issues = self.issues(self.a)
        self.assertTrue(any("snapshot unavailable" in issue for issue in issues))
        self.assertFalse(any("PRIVATE" in issue or "broken" in issue for issue in issues))

    def test_malformed_private_queue_is_sanitized_and_preserved(self) -> None:
        paths = agentbot_paths(self.tmp / "agentbot")
        path = paths.config_home / "memory-capture/queue.json"
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_text("PRIVATE MALFORMED INPUT")
        path.chmod(0o600)
        issues = self.issues(self.a)
        self.assertTrue(any("queue is unavailable" in issue for issue in issues))
        self.assertFalse(any("PRIVATE" in issue for issue in issues))
        self.assertEqual("PRIVATE MALFORMED INPUT", path.read_text())

    def test_no_vault_and_a_healthy_vault_add_nothing(self) -> None:
        self.assertEqual([], self.issues())
        proposals.reject(self.a, "proposals/core/lessons/p.md")
        autosync.after_write(self.a, self.config)
        self.assertEqual([], self.issues(self.a))

    def test_each_thing_that_needs_the_user_is_reported(self) -> None:
        # The fixture ships one proposal waiting for approval.
        self.assertTrue(any("wait for your approval" in i for i in self.issues(self.a)))
        note = self.a / "projects/alpha/notes/n.md"
        note.write_text(note.read_text() + "\nhand edit\n")
        self.assertTrue(any("sync is paused" in i for i in self.issues(self.a)))

    def test_a_broken_vault_is_an_error(self) -> None:
        (self.a / ".meta/vault.json").write_text("not json")
        found = self.issues(self.a)
        self.assertTrue(found and found[0].startswith("error: memory vault"))
