"""Approval and the owned vault hooks, against sanitized v2 fixtures."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import memory, memory_approve, memory_hook
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
BODY = f"## Evidence\n\nSeen {SENTINEL}.\n"
E_ID = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8"


class ApproveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.vault = v2_vault(self.tmp / "v")
        self.vault.commit()
        self.config = self.tmp / "config"

    def draft(self, title: str = "Verify the build", body: str = BODY) -> str:
        result = drafts.propose(
            self.vault.root, drafts.Proposal("lesson", title, "global"), body, today=TODAY
        )
        assert result.created and result.path is not None, result.findings
        return result.path

    def approve(self, relative: str, **kwargs: object) -> memory_approve.ApprovalResult:
        return memory_approve.approve(self.vault.root, relative, config_home=self.config, **kwargs)  # type: ignore[arg-type]


class ApproveTests(ApproveTestCase):
    def test_preview_writes_nothing(self) -> None:
        relative = self.draft()
        before = _tree_digest(self.vault.root)
        result = self.approve(relative)
        self.assertEqual("preview", result.state, result.findings)
        self.assertEqual("lessons/2026-09-26-verify-the-build.md", result.destination)
        self.assertEqual(before, _tree_digest(self.vault.root))
        self.assertFalse(self.config.exists())

    def test_apply_installs_an_accepted_record_and_removes_the_draft(self) -> None:
        relative = self.draft()
        draft_text = (self.vault.root / relative).read_text()
        result = self.approve(relative, apply=True)
        self.assertEqual("applied", result.state, (result.findings, result.notes))
        assert result.destination is not None
        installed = (self.vault.root / result.destination).read_text()
        self.assertEqual(draft_text.replace("status: draft", "status: accepted"), installed)
        self.assertFalse((self.vault.root / relative).exists())
        report = memory.validate(self.vault.root)
        self.assertEqual([], report.findings)
        record = next(r for r in report.records if r.path == result.destination)
        self.assertEqual(("accepted", result.id), (record.status, record.id))
        # No lock and no temporary file left behind; nothing staged.
        self.assertEqual([], list((self.config / "memory-locks").iterdir()))
        self.assertEqual(
            [], [p.name for p in (self.vault.root / "lessons").iterdir() if p.name.startswith(".")]
        )
        staged = subprocess.run(
            ["git", "-C", str(self.vault.root), "diff", "--cached", "--name-only"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(b"", staged.stdout)

    def test_an_existing_destination_is_a_conflict_and_the_draft_survives(self) -> None:
        first, second = self.draft(), self.draft()
        self.assertEqual("applied", self.approve(first, apply=True).state)
        existing = (self.vault.root / "lessons/2026-09-26-verify-the-build.md").read_bytes()
        result = self.approve(second, apply=True)
        self.assertEqual("conflict", result.state)
        self.assertTrue((self.vault.root / second).exists())
        self.assertEqual(
            existing, (self.vault.root / "lessons/2026-09-26-verify-the-build.md").read_bytes()
        )

    def test_a_racing_human_edit_is_a_conflict(self) -> None:
        relative = self.draft()
        destination = self.vault.root / "lessons/2026-09-26-verify-the-build.md"

        def human() -> None:
            destination.write_text("written by a person\n")

        # The person writes the destination after the in-lock recheck passed:
        # only link()'s refusal to replace stands between them and a lost edit.
        with patch.object(memory_approve, "_recheck", side_effect=lambda root, plan: human()):
            result = self.approve(relative, apply=True)
        self.assertEqual("conflict", result.state)
        self.assertEqual("written by a person\n", destination.read_text())
        self.assertTrue((self.vault.root / relative).exists())

    def test_a_draft_edited_during_approval_is_a_conflict(self) -> None:
        relative = self.draft()

        def edit() -> None:
            with open(self.vault.root / relative, "a", encoding="utf-8") as stream:
                stream.write("late edit\n")

        with patch.object(memory_approve, "_install", wraps=memory_approve._install) as install:
            original = memory_approve._recheck

            def recheck(root: Path, plan: object) -> None:
                edit()
                original(root, plan)  # type: ignore[arg-type]

            with patch.object(memory_approve, "_recheck", side_effect=recheck):
                result = self.approve(relative, apply=True)
        self.assertEqual("conflict", result.state)
        install.assert_not_called()
        self.assertIn("late edit", (self.vault.root / relative).read_text())

    def test_an_interrupted_install_leaves_no_partial_record(self) -> None:
        relative = self.draft()
        before = _tree_digest(self.vault.root)
        with patch.object(memory_approve.os, "link", side_effect=OSError("disk vanished")):
            with self.assertRaises(OSError):
                self.approve(relative, apply=True)
        self.assertEqual(before, _tree_digest(self.vault.root))
        self.assertEqual(
            [], [p.name for p in (self.vault.root / "lessons").iterdir() if p.name.startswith(".")]
        )
        self.assertEqual([], list((self.config / "memory-locks").iterdir()))

    def test_a_live_lock_times_out_as_a_conflict(self) -> None:
        relative = self.draft()
        lock = memory_approve.lock_path(self.config, self.vault.root)
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "acquired": "earlier"}))
        result = self.approve(relative, apply=True, lock_wait=0.2)
        self.assertEqual("conflict", result.state)
        self.assertIn(f"pid {os.getpid()}", " ".join(result.notes))
        self.assertTrue((self.vault.root / relative).exists())
        self.assertTrue(lock.exists())

    def test_a_stale_lock_is_broken_and_recorded(self) -> None:
        relative = self.draft()
        dead = subprocess.Popen(["true"])
        dead.wait()
        lock = memory_approve.lock_path(self.config, self.vault.root)
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": dead.pid, "acquired": "earlier"}))
        result = self.approve(relative, apply=True)
        self.assertEqual("applied", result.state, result.notes)
        self.assertIn("stale lock", " ".join(result.notes))
        self.assertFalse(lock.exists())

    def test_sequential_approvals_to_one_destination_never_lose_a_record(self) -> None:
        paths = [self.draft() for _ in range(3)]
        states = [self.approve(path, apply=True).state for path in paths]
        self.assertEqual(["applied", "conflict", "conflict"], states)
        self.assertEqual(2, sum(1 for path in paths if (self.vault.root / path).exists()))

    def test_concurrent_processes_install_exactly_one_record(self) -> None:
        first, second = self.draft(), self.draft()
        script = (
            "import sys; from pathlib import Path; from src import memory_approve as m; "
            "r = m.approve(Path(sys.argv[1]), sys.argv[2], config_home=Path(sys.argv[3]), apply=True); "
            "print(r.state)"
        )
        root = Path(__file__).resolve().parents[1]
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.vault.root), path, str(self.config)],
                cwd=root,
                stdout=subprocess.PIPE,
                text=True,
            )
            for path in (first, second)
        ]
        states = sorted(process.communicate()[0].strip() for process in processes)
        self.assertEqual(["applied", "conflict"], states)
        self.assertEqual(1, sum(1 for path in (first, second) if (self.vault.root / path).exists()))
        self.assertEqual([], memory.validate(self.vault.root).findings)

    def test_invalid_drafts_and_secrets_are_refused(self) -> None:
        self.vault.write("drafts/bad.md", note(v2("lesson", E_ID, "global")))  # status accepted
        self.vault.write(
            "drafts/leak.md",
            note(
                v2("lesson", "7e2a4b6c-8d0e-4f1a-b2c3-d4e5f6a7b8c9", "global", status="draft"),
                body=FAKE_GITHUB_TOKEN,
            ),
        )
        self.vault.write(
            "drafts/warn.md",
            note(
                v2(
                    "lesson",
                    "8f3b5c7d-9e1f-4a2b-83c4-d5e6f7a8b9c0",
                    "global",
                    status="draft",
                    title="Warned",
                ),
                body=FAKE_PASSWORD_LINE + "\n",
            ),
        )
        self.vault.write(
            "drafts/supersede.md",
            note(
                v2(
                    "lesson",
                    "9a4c6d8e-0f2a-4b3c-94d5-e6f7a8b9c0d1",
                    "global",
                    status="draft",
                    supersedes=[ID_C],
                )
            ),
        )
        before = _tree_digest(self.vault.root)
        for relative, rule in (
            ("drafts/bad.md", "MEMORY_STATUS"),
            ("drafts/leak.md", "SECRET_GITHUB_CLASSIC_TOKEN"),
            ("drafts/warn.md", "WARN_PASSWORD_ASSIGNMENT"),
            ("drafts/supersede.md", "MEMORY_SUPERSEDES"),
        ):
            with self.subTest(relative):
                result = self.approve(relative, apply=True)
                self.assertEqual("refused", result.state)
                self.assertIn(rule, [item.rule for item in result.findings])
                self.assertNotIn(
                    FAKE_GITHUB_TOKEN, json.dumps(memory_approve.approval_json(result))
                )
        self.assertEqual(before, _tree_digest(self.vault.root))
        acknowledged = self.approve(
            "drafts/warn.md", apply=True, acknowledge=["WARN_PASSWORD_ASSIGNMENT"]
        )
        self.assertEqual("applied", acknowledged.state, acknowledged.findings)

    def test_a_duplicate_id_draft_is_refused(self) -> None:
        self.vault.write(
            "drafts/dup.md", note(v2("lesson", ID_C, "global", status="draft", title="Dup"))
        )
        result = self.approve("drafts/dup.md", apply=True)
        self.assertEqual("refused", result.state)

    def test_a_symlinked_destination_directory_is_refused(self) -> None:
        relative = self.draft()
        outside = self.tmp / "outside"
        outside.mkdir()
        for child in (self.vault.root / "lessons").iterdir():
            child.unlink()
        (self.vault.root / "lessons").rmdir()
        (self.vault.root / "lessons").symlink_to(outside, target_is_directory=True)
        try:
            result = self.approve(relative, apply=True)
        except memory.MemoryVaultError:
            pass
        else:
            self.assertNotEqual("applied", result.state)
        self.assertEqual([], list(outside.iterdir()))
        self.assertTrue((self.vault.root / relative).exists())

    def test_bad_arguments_and_v1_vaults(self) -> None:
        for bad in ("lessons/2026-09-23-new.md", "drafts/missing.md"):
            with self.subTest(bad), self.assertRaises(memory.MemoryVaultError):
                self.approve(bad)
        old = v1_vault(self.tmp / "old")
        with self.assertRaises(memory.MemoryVaultError):
            memory_approve.approve(
                old.root, "drafts/2026-09-23-a-draft.md", config_home=self.config
            )


class HookTests(ApproveTestCase):
    def hooks(self) -> Path:
        return self.vault.root / ".git" / "hooks"

    def test_install_status_and_remove_touch_only_owned_hooks(self) -> None:
        home = self.tmp / "agentbot"
        self.assertEqual(
            {"pre-commit": "absent", "pre-push": "absent"},
            memory_hook.inspect(self.vault.root, home).states,
        )
        state = memory_hook.install(self.vault.root, home)
        self.assertEqual(["pre-commit", "pre-push"], state.applied)
        text = (self.hooks() / "pre-commit").read_text()
        self.assertIn(memory_hook.MARKER, text.splitlines()[1])
        self.assertTrue(os.stat(self.hooks() / "pre-push").st_mode & stat.S_IXUSR)
        self.assertEqual(
            {"pre-commit": "owned", "pre-push": "owned"},
            memory_hook.inspect(self.vault.root, home).states,
        )
        moved = memory_hook.inspect(self.vault.root, self.tmp / "elsewhere")
        self.assertEqual({"pre-commit": "stale", "pre-push": "stale"}, moved.states)
        self.assertEqual(
            ["pre-commit", "pre-push"], memory_hook.remove(self.vault.root, home).applied
        )
        self.assertFalse((self.hooks() / "pre-commit").exists())

    def test_an_unowned_hook_is_never_replaced_or_removed(self) -> None:
        home = self.tmp / "agentbot"
        self.hooks().mkdir(exist_ok=True)
        (self.hooks() / "pre-push").write_text("#!/bin/sh\necho mine\n")
        state = memory_hook.install(self.vault.root, home)
        self.assertEqual({"pre-commit": "owned", "pre-push": "unowned"}, state.states)
        memory_hook.remove(self.vault.root, home)
        self.assertEqual("#!/bin/sh\necho mine\n", (self.hooks() / "pre-push").read_text())

    def test_a_shared_hooks_path_is_refused(self) -> None:
        shared = self.tmp / "shared-hooks"
        subprocess.run(
            ["git", "-C", str(self.vault.root), "config", "core.hooksPath", str(shared)], check=True
        )
        state = memory_hook.install(self.vault.root, self.tmp / "agentbot")
        self.assertIsNotNone(state.problem)
        self.assertFalse(shared.exists())

    def test_the_hook_blocks_a_commit_that_adds_a_secret(self) -> None:
        home = self.tmp / "agentbot"
        home.mkdir()
        # A stand-in installer: the hook's contract is to exec it with memory
        # validate and the vault named, and to stop when it fails.
        (home / "install.sh").write_text(
            "#!/bin/sh\n"
            f'cd {Path(__file__).resolve().parents[1]} && exec {sys.executable} -m src.cli --root "$PWD" "$@"\n'
        )
        (home / "install.sh").chmod(0o755)
        memory_hook.install(self.vault.root, home)
        self.vault.write(
            "lessons/leak.md", note(v2("lesson", E_ID, "global"), body=FAKE_GITHUB_TOKEN)
        )
        git = [
            "git",
            "-C",
            str(self.vault.root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
        ]
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        blocked = subprocess.run(
            [*git, "-c", "commit.gpgsign=false", "commit", "-qm", "leak"],
            capture_output=True,
            env=env,
        )
        self.assertNotEqual(0, blocked.returncode)
        self.assertIn(b"SECRET_GITHUB_CLASSIC_TOKEN", blocked.stdout + blocked.stderr)
        self.assertNotIn(FAKE_GITHUB_TOKEN.encode(), blocked.stdout + blocked.stderr)
        (self.vault.root / "lessons/leak.md").write_text(note(v2("lesson", E_ID, "global")))
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        allowed = subprocess.run(
            [*git, "-c", "commit.gpgsign=false", "commit", "-qm", "clean"],
            capture_output=True,
            env=env,
        )
        self.assertEqual(0, allowed.returncode, allowed.stdout + allowed.stderr)

    def test_a_missing_agentbot_fails_closed(self) -> None:
        memory_hook.install(self.vault.root, self.tmp / "gone")
        result = subprocess.run(
            [str(self.hooks() / "pre-commit")], cwd=self.vault.root, capture_output=True
        )
        self.assertEqual(1, result.returncode)
        self.assertIn(b"refusing", result.stderr)


class ApproveCliTests(ApproveTestCase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            AGENTBOT_MEMORY_DIR=str(self.vault.root),
            XDG_CONFIG_HOME=str(self.config),
        )
        with patch.dict(os.environ, env, clear=True):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_preview_then_apply(self) -> None:
        relative = self.draft()
        rc, stdout = self._cli("memory", "approve", relative)
        self.assertEqual(0, rc, stdout)
        self.assertIn("Rerun with --yes", stdout)
        self.assertTrue((self.vault.root / relative).exists())
        rc, stdout = self._cli("memory", "approve", relative, "--yes", "--json")
        self.assertEqual(0, rc, stdout)
        self.assertEqual("applied", json.loads(stdout)["state"])
        self.assertNotIn(SENTINEL, stdout)
        rc, _ = self._cli("memory", "approve", relative, "--yes")
        self.assertEqual(1, rc)

    def test_hook_preview_does_not_write(self) -> None:
        rc, stdout = self._cli("memory", "hook", "install")
        self.assertEqual(0, rc, stdout)
        self.assertIn("Preview only", stdout)
        self.assertIn(".obsidian/ is not scanned", stdout)
        self.assertFalse((self.vault.root / ".git/hooks/pre-commit").exists())
        rc, stdout = self._cli("memory", "hook", "install", "--yes")
        self.assertEqual(0, rc, stdout)
        self.assertTrue((self.vault.root / ".git/hooks/pre-commit").exists())


if __name__ == "__main__":
    unittest.main()
