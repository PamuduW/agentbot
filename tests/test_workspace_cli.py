from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.workspace_render import RenderAction
from src.workspace_service import WorkspaceReport, WorkspaceResult
from src.workspace_state import WorkspaceRecord
from tests.support import run_cli_main


class WorkspaceCliTests(unittest.TestCase):
    def _service(self) -> MagicMock:
        service = MagicMock()
        service.preview_workspace.return_value = WorkspaceResult(
            Path("/repo"),
            "preview",
            (
                RenderAction(
                    "AGENTS.md",
                    "create",
                    "# generated\n",
                    "create managed AGENTS.md",
                ),
            ),
            "preview directory workspace /repo; policy=managed",
        )
        service.apply_workspace.return_value = WorkspaceResult(
            Path("/repo"),
            "applied",
            (),
            "applied directory workspace /repo; policy=managed",
        )
        service.resync_workspaces.return_value = WorkspaceReport(())
        service.list_workspaces.return_value = ()
        return service

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_workspace_is_preview_only_without_yes(self, service_type, _default_paths) -> None:
        service = self._service()
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "workspace", "/repo"])

        self.assertEqual(0, rc)
        service.preview_workspace.assert_called_once_with(
            Path("/repo"),
            profile=None,
            targets=None,
        )
        service.apply_workspace.assert_not_called()
        self.assertIn("preview", stdout)
        self.assertIn("AGENTS.md", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_workspace_yes_applies_and_registers(self, service_type, _default_paths) -> None:
        service = self._service()
        service_type.return_value = service

        rc, _stdout, _stderr = run_cli_main(
            [
                "agentbot",
                "workspace",
                "--yes",
                "--profile",
                "safe-default",
                "--targets",
                "claude,codex",
                "/repo",
            ]
        )

        self.assertEqual(0, rc)
        service.apply_workspace.assert_called_once_with(
            Path("/repo"),
            profile="safe-default",
            targets=("agents", "claude"),
            register=True,
        )

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_resync_all_is_preview_only_without_yes(self, service_type, _default_paths) -> None:
        service = self._service()
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "resync", "--all"])

        self.assertEqual(0, rc)
        service.resync_workspaces.assert_called_once_with(apply=False, paths=())
        self.assertIn("Workspace resync", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_resync_dry_run_alias_is_preview_only(self, service_type, _default_paths) -> None:
        service = self._service()
        service_type.return_value = service

        rc, _stdout, _stderr = run_cli_main(
            ["agentbot", "resync", "--dry-run", "--all"]
        )

        self.assertEqual(0, rc)
        service.resync_workspaces.assert_called_once_with(apply=False, paths=())

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_resync_specific_paths_does_not_register(self, service_type, _default_paths) -> None:
        service = self._service()
        service_type.return_value = service

        rc, _stdout, _stderr = run_cli_main(
            ["agentbot", "resync", "--yes", "/repo", "/other"]
        )

        self.assertEqual(0, rc)
        service.resync_workspaces.assert_called_once_with(
            apply=True,
            paths=(Path("/repo"), Path("/other")),
        )

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_workspaces_lists_local_records(self, service_type, _default_paths) -> None:
        service = self._service()
        service.list_workspaces.return_value = (
            WorkspaceRecord(
                path="/repo",
                kind="directory",
                policy_mode="managed",
                profile="safe-default",
                targets=("agents",),
                enabled=True,
                last_commit=None,
                last_rendered_at=None,
            ),
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "workspaces"])

        self.assertEqual(0, rc)
        service.list_workspaces.assert_called_once_with()
        self.assertIn("/repo", stdout)
        self.assertIn("safe-default", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_workspaces_paths0_is_nul_delimited(self, service_type, _default_paths) -> None:
        service = self._service()
        service.list_workspaces.return_value = (
            WorkspaceRecord(
                path="/repo",
                kind="directory",
                policy_mode="managed",
                profile="safe-default",
                targets=("agents",),
                enabled=True,
                last_commit=None,
                last_rendered_at=None,
            ),
            WorkspaceRecord(
                path="/second",
                kind="directory",
                policy_mode="managed",
                profile="safe-default",
                targets=("agents",),
                enabled=True,
                last_commit=None,
                last_rendered_at=None,
            ),
        )
        service_type.return_value = service

        rc, stdout, stderr = run_cli_main(["agentbot", "workspaces", "--paths0"])

        self.assertEqual(0, rc)
        self.assertEqual("/repo\0/second\0", stdout)
        self.assertEqual("", stderr)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_workspaces_remove_reports_registry_only_change(
        self, service_type, _default_paths
    ) -> None:
        service = self._service()
        service.remove_workspace.return_value = WorkspaceRecord(
            path="/missing/repo",
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        service_type.return_value = service

        rc, stdout, stderr = run_cli_main(
            ["agentbot", "workspaces", "--remove", "/missing/repo"]
        )

        self.assertEqual(0, rc)
        service.remove_workspace.assert_called_once_with(Path("/missing/repo"))
        self.assertIn("Stopped managing", stdout)
        self.assertIn("No workspace files were changed", stdout)
        self.assertEqual("", stderr)

    def test_resync_requires_all_or_explicit_paths(self) -> None:
        with patch("src.cli.default_paths"), patch("src.cli.Lifecycle"):
            rc, _stdout, stderr = run_cli_main(["agentbot", "resync"])

        self.assertEqual(1, rc)
        self.assertIn("resync requires --all or at least one PATH", stderr)


if __name__ == "__main__":
    unittest.main()
