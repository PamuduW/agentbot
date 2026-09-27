"""Ticket M7: tracked core proposals on a schema 3 vault."""

from __future__ import annotations

import io
import json
import os
from unittest.mock import patch

from src import memory
from src import memory_autosync as autosync
from src import memory_proposals as proposals
from src import memory_retrieve as retrieve
from src import memory_sync as sync
from src.memory import MemoryVaultError
from tests.support import TerminalInput, run_cli_main
from tests.test_memory import FAKE_GITHUB_TOKEN, uid
from tests.test_memory_autosync import AutosyncTestCase
from tests.test_memory_sync import git


class ProposalCase(AutosyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Start from an empty queue on both machines: the shared fixture ships one proposal.
        proposals.reject(self.a, "proposals/core/lessons/p.md")
        autosync.after_write(self.a, self.config)
        autosync.run(self.b)


class ProposeTests(ProposalCase):
    def test_a_proposal_is_tracked_synced_and_never_retrieved(self) -> None:
        result = proposals.propose(
            self.a, kind="lesson", title="Prefer small commits", body="Evidence here."
        )
        path = result["path"]
        self.assertTrue(path.startswith("proposals/core/lessons/"))
        self.assertEqual([], memory.validate(self.a).findings)
        self.assertIn(path, git(self.a, "ls-files"))
        autosync.after_write(self.a, self.config)
        autosync.run(self.b)
        self.assertTrue((self.b / path).exists())
        hits = retrieve.search(
            self.b, "commits", retrieve.Request(cross_project=True, history=True), limit=50
        ).hits
        self.assertFalse(any(hit.record.path.startswith("proposals/") for hit in hits))
        self.assertEqual([path], [item["path"] for item in proposals.queue(self.b)])

    def test_bad_duplicate_and_secret_proposals_write_nothing(self) -> None:
        proposals.propose(self.a, kind="lesson", title="One idea", body="x")
        head = git(self.a, "rev-parse", "HEAD")
        attempts = [
            {"kind": "lesson", "title": "one idea", "body": "duplicate title"},
            {"kind": "lesson", "title": "Leak", "body": f"token {FAKE_GITHUB_TOKEN}"},
            {
                "kind": "lesson",
                "title": "Project scoped",
                "body": "x",
                "scope": "project",
                "projects": ["alpha"],
            },
            {"kind": "context", "title": "Wrong kind", "body": "x"},
        ]
        for attempt in attempts:
            with self.subTest(title=attempt["title"]), self.assertRaises(MemoryVaultError):
                proposals.propose(self.a, **attempt)  # type: ignore[arg-type]
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))

    def test_the_queue_warns_then_stops(self) -> None:
        with patch.object(proposals, "SOFT_CAP", 2), patch.object(proposals, "HARD_CAP", 3):
            self.assertNotIn(
                "warning", proposals.propose(self.a, kind="lesson", title="p1", body="x")
            )
            self.assertIn("warning", proposals.propose(self.a, kind="lesson", title="p2", body="x"))
            proposals.propose(self.a, kind="lesson", title="p3", body="x")
            with self.assertRaises(MemoryVaultError):
                proposals.propose(self.a, kind="lesson", title="p4", body="x")

    def test_a_proposal_operation_cannot_touch_core(self) -> None:
        with self.assertRaises(MemoryVaultError):
            sync.change_core(self.a, [("core/lessons/x.md", b"---\n---\n")], tier="proposal")


