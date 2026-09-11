"""Each CLI command handler, exercised on its own.

main() used to be one long if/elif chain, so a branch could only be reached by
driving the whole parser. The dispatch table makes every handler callable with
a CommandContext, which is what these tests do.
"""

from __future__ import annotations

import argparse
import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src import cli
from src.graphify import GraphifyStatus
from src.models import UpdatePlan, UpdateSnapshot
from src.paths import default_paths
from src.skill_reconcile import SkillReconcilePlan
from src.ui.table import strip_ansi
from src.workspace_service import WorkspaceReport, WorkspaceResult


def _graphify(state: str) -> GraphifyStatus:
    return GraphifyStatus(
        state=state,
        cli_path=None,
        cli_version=None,
        skill_path=Path("/tmp/skill"),
        skill_version=None,
        codex_state="absent",
        claude_state="absent",
        message="",
    )


def _workspace_result(status: str) -> WorkspaceResult:
    return WorkspaceResult(
        path=Path("/tmp/ws"), status=status, actions=(), message=""
    )


def _plan(*, additions=(), removals=(), manifest=()) -> UpdatePlan:
    return UpdatePlan(
        snapshot=UpdateSnapshot(
            repository_head="abc123", manifest_sha256="deadbeef", global_lock_sha256=None
        ),
        reconcile=SkillReconcilePlan(
            updates=(),
            wildcard_additions=tuple(additions),
            wildcard_removals=tuple(removals),
            explicit_missing=(),
            explicit_discovered=(),
            manifest_changes=tuple(manifest),
        ),
        graphify_action="skip",
        workspace_report=WorkspaceReport(results=(), global_actions=()),
    )


def _context(**args) -> cli.CommandContext:
    namespace = argparse.Namespace(**args)
    root = Path(tempfile.mkdtemp())
    return cli.CommandContext(
        args=namespace,
        paths=default_paths(root),
        diagnostics=mock.Mock(),
        lifecycle=mock.Mock(),
    )


class CommandTableTests(unittest.TestCase):
    def test_every_parser_command_has_a_handler(self):
        parser = cli.build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        names = set(subparsers[0].choices)
        # `help` is handled before dispatch; the rest must be in the table.
        missing = names - set(cli.COMMAND_HANDLERS) - {"help"}
        self.assertEqual(missing, set(), f"commands without a handler: {sorted(missing)}")

    def test_update_and_upgrade_share_one_handler(self):
        self.assertIs(cli.COMMAND_HANDLERS["update"], cli.COMMAND_HANDLERS["upgrade"])

    def test_archived_commands_are_rejected_before_dispatch(self):
        for command in cli.ARCHIVED_COMMANDS:
            self.assertNotIn(command, cli.COMMAND_HANDLERS)


