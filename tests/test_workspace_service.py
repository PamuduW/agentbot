from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.paths import AgentbotPaths
from src.workspace_service import WorkspaceService
from src.workspace_state import WorkspaceRecord


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.product_root = Path(__file__).parents[1]
        self.config_home = self.root / "config"
        self.paths = AgentbotPaths(
            root=self.product_root,
            codex_home=self.root / "codex",
            claude_home=self.root / "claude",
            cursor_home=self.root / "cursor",
            config_home=self.config_home,
            agents_home=self.root / "agents",
        )
        self.workspace_service = WorkspaceService(self.paths)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git_repo(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        return path

    def test_apply_registers_a_successful_folder_render(self) -> None:
        repo = self._git_repo("good-repo")

        result = self.workspace_service.apply(
            repo,
            profile_name=None,
            targets=("agents", "claude"),
            register=True,
        )

        self.assertEqual("applied", result.status)
        self.assertTrue((repo / "AGENTS.md").is_file())
        self.assertTrue((repo / "CLAUDE.md").is_file())
        records = self.workspace_service.store.load()
        self.assertEqual([str(repo.resolve())], [item.path for item in records])
        self.assertEqual("git", records[0].kind)
        self.assertEqual(("agents", "claude"), records[0].targets)

    def test_workspace_service_uses_its_injected_runner_for_git_identity(self) -> None:
        """Break caught: workspace probes bypass the lifecycle runner through a module global."""
        from src.command_runner import CommandResult

        workspace = self.root / "runner-workspace"
        workspace.mkdir()
        runner = mock.Mock()
        runner.run.return_value = CommandResult(1)
        service = WorkspaceService(self.paths, command_runner=runner)

        result = service.preview(workspace, profile_name=None, targets=("agents",))

        self.assertEqual("preview", result.status)
        runner.run.assert_called()
        self.assertEqual("rev-parse", runner.run.call_args.args[0][3])

    def test_custom_agents_are_preserved_and_recorded(self) -> None:
        custom = self.root / "custom-folder"
        custom.mkdir()
        (custom / "AGENTS.md").write_text("# User policy\n", encoding="utf-8")

        result = self.workspace_service.apply(
            custom,
            profile_name=None,
            targets=("claude",),
            register=True,
        )

        self.assertEqual("applied", result.status)
        self.assertEqual("# User policy\n", (custom / "AGENTS.md").read_text(encoding="utf-8"))
        record = self.workspace_service.store.load()[0]
        self.assertEqual("directory", record.kind)
        self.assertEqual("custom", record.policy_mode)
        self.assertEqual(("agents", "claude"), record.targets)

    def test_batch_continues_after_one_workspace_review_template(self) -> None:
        good = self._git_repo("good")
        conflict = self.root / "conflict"
        conflict.mkdir()
        (conflict / "AGENTS.md").write_text("# User policy\n", encoding="utf-8")
        (conflict / "CLAUDE.md").write_text("# User instructions\n", encoding="utf-8")

        self.workspace_service.store.replace(
            (
                WorkspaceRecord(
                    path=str(good.resolve()),
                    kind="git",
                    policy_mode="managed",
                    profile="safe-default",
                    targets=("agents", "claude"),
                    enabled=True,
                    last_commit=None,
                    last_rendered_at=None,
                ),
                WorkspaceRecord(
                    path=str(conflict.resolve()),
                    kind="directory",
                    policy_mode="custom",
                    profile="safe-default",
                    targets=("agents", "claude"),
                    enabled=True,
                    last_commit=None,
                    last_rendered_at=None,
                ),
            )
        )

        report = self.workspace_service.resync(apply=False)

        self.assertEqual(2, len(report.results))
        statuses = {result.path.name: result.status for result in report.results}
        self.assertEqual("preview", statuses["good"])
        self.assertEqual("preview", statuses["conflict"])

    def test_recorded_git_workspace_that_loses_git_identity_fails(self) -> None:
        repo = self._git_repo("recorded")
        self.workspace_service.store.replace(
            (
                WorkspaceRecord(
                    path=str(repo.resolve()),
                    kind="git",
                    policy_mode="managed",
                    profile="safe-default",
                    targets=("agents",),
                    enabled=True,
                    last_commit=None,
                    last_rendered_at=None,
                ),
            )
        )
        (repo / ".git").rename(repo / ".git.saved")

        report = self.workspace_service.resync(apply=False)

        self.assertEqual("failed", report.results[0].status)
        self.assertIn("no longer resolves to the recorded Git root", report.results[0].message)

    def test_symlinked_output_is_reported_as_conflict(self) -> None:
        target = self.root / "symlinked"
        target.mkdir()
        outside = self.root / "outside-claude.md"
        outside.write_text("# User-owned\n", encoding="utf-8")
        (target / "CLAUDE.md").symlink_to(outside)

        result = self.workspace_service.preview(
            target,
            profile_name=None,
            targets=("claude",),
        )

        self.assertEqual("conflict", result.status)
        self.assertIn("symlink", result.message)

    def test_apply_preserves_user_file_and_writes_review_template(self) -> None:
        target = self.root / "review-apply"
        target.mkdir()
        claude = target / "CLAUDE.md"
        claude.write_text("# User-owned\n", encoding="utf-8")

        result = self.workspace_service.apply(
            target,
            profile_name=None,
            targets=("claude",),
            register=True,
        )

        self.assertEqual("applied", result.status)
        self.assertEqual("# User-owned\n", claude.read_text(encoding="utf-8"))
        review = target / "CLAUDE_temp.md"
        self.assertTrue(review.is_file())
        self.assertIn("sha256=", review.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.workspace_service.store.load()))

    def test_apply_does_not_overwrite_an_edited_current_review_template(self) -> None:
        target = self.root / "review-repeat"
        target.mkdir()
        claude = target / "CLAUDE.md"
        claude.write_text("# User-owned\n", encoding="utf-8")

        first = self.workspace_service.apply(
            target,
            profile_name=None,
            targets=("claude",),
            register=True,
        )
        self.assertEqual("applied", first.status)

        review = target / "CLAUDE_temp.md"
        review.write_text(review.read_text(encoding="utf-8") + "\nMy notes.\n", encoding="utf-8")

        second = self.workspace_service.apply(
            target,
            profile_name=None,
            targets=("claude",),
            register=True,
        )

        self.assertEqual("applied", second.status)
        self.assertIn("My notes.", review.read_text(encoding="utf-8"))
        self.assertFalse((target / "CLAUDE_temp_1.md").exists())

    def test_remove_forgets_missing_workspace_without_touching_its_path(self) -> None:
        missing = self.root / "missing-workspace"
        record = WorkspaceRecord(
            path=str(missing.resolve()),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        self.workspace_service.store.replace((record,))

        removed = self.workspace_service.remove(missing)

        self.assertEqual(record, removed)
        self.assertEqual((), self.workspace_service.store.load())
        self.assertFalse(missing.exists())

    def _registered(self, path: Path) -> WorkspaceRecord:
        record = WorkspaceRecord(
            path=str(path),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        self.workspace_service.store.replace((record,))
        return record

    def test_a_missing_workspace_is_skipped_and_kept(self) -> None:
        # A deleted temporary repository used to fail every update until it
        # was removed by hand.
        record = self._registered(self.root.resolve() / "deleted-temp-repo")
        for apply in (False, True):
            with self.subTest(apply=apply):
                report = self.workspace_service.resync(apply=apply)
                self.assertEqual(["skipped"], [r.status for r in report.results])
                self.assertIn("workspaces --remove", report.results[0].message)
                self.assertEqual((record,), self.workspace_service.store.load())

    def test_remove_unregistered_workspace_does_not_rewrite_registry(self) -> None:
        registered = self.root / "registered"
        record = WorkspaceRecord(
            path=str(registered.resolve()),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        self.workspace_service.store.replace((record,))
        before = self.paths.workspace_state_file.read_bytes()

        with self.assertRaisesRegex(ValueError, "workspace is not registered"):
            self.workspace_service.remove(self.root / "unknown")

        self.assertEqual(before, self.paths.workspace_state_file.read_bytes())

    def test_generated_output_for_an_unregistered_target_is_reported(self):
        from src.workspace_state import WorkspaceRecord

        workspace = self.root / "ws"
        (workspace / ".cursor" / "rules").mkdir(parents=True)
        (workspace / "AGENTS.md").write_text("# policy\n", encoding="utf-8")
        # An Agentbot-generated Cursor rule left behind after the workspace was
        # re-registered without the cursor target.
        (workspace / ".cursor" / "rules" / "agentbot-policy.mdc").write_text(
            "---\ndescription: x\n---\n\n<!-- BEGIN AGENTBOT MANAGED BASELINE -->\n"
            "old\n<!-- END AGENTBOT MANAGED BASELINE -->\n",
            encoding="utf-8",
        )
        record = WorkspaceRecord(
            path=str(workspace),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )

        orphans = self.workspace_service._orphaned_output_results(record)

        self.assertEqual(1, len(orphans))
        self.assertEqual("conflict", orphans[0].status)
        self.assertIn("agentbot-policy.mdc", orphans[0].message)
        self.assertIn("--cursor", orphans[0].message)

    def test_a_user_authored_file_at_a_target_path_is_left_alone(self):
        from src.workspace_state import WorkspaceRecord

        workspace = self.root / "ws2"
        (workspace / ".cursor" / "rules").mkdir(parents=True)
        # No managed marker: the user wrote this, it is not ours to police.
        (workspace / ".cursor" / "rules" / "agentbot-policy.mdc").write_text(
            "my own rules\n", encoding="utf-8"
        )
        record = WorkspaceRecord(
            path=str(workspace),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )

        self.assertEqual([], self.workspace_service._orphaned_output_results(record))

    def test_a_registered_target_is_not_reported_as_an_orphan(self):
        from src.workspace_state import WorkspaceRecord

        workspace = self.root / "ws3"
        (workspace / ".cursor" / "rules").mkdir(parents=True)
        (workspace / ".cursor" / "rules" / "agentbot-policy.mdc").write_text(
            "<!-- BEGIN AGENTBOT MANAGED BASELINE -->\n", encoding="utf-8"
        )
        record = WorkspaceRecord(
            path=str(workspace),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents", "cursor"),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )

        self.assertEqual([], self.workspace_service._orphaned_output_results(record))

    def test_a_retired_target_output_is_reported_with_delete_advice(self):
        from src.workspace_state import WorkspaceRecord

        workspace = self.root / "ws-retired"
        (workspace / ".github").mkdir(parents=True)
        (workspace / ".github" / "copilot-instructions.md").write_text(
            "<!-- BEGIN AGENTBOT MANAGED BASELINE -->\nold\n"
            "<!-- END AGENTBOT MANAGED BASELINE -->\n",
            encoding="utf-8",
        )
        record = WorkspaceRecord(
            path=str(workspace),
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )

        orphans = self.workspace_service._orphaned_output_results(record)

        self.assertEqual(1, len(orphans))
        self.assertIn("copilot-instructions.md", orphans[0].message)
        # Retired targets cannot be re-registered, so the advice must differ.
        self.assertIn("was removed, so delete the file", orphans[0].message)
        self.assertNotIn("Re-register", orphans[0].message)


if __name__ == "__main__":
    unittest.main()