class ApproveRejectTests(ProposalCase):
    def test_approve_installs_in_core_and_removes_the_proposal_in_one_commit(self) -> None:
        proposed = proposals.propose(
            self.a, kind="decision", title="Adopt two tiers", body="Why: R7."
        )
        before = git(self.a, "rev-list", "--count", "HEAD")
        result = proposals.approve(self.a, proposed["path"])
        self.assertEqual(int(before) + 1, int(git(self.a, "rev-list", "--count", "HEAD")))
        self.assertFalse((self.a / proposed["path"]).exists())
        records = {r.path: r for r in memory.validate(self.a).records}
        installed = records[result["path"]]
        self.assertEqual(("accepted", proposed["id"]), (installed.status, installed.id))
        self.assertTrue(result["path"].startswith("core/decisions/"))
        self.assertEqual([], memory.validate(self.a).findings)

    def test_a_preference_proposal_replaces_preferences_and_keeps_its_id(self) -> None:
        proposed = proposals.propose(
            self.a, kind="preference", title="Working preferences", body="- Plain words first."
        )
        proposals.approve(self.a, proposed["path"])
        records = {r.path: r for r in memory.validate(self.a).records}
        self.assertEqual(uid(1), records["core/user/preferences.md"].id)
        self.assertIn("Plain words first.", (self.a / "core/user/preferences.md").read_text())
        self.assertEqual([], memory.validate(self.a).findings)

    def test_approval_supersedes_core_records_in_the_same_change(self) -> None:
        proposed = proposals.propose(
            self.a, kind="lesson", title="Newer core lesson", body="x", supersedes=[uid(3)]
        )
        proposals.approve(self.a, proposed["path"])
        records = {r.path: r for r in memory.validate(self.a).records}
        self.assertEqual("superseded", records["core/lessons/2026-09-27-core.md"].status)
        self.assertEqual([], memory.validate(self.a).findings)

    def test_approval_refuses_project_targets_and_existing_destinations(self) -> None:
        # A proposal to supersede a project record is refused when it is made:
        # the vault would hold a cross-tier supersession.
        with self.assertRaises(MemoryVaultError) as raised:
            proposals.propose(
                self.a, kind="lesson", title="Cross tier", body="x", supersedes=[uid(6)]
            )
        self.assertIn("MEMORY_SUPERSEDES_TIER", str(raised.exception))
        clash = proposals.propose(self.a, kind="lesson", title="Clash", body="x")
        name = clash["path"].rsplit("/", 1)[-1]
        taken = (
            (self.a / clash["path"]).read_bytes().replace(clash["id"].encode(), uid(99).encode())
        )
        self.vault_write(f"core/lessons/{name}", taken)
        with self.assertRaises(MemoryVaultError):
            proposals.approve(self.a, clash["path"])

    def vault_write(self, relative: str, data: bytes) -> None:
        sync.change_core(
            self.a, [(relative, data.replace(b"status: draft", b"status: accepted"))], tier="core"
        )

    def test_reject_removes_the_proposal(self) -> None:
        proposed = proposals.propose(self.a, kind="lesson", title="Not this", body="x")
        proposals.reject(self.a, proposed["path"])
        self.assertFalse((self.a / proposed["path"]).exists())
        self.assertEqual([], proposals.queue(self.a))
        for bad in (
            "core/lessons/2026-09-27-core.md",
            "proposals/core/../core/x.md",
            "proposals/core/none.md",
        ):
            with self.subTest(bad=bad), self.assertRaises(MemoryVaultError):
                proposals.reject(self.a, bad)

    def test_two_machines_approving_one_proposal_converge_without_conflict(self) -> None:
        proposed = proposals.propose(self.a, kind="lesson", title="Shared lesson", body="x")
        autosync.after_write(self.a, self.config)
        autosync.run(self.b)
        proposals.approve(self.a, proposed["path"])
        autosync.after_write(self.a, self.config)
        proposals.approve(self.b, proposed["path"])
        outcome = autosync.after_write(self.b, self.config_b)
        # Both approvals produce identical bytes, so the second replays as a no-op.
        self.assertEqual(("synced", 0), (outcome["state"], outcome["conflicts"]))
        self.assertEqual(
            git(self.a, "rev-parse", "HEAD^{tree}"), git(self.b, "rev-parse", "HEAD^{tree}")
        )
        self.assertEqual([], memory.validate(self.b).findings)

    def test_different_core_approvals_of_one_file_leave_a_human_conflict(self) -> None:
        first = proposals.propose(self.a, kind="preference", title="Prefs A", body="- A")
        autosync.after_write(self.a, self.config)
        autosync.run(self.b)
        second = proposals.propose(self.b, kind="preference", title="Prefs B", body="- B")
        autosync.after_write(self.b, self.config_b)
        autosync.run(self.a)
        proposals.approve(self.a, first["path"])
        autosync.after_write(self.a, self.config)
        proposals.approve(self.b, second["path"])
        outcome = autosync.after_write(self.b, self.config_b)
        self.assertEqual(1, outcome["conflicts"])
        (conflict,) = autosync.open_conflicts(self.b)
        self.assertEqual("human", conflict["resolver"])
        self.assertEqual([], memory.validate(self.b).findings)