class HandlerTests(unittest.TestCase):
    def test_global_renders_and_succeeds(self):
        context = _context()
        self.assertEqual(cli._handle_global(context), 0)
        context.lifecycle.render_global.assert_called_once_with()

    def test_install_delegates_to_the_agentbot_install_command(self):
        context = _context()
        with mock.patch.object(cli, "run_agentbot_install", return_value=7) as run:
            self.assertEqual(cli._handle_install(context), 7)
        # No selection means every component, which the install call expresses
        # as None rather than as the full list.
        run.assert_called_once_with(context.lifecycle, context.paths, components=None)

    def test_install_passes_a_component_selection_through(self):
        context = _context(components="skills,boost")
        with mock.patch.object(cli, "run_agentbot_install", return_value=0) as run:
            self.assertEqual(cli._handle_install(context), 0)
        run.assert_called_once_with(
            context.lifecycle, context.paths, components=("skills", "boost")
        )

    def test_install_refuses_an_unknown_component(self):
        context = _context(components="skils")
        with self.assertRaises(SystemExit) as raised:
            cli._handle_install(context)
        self.assertIn("skils", str(raised.exception))

    def test_skills_delegates_with_the_parsed_subcommand(self):
        context = _context(skills_command="list")
        with mock.patch.object(cli, "handle_skills_command", return_value=3) as handle:
            self.assertEqual(cli._handle_skills(context), 3)
        handle.assert_called_once_with(context.lifecycle, "list")

    def test_skills_remove_manual_delegates_exact_selected_names(self):
        context = _context(
            skills_command="remove-manual",
            manual_names=["gpt-taste", "mermaid"],
            confirm=True,
            names0=False,
        )
        with mock.patch.object(cli, "handle_skills_remove_manual", return_value=0) as handle:
            self.assertEqual(cli._handle_skills(context), 0)
        handle.assert_called_once_with(
            context.lifecycle,
            names=("gpt-taste", "mermaid"),
            apply=True,
            names0=False,
        )

    def test_graphify_status_fails_only_when_broken(self):
        for state, expected in (("ready", 0), ("stale", 0), ("broken", 1)):
            context = _context(graphify_command="status")
            context.lifecycle.graphify_status.return_value = _graphify(state)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli._handle_graphify(context), expected, state)

    def test_graphify_setup_accepts_recoverable_states(self):
        for state, expected in (("ready", 0), ("conflict", 0), ("stale", 0), ("broken", 1)):
            context = _context(graphify_command="setup")
            context.lifecycle.setup_graphify.return_value = _graphify(state)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli._handle_graphify(context), expected, state)

    def test_workspaces_remove_takes_priority_over_listing(self):
        context = _context(remove="/tmp/somewhere", paths0=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._handle_workspaces(context), 0)
        context.lifecycle.remove_workspace.assert_called_once()
        context.lifecycle.list_workspaces.assert_not_called()

    def test_workspaces_paths0_emits_nul_separated_paths(self):
        context = _context(remove=None, paths0=True)
        context.lifecycle.list_workspaces.return_value = [
            mock.Mock(path="/one"),
            mock.Mock(path="/two"),
        ]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(cli._handle_workspaces(context), 0)
        self.assertEqual(buffer.getvalue(), "/one\0/two\0")

    def test_resync_rejects_contradictory_flags(self):
        cases = [
            {"yes": True, "dry_run": True, "all": False, "paths": []},
            {"yes": False, "dry_run": False, "all": True, "paths": ["/a"]},
            {"yes": False, "dry_run": False, "all": False, "paths": []},
        ]
        for case in cases:
            with self.assertRaises(ValueError):
                cli._handle_resync(_context(**case))

    def test_resync_reports_failure_when_a_workspace_conflicts(self):
        context = _context(yes=False, dry_run=False, all=True, paths=[])
        context.lifecycle.resync_workspaces.return_value = mock.Mock(
            results=[_workspace_result("conflict")], global_actions=[]
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._handle_resync(context), 1)

    def test_workspace_preview_is_used_without_yes(self):
        context = _context(targets=None, yes=False, path="/tmp/ws", profile=None)
        context.lifecycle.preview_workspace.return_value = _workspace_result("preview")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._handle_workspace(context), 0)
        context.lifecycle.preview_workspace.assert_called_once()
        context.lifecycle.apply_workspace.assert_not_called()

    def test_workspace_conflict_is_a_failure(self):
        context = _context(targets=None, yes=True, path="/tmp/ws", profile=None)
        context.lifecycle.apply_workspace.return_value = _workspace_result("conflict")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._handle_workspace(context), 1)

    def test_update_dry_run_stops_before_applying(self):
        context = _context(command="update", dry_run=True, interactive=False, confirm=False)
        context.lifecycle.plan_update.return_value = _plan()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(cli._handle_update(context), 0)
        context.lifecycle.apply_update.assert_not_called()
        # A dry run has no prompt to close it, so it ended on the plan table.
        self.assertIn("Dry run: nothing was changed.", buffer.getvalue())

    def test_an_applied_update_opens_the_work_under_its_own_heading(self):
        """The [STEP] lines started directly under the plan table.

        The table the operator had just approved and the run that followed read
        as one block. And the result closed on three separate summaries, the
        last of which counted only the resync while reading as the verdict on
        the whole update.
        """
        from src.models import UpdateOutcome

        context = _context(command="update", dry_run=False, interactive=False, confirm=True)
        context.lifecycle.plan_update.return_value = _plan()
        context.lifecycle.apply_update.return_value = UpdateOutcome(
            "applied", "Update plan applied."
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_update(context)
        rendered = strip_ansi(buffer.getvalue())

        self.assertIn("=== Applying update ===", rendered)
        self.assertIn("[Legend]", rendered)
        self.assertIn("=== Update result ===", rendered)
        # One heading for the result, not a second one inside it.
        self.assertNotIn("=== Workspace Resync ===", rendered)
        self.assertEqual(1, len(re.findall(r"\d+ ok|All \d+ component", rendered)))

    def test_update_requires_confirmation_for_source_owned_changes(self):
        context = _context(command="update", dry_run=False, interactive=False, confirm=False)
        context.lifecycle.plan_update.return_value = _plan(additions=("a",))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(cli._handle_update(context), 0)
        self.assertIn("confirmation_required", buffer.getvalue())
        context.lifecycle.apply_update.assert_not_called()

    def test_interactive_update_declining_cancels(self):
        context = _context(command="update", dry_run=False, interactive=True, confirm=False)
        context.lifecycle.plan_update.return_value = _plan()
        with mock.patch.object(cli, "confirm_update_plan", return_value=False):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(cli._handle_update(context), 0)
        self.assertIn("Update cancelled.", buffer.getvalue())
        context.lifecycle.apply_update.assert_not_called()


class WorkspaceTargetParsingTests(unittest.TestCase):
    def test_agents_is_always_first(self):
        self.assertEqual(cli.parse_workspace_targets("claude,agents"), ("agents", "claude"))

    def test_codex_is_an_alias_for_agents(self):
        self.assertEqual(cli.parse_workspace_targets("codex"), ("agents",))

    def test_none_means_the_profile_default(self):
        self.assertIsNone(cli.parse_workspace_targets(None))

    def test_unsupported_and_duplicate_targets_are_rejected(self):
        for value in ("nonsense", "claude,claude", ""):
            with self.assertRaises(ValueError):
                cli.parse_workspace_targets(value)


if __name__ == "__main__":
    unittest.main()
