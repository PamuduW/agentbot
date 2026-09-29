"""Doctor reports what the memory vault needs from the user, and nothing when it is healthy."""

from __future__ import annotations

import os
from unittest.mock import patch

from src import memory_autosync as autosync
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