class ProposalCliTests(ProposalCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.tmp / "xdg" / "agentbot"
        from src import memory_setup

        memory_setup.setup_path(self.a, self.config, apply=True)

    def _cli(self, *argv: str, stdin: bytes = b"", typed: str | None = None) -> tuple[int, dict]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            XDG_CONFIG_HOME=str(self.tmp / "xdg"),
            AGENTBOT_CALLER_PWD=str(self.tmp),
        )
        stream: io.TextIOBase = (
            TerminalInput(typed + "\n")
            if typed is not None
            else io.TextIOWrapper(io.BytesIO(stdin))
        )
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin", stream):
            rc, stdout, stderr = run_cli_main(
                ["agentbot", "--root", str(self.tmp / "agentbot"), *argv, "--json"]
            )
        self.stderr = stderr
        return rc, json.loads(stdout) if stdout.strip() else {}

    def test_cli_propose_review_approve(self) -> None:
        _rc, proposed = self._cli(
            "memory",
            "propose",
            "--type",
            "lesson",
            "--title",
            "Via CLI",
            "--scope",
            "global",
            "--stdin",
            stdin=b"body\n",
        )
        self.assertEqual(("proposed", "synced"), (proposed["state"], proposed["sync"]["state"]))
        _rc, queue = self._cli("memory", "review")
        self.assertEqual(1, queue["open"])
        _rc, one = self._cli("memory", "review", proposed["path"])
        self.assertEqual(("open", "body"), (one["state"], one["body"]))
        _rc, preview = self._cli("memory", "approve", proposed["path"])
        self.assertEqual("preview", preview["state"])
        self.assertTrue((self.a / proposed["path"]).exists())
        code = proposed["id"][:8]
        _rc, approved = self._cli("memory", "approve", proposed["path"], "--yes", typed=code)
        self.assertEqual("approved", approved["state"])
        self.assertIn(
            f"Agentbot-Op: {approved['op']}", git(self.origin, "log", "--format=%B", "main")
        )
        rc, _missing = self._cli("memory", "reject", "proposals/core/lessons/x.md")
        self.assertEqual(1, rc)

    def test_approve_and_reject_need_a_person_at_a_terminal(self) -> None:
        """Ticket 12 run 4: Codex ran approve --yes itself when told to approve."""
        _rc, proposed = self._cli(
            "memory",
            "propose",
            "--type",
            "lesson",
            "--title",
            "Guarded",
            "--scope",
            "global",
            "--stdin",
            stdin=b"x\n",
        )
        head = git(self.a, "rev-parse", "HEAD")
        for command in ("approve", "reject"):
            with self.subTest(command=command):
                rc, _ = self._cli("memory", command, proposed["path"], "--yes")
                self.assertEqual(1, rc)
                self.assertIn("human-only", self.stderr)
                self.assertIn(f"agentbot memory {command} {proposed['path']} --yes", self.stderr)
                rc, _ = self._cli("memory", command, proposed["path"], "--yes", typed="nope")
                self.assertEqual(1, rc)
                self.assertIn("cancelled", self.stderr)
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))
        _rc, rejected = self._cli(
            "memory", "reject", proposed["path"], "--yes", typed=proposed["id"][:8]
        )
        self.assertEqual("rejected", rejected["state"])

    def test_a_core_conflict_is_resolved_only_by_hand(self) -> None:
        first = proposals.propose(self.a, kind="preference", title="Prefs A", body="- A")
        autosync.after_write(self.a, self.config)
        autosync.run(self.b)
        second = proposals.propose(self.b, kind="preference", title="Prefs B", body="- B")
        autosync.after_write(self.b, self.config_b)
        autosync.run(self.a)
        proposals.approve(self.b, second["path"])
        autosync.after_write(self.b, self.config_b)
        proposals.approve(self.a, first["path"])
        self.assertEqual(1, autosync.after_write(self.a, self.config)["conflicts"])
        (conflict,) = autosync.open_conflicts(self.a)
        op = conflict["op"]
        rc, _ = self._cli("memory", "conflict", "resolve", op, "--keep", "theirs")
        self.assertEqual(1, rc)
        self.assertIn("human-only", self.stderr)
        self.assertEqual(1, len(autosync.open_conflicts(self.a)))
        rc, resolved = self._cli(
            "memory", "conflict", "resolve", op, "--keep", "theirs", typed=op[:8]
        )
        self.assertEqual((0, "theirs"), (rc, resolved["kept"]))
        self.assertEqual([], autosync.open_conflicts(self.a))


if __name__ == "__main__":
    import unittest

    unittest.main()
