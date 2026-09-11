import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.support import (
    configure_update,
    isolated_launcher_env,
    run_agentbot_launcher,
    run_cli_main,
)


class CliTests(unittest.TestCase):
    def test_real_launcher_help_resolves_every_metadata_topic_and_alias(self) -> None:
        """Break caught: help accepts only one argv token, hiding nested commands."""
        from src.commands import COMMANDS, command_by_name

        topics = tuple(spec.name for spec in COMMANDS) + tuple(
            alias for spec in COMMANDS for alias in spec.aliases
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            env = isolated_launcher_env(Path(temporary_directory))
            for topic in topics:
                with self.subTest(topic=topic):
                    result = run_agentbot_launcher(("help", *topic.split()), env)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn(
                        f"=== {command_by_name(topic).name} ===",
                        result.stdout,
                    )
                    self.assertEqual("", result.stderr)

    def test_real_launcher_help_rejects_unknown_topic_on_stderr(self) -> None:
        """Break caught: unknown help topics leak to stdout or return a success code."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_agentbot_launcher(
                ("help", "skills", "invented"),
                isolated_launcher_env(Path(temporary_directory)),
            )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("Error: unknown help topic: skills invented", result.stderr)

    def test_parser_accepts_skills_prune_options(self) -> None:
        """Break caught: advertised prune flags fail before the skills handler sees them."""
        from src.cli import build_parser

        args = build_parser().parse_args(["skills", "prune", "--yes", "--include-manual"])

        self.assertEqual("skills", args.command)
        self.assertEqual("prune", args.skills_command)
        self.assertTrue(args.confirm)
        self.assertTrue(args.include_manual)

    def test_parser_accepts_selective_prune_and_candidate_discovery(self) -> None:
        """Break caught: the pruning TUI cannot discover details or select exact skills."""
        from src.cli import build_parser

        selected = build_parser().parse_args(
            ["skills", "prune", "gitlab-ci", "remove-ai-marks", "--yes"]
        )
        discovery = build_parser().parse_args(["skills", "prune", "--candidates0"])

        self.assertEqual(["gitlab-ci", "remove-ai-marks"], selected.prune_names)
        self.assertTrue(selected.confirm)
        self.assertTrue(discovery.candidates0)
        self.assertEqual([], discovery.prune_names)

    def test_parser_accepts_selective_manual_skill_removal(self) -> None:
        """Break caught: the safe selective command is unavailable to scripts and the TUI."""
        from src.cli import build_parser

        args = build_parser().parse_args(
            [
                "skills",
                "remove-manual",
                "gpt-taste",
                "mermaid",
                "--yes",
            ]
        )

        self.assertEqual("remove-manual", args.skills_command)
        self.assertEqual(["gpt-taste", "mermaid"], args.manual_names)
        self.assertTrue(args.confirm)
        self.assertFalse(args.names0)

    def test_parser_accepts_nul_separated_manual_skill_discovery(self) -> None:
        """Break caught: the checkbox menu must scrape a human-formatted table for names."""
        from src.cli import build_parser

        args = build_parser().parse_args(["skills", "remove-manual", "--names0"])

        self.assertTrue(args.names0)
        self.assertEqual([], args.manual_names)

    def test_prune_candidate_discovery_rejects_an_invalid_lock(self) -> None:
        """Break caught: invalid managed state becomes a successful empty picker."""
        from types import SimpleNamespace

        from src.cli import handle_skills_prune
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            paths = AgentbotPaths(
                root=root,
                codex_home=home / ".codex",
                claude_home=home / ".claude",
                cursor_home=home / ".cursor",
                config_home=home / ".config" / "agentbot",
                agents_home=home / ".agents",
            )
            paths.agents_skills_home.mkdir(parents=True)
            paths.skills_sources_file.write_text(
                "version: 1\nagents: [codex]\nscope: global\nsources: []\n",
                encoding="utf-8",
            )
            paths.global_skill_lock.write_text("{invalid", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid global skill lock"):
                handle_skills_prune(
                    SimpleNamespace(paths=paths),
                    names=(),
                    apply=False,
                    include_manual=False,
                    candidates0=True,
                )

    def test_manual_skill_discovery_is_machine_readable_and_protects_graphify(self) -> None:
        """Break caught: the menu sees protected integrations or must parse display text."""
        from types import SimpleNamespace

        from src.cli import handle_skills_remove_manual
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            paths = AgentbotPaths(
                root=root,
                codex_home=home / ".codex",
                claude_home=home / ".claude",
                cursor_home=home / ".cursor",
                config_home=home / ".config" / "agentbot",
                agents_home=home / ".agents",
            )
            paths.agents_skills_home.mkdir(parents=True)
            paths.skills_sources_file.write_text(
                "version: 1\nagents: [codex]\nscope: global\nsources: []\n",
                encoding="utf-8",
            )
            for name in ("gpt-taste", "mermaid", "graphify"):
                skill = paths.agents_skills_home / name
                skill.mkdir()
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            (paths.agents_skills_home / "graphify" / ".graphify_version").write_text(
                "1.2.3\n", encoding="utf-8"
            )

            output = io.StringIO()
            with patch("sys.stdout", output):
                rc = handle_skills_remove_manual(
                    SimpleNamespace(paths=paths),
                    names=(),
                    apply=False,
                    names0=True,
                )

        self.assertEqual(0, rc)
        self.assertEqual("gpt-taste\0mermaid\0", output.getvalue())

    def test_manual_skill_removal_requires_an_explicit_nonempty_selection(self) -> None:
        """Break caught: --yes with no checkbox selection deletes every manual skill."""
        from types import SimpleNamespace

        from src.cli import handle_skills_remove_manual

        with self.assertRaisesRegex(ValueError, "at least one manual skill name"):
            handle_skills_remove_manual(
                SimpleNamespace(paths=MagicMock()),
                names=(),
                apply=True,
                names0=False,
            )

    def test_real_launcher_read_only_process_matrix_uses_an_isolated_home(self) -> None:
        """Break caught: read-only public commands depend on host state or mutate the isolated home."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            isolated_root = Path(temporary_directory)
            env = isolated_launcher_env(isolated_root)
            self.assertEqual([str(isolated_root / "path-bin")], env["PATH"].split(os.pathsep))
            cases = {
                ("status",): (0, "Agentbot › Check Status"),
                ("status", "--json"): (0, None),
                ("doctor",): (1, "Agentbot › Doctor"),
                ("graphify", "status"): (
                    0,
                    "Graphify CLI and Agent Skills integration are not installed.",
                ),
                ("boost", "status"): (0, "Boost CLI is not installed."),
            }
            for args, (expected_returncode, expected_stdout) in cases.items():
                with self.subTest(args=args):
                    result = run_agentbot_launcher(args, env)
                    self.assertEqual(expected_returncode, result.returncode, result.stderr)
                    self.assertEqual("", result.stderr)
                    if expected_stdout is not None:
                        self.assertIn(expected_stdout, result.stdout)
                    else:
                        self.assertEqual(0, json.loads(result.stdout)["installed_skills"])

            self.assertFalse((isolated_root / "home" / ".agents").exists())

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_boot_defers_target_selection_to_the_active_profile(
        self, lifecycle_type, _default_paths
    ) -> None:
        """Break caught: boot hardcoded every target and ignored the profile defaults."""
        from src.workspace_service import WorkspaceResult

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            lifecycle = MagicMock()
            lifecycle.apply_workspace.return_value = WorkspaceResult(
                target,
                "applied",
                (),
                "rendered",
            )
            lifecycle_type.return_value = lifecycle

            rc, _stdout, stderr = run_cli_main(["agentbot", "boot", str(target)])

        self.assertEqual(0, rc, stderr)
        lifecycle.apply_workspace.assert_called_once_with(
            target,
            profile=None,
            targets=None,
            register=True,
        )

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_boot_explicit_selectors_replace_optional_defaults(
        self, lifecycle_type, _default_paths
    ) -> None:
        """Break caught: selecting Cursor accidentally retains the default Claude output."""
        from src.workspace_service import WorkspaceResult

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            lifecycle = MagicMock()
            lifecycle.apply_workspace.return_value = WorkspaceResult(
                target,
                "applied",
                (),
                "rendered",
            )
            lifecycle_type.return_value = lifecycle

            rc, _stdout, stderr = run_cli_main(
                ["agentbot", "boot", "--agents", "--cursor", str(target)]
            )

        self.assertEqual(0, rc, stderr)
        lifecycle.apply_workspace.assert_called_once_with(
            target,
            profile=None,
            targets=("agents", "cursor"),
            register=True,
        )

    def test_caller_path_resolves_relative_paths_against_the_invoking_directory(self) -> None:
        """Break caught: install.sh cd's to the checkout, so "." meant Agentbot itself."""
        from src.cli import caller_path

        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "caller"
            (caller / "nested").mkdir(parents=True)
            with patch.dict(os.environ, {"AGENTBOT_CALLER_PWD": str(caller)}):
                self.assertEqual(caller / ".", caller_path("."))
                self.assertEqual(caller / "nested", caller_path("nested"))
                self.assertEqual(Path("/absolute/repo"), caller_path("/absolute/repo"))

    def test_caller_path_ignores_an_unusable_caller_directory(self) -> None:
        """Break caught: a stale or relative caller value silently redirected the render."""
        from src.cli import caller_path

        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "gone"
            for value in (str(missing), "relative/dir", ""):
                with self.subTest(value=value):
                    with patch.dict(os.environ, {"AGENTBOT_CALLER_PWD": value}):
                        self.assertEqual(Path("nested"), caller_path("nested"))
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Path("nested"), caller_path("nested"))

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_boot_default_path_targets_the_caller_directory(
        self, lifecycle_type, _default_paths
    ) -> None:
        """Break caught: a bare boot rendered into the Agentbot checkout, not the repository."""
        from src.workspace_service import WorkspaceResult

        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "caller"
            caller.mkdir()
            lifecycle = MagicMock()
            lifecycle.apply_workspace.return_value = WorkspaceResult(
                caller, "applied", (), "rendered"
            )
            lifecycle_type.return_value = lifecycle

            with patch.dict(os.environ, {"AGENTBOT_CALLER_PWD": str(caller)}):
                rc, _stdout, stderr = run_cli_main(["agentbot", "boot"])

        self.assertEqual(0, rc, stderr)
        self.assertEqual(caller / ".", lifecycle.apply_workspace.call_args.args[0])

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_workspace_relative_path_targets_the_caller_directory(
        self, lifecycle_type, _default_paths
    ) -> None:
        """Break caught: `agentbot workspace .` previewed the Agentbot checkout."""
        from src.workspace_service import WorkspaceResult

        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "caller"
            (caller / "nested").mkdir(parents=True)
            lifecycle = MagicMock()
            lifecycle.preview_workspace.return_value = WorkspaceResult(
                caller / "nested", "preview", (), "previewed"
            )
            lifecycle_type.return_value = lifecycle

            with patch.dict(os.environ, {"AGENTBOT_CALLER_PWD": str(caller)}):
                rc, _stdout, stderr = run_cli_main(["agentbot", "workspace", "nested"])

        self.assertEqual(0, rc, stderr)
        lifecycle.preview_workspace.assert_called_once_with(
            caller / "nested",
            profile=None,
            targets=None,
        )

    def test_command_specs_cover_parser_and_public_dispatcher(self) -> None:
        import argparse

        from src.cli import build_parser
        from src.commands import COMMANDS, commands_for_surface

        parser = build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        def parser_leaf_paths(
            choices: dict[str, argparse.ArgumentParser], prefix: tuple[str, ...] = ()
        ) -> set[str]:
            paths: set[str] = set()
            for name, child in choices.items():
                child_subparsers = next(
                    (
                        action
                        for action in child._actions
                        if isinstance(action, argparse._SubParsersAction)
                    ),
                    None,
                )
                path = (*prefix, name)
                if child_subparsers is None:
                    paths.add(" ".join(path))
                else:
                    paths.update(parser_leaf_paths(child_subparsers.choices, path))
            return paths

        parser_commands = parser_leaf_paths(subparsers.choices)
        covered_parser_commands = {command for spec in COMMANDS for command in spec.parser_commands}
        self.assertEqual(parser_commands, covered_parser_commands)
        self.assertEqual(
            {
                "status",
                "install",
                "full",
                "update",
                "token",
                "boot",
                "workspace",
                "workspaces",
                "resync",
                "doctor",
                "graphify",
                "boost",
                "cli-config",
                "cursor",
                "vscode",
                "help",
            },
            {spec.name for spec in commands_for_surface("public")},
        )

    def test_every_command_spec_has_a_help_detail(self) -> None:
        from src.commands import COMMANDS

        for spec in COMMANDS:
            with self.subTest(command=spec.name):
                rc, stdout, stderr = run_cli_main(["agentbot", "help", spec.name])
                self.assertEqual(0, rc, stderr)
                self.assertIn(f"=== {spec.name} ===", stdout)
                for command_option in spec.options:
                    self.assertIn(command_option.usage, stdout)

    def test_selective_manual_removal_is_documented_as_a_mutating_command(self) -> None:
        """Break caught: the new destructive path is absent from Agentbot's safety library."""
        from src.commands import command_by_name

        spec = command_by_name("skills remove-manual")

        self.assertEqual("mutating", spec.behavior)
        self.assertIn("--yes", spec.usage)
        self.assertIn("selected", spec.effects.lower())

    def test_help_aliases_resolve_to_canonical_commands(self) -> None:
        from src.commands import COMMANDS

        aliases = {
            alias: spec.name
            for spec in COMMANDS
            for alias in spec.aliases
        }
        self.assertEqual({"upgrade": "update", "skills upgrade": "skills update"}, aliases)
        for alias, canonical in aliases.items():
            with self.subTest(alias=alias):
                rc, stdout, stderr = run_cli_main(["agentbot", "help", *alias.split()])
                self.assertEqual(0, rc, stderr)
                self.assertIn(f"=== {canonical} ===", stdout)

    @patch("src.cli.handle_skills_prune")
    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_skills_commands_render_failures_to_stderr_with_one_exit_contract(
        self, lifecycle_type, _default_paths, prune_handler
    ) -> None:
        """Break caught: one skills path formats an exception differently from its siblings."""
        service = MagicMock()
        lifecycle_type.return_value = service
        cases = (
            (["agentbot", "skills", "install"], service.install_skills),
            (["agentbot", "skills", "update"], service.update_skills),
            (["agentbot", "skills", "prune"], prune_handler),
        )
        for argv, failing_boundary in cases:
            with self.subTest(argv=argv):
                failure = OSError("isolated skills failure")
                failing_boundary.side_effect = failure
                rc, stdout, stderr = run_cli_main(argv)
                self.assertEqual(1, rc)
                self.assertEqual("", stdout)
                self.assertEqual("  Error: isolated skills failure\n", stderr)
                failing_boundary.reset_mock()

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_skills_programming_errors_propagate(self, lifecycle_type, _default_paths) -> None:
        """Break caught: a programmer defect is misreported as an expected user failure."""
        service = MagicMock()
        lifecycle_type.return_value = service

        for failure in (TypeError("programming defect"), AssertionError("broken invariant")):
            with self.subTest(error=type(failure).__name__):
                service.list_skills.side_effect = failure
                with self.assertRaisesRegex(type(failure), str(failure)):
                    run_cli_main(["agentbot", "skills", "list"])

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_skills_install_refresh_failure_has_no_success_report(
        self, lifecycle_type, _default_paths
    ) -> None:
        """Break caught: install reports success before its required output refresh completes."""
        service = MagicMock()
        service.install_skills.return_value = []
        service.refresh_outputs.side_effect = OSError("refresh failed")
        lifecycle_type.return_value = service

        rc, stdout, stderr = run_cli_main(["agentbot", "skills", "install"])

        self.assertEqual(1, rc)
        self.assertEqual("", stdout)
        self.assertEqual("  Error: refresh failed\n", stderr)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_skills_install_refreshes_agent_outputs(self, service_type, _default_paths) -> None:
        from src.models import OutputRefreshOutcome

        service = MagicMock()
        service.install_skills.return_value = []
        service.refresh_outputs.return_value = OutputRefreshOutcome(3, 2, 1)
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "skills", "install"])

        self.assertEqual(0, rc)
        service.refresh_outputs.assert_called_once_with()
        self.assertIn("3 linked, 2 updated, 1 skipped", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_install_skill_failure_is_a_clean_cli_error(
        self, service_type, _default_paths
    ) -> None:
        from src.skills_installer import SkillsInstallError

        service = MagicMock()
        service.install.side_effect = SkillsInstallError("failed to install source 'test': offline")
        service_type.return_value = service

        rc, _stdout, stderr = run_cli_main(["agentbot", "install"])

        self.assertEqual(1, rc)
        self.assertIn("Error: failed to install source 'test': offline", stderr)

    def test_bootstrap_header_uses_install_breadcrumb(self) -> None:
        from pathlib import Path

        from src.boost import BoostStatus
        from src.cli import run_agentbot_install
        from src.graphify import GraphifyStatus
        from src.models import DiagnosticsSnapshot, InstallOutcome, OutputRefreshOutcome
        from src.paths import AgentbotPaths

        paths = AgentbotPaths(
            Path("/repo"),
            Path("/codex"),
            Path("/claude"),
            Path("/cursor"),
            config_home=Path("/config/agentbot"),
            agents_home=Path("/agents"),
        )
        outcome = InstallOutcome(
            skills=(),
            graphify=GraphifyStatus(
                "not-installed",
                None,
                None,
                Path("/agents/skills/graphify/SKILL.md"),
                None,
                "missing",
                "missing",
                "Graphify is not installed.",
            ),
            boost=BoostStatus(
                "not-installed",
                None,
                None,
                Path("/.boost/config.toml"),
                False,
                False,
                "missing",
                "missing",
                "missing",
                "absent",
                "Boost is not installed.",
            ),
            outputs=OutputRefreshOutcome(3, 2, 1),
            diagnostics=DiagnosticsSnapshot((), 0, True, True, False, 0, 0, 0, 0, "missing", ()),
        )
        lifecycle = MagicMock()
        lifecycle.install.return_value = outcome
        output = io.StringIO()

        with patch("sys.stdout", output):
            rc = run_agentbot_install(lifecycle, paths)

        self.assertEqual(0, rc)
        self.assertIn("Agentbot › Install Agentbot", output.getvalue())
        self.assertIn("3 linked, 2 updated, 1 skipped", output.getvalue())

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_update_prints_reconciliation_result_report(self, service_type, _default_paths) -> None:
        from pathlib import Path

        from src.skill_reconcile import ReconcileResult

        service = MagicMock()
        configure_update(
            service,
            ReconcileResult(
                "applied",
                (Path("AGENTS.md"),),
                ("removed-skill",),
                ("added-skill",),
                updated_skills=("updated-skill",),
            ),
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "update", "--yes"])

        self.assertEqual(0, rc)
        self.assertIn("Reconciliation report", stdout)
        self.assertIn("added-skill", stdout)
        self.assertIn("removed-skill", stdout)
        self.assertIn("updated-skill", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_update_returns_failure_for_global_output_conflict(
        self, service_type, _default_paths
    ) -> None:
        from src.skill_reconcile import ReconcileResult
        from src.workspace_render import RenderAction
        from src.workspace_service import WorkspaceReport

        service = MagicMock()
        configure_update(
            service,
            ReconcileResult(
                "applied",
                (),
                (),
                (),
                workspace_report=WorkspaceReport(
                    results=(),
                    global_actions=(
                        RenderAction(
                            "global/AGENTS.md",
                            "conflict",
                            None,
                            "missing global baseline",
                        ),
                    ),
                ),
            ),
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "update", "--yes"])

        self.assertEqual(1, rc)
        self.assertIn("conflict", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_update_accepts_non_conflicting_global_output_actions(
        self, service_type, _default_paths
    ) -> None:
        from src.skill_reconcile import ReconcileResult
        from src.workspace_render import RenderAction
        from src.workspace_service import WorkspaceReport

        actions = tuple(
            RenderAction(f"global/{kind}", kind, None, kind)
            for kind in ("create", "update", "unchanged")
        )
        service = MagicMock()
        configure_update(
            service,
            ReconcileResult(
                "applied",
                (),
                (),
                (),
                workspace_report=WorkspaceReport(results=(), global_actions=actions),
            ),
        )
        service_type.return_value = service

        rc, _stdout, _stderr = run_cli_main(["agentbot", "update", "--yes"])

        self.assertEqual(0, rc)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_update_returns_failure_for_broken_graphify_setup(
        self, service_type, _default_paths
    ) -> None:
        from src.skill_reconcile import ReconcileResult

        service = MagicMock()
        configure_update(
            service, ReconcileResult("failed", (), (), (), message="Graphify: skill setup failed")
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "update", "--yes"])

        self.assertEqual(1, rc)
        self.assertIn("Graphify: skill setup failed", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_upgrade_is_update_alias_and_prints_skill_delta(
        self, service_type, _default_paths
    ) -> None:
        from src.skill_reconcile import ReconcileResult

        service = MagicMock()
        configure_update(
            service,
            ReconcileResult(
                "applied", (), ("removed-skill",), (), updated_skills=("updated-skill",)
            ),
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "upgrade", "--yes"])

        self.assertEqual(0, rc)
        self.assertIn("Agentbot › Upgrade", stdout)
        self.assertIn("updated-skill", stdout)
        self.assertIn("removed-skill", stdout)
        service.apply_update.assert_called_once()
        self.assertEqual(
            (service.plan_update.return_value,), service.apply_update.call_args.args
        )

    def test_parser_accepts_upgrade_alias(self) -> None:
        from src.cli import build_parser

        args = build_parser().parse_args(["upgrade", "--dry-run"])

        self.assertEqual("upgrade", args.command)
        self.assertTrue(args.dry_run)

    def test_parser_accepts_interactive_update(self) -> None:
        from src.cli import build_parser

        args = build_parser().parse_args(["update", "--interactive"])

        self.assertTrue(args.interactive)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    @patch("src.cli.confirm_update_plan", return_value=True)
    @patch("src.cli.print_update_plan")
    @patch("src.cli.print_update_outcome")
    def test_interactive_update_applies_the_same_confirmed_plan_once(
        self,
        _print_outcome,
        _print_plan,
        _confirm,
        lifecycle_type,
        _default_paths,
    ) -> None:
        from src.models import UpdateOutcome

        lifecycle = MagicMock()
        plan = MagicMock()
        lifecycle.plan_update.return_value = plan
        lifecycle.apply_update.return_value = UpdateOutcome("applied")
        lifecycle_type.return_value = lifecycle

        rc, _stdout, _stderr = run_cli_main(["agentbot", "update", "--interactive"])

        self.assertEqual(0, rc)
        lifecycle.plan_update.assert_called_once_with()
        # The plan is still applied exactly once, and now with the progress
        # hook that reports each phase as it starts.
        lifecycle.apply_update.assert_called_once()
        self.assertEqual((plan,), lifecycle.apply_update.call_args.args)

    def test_parser_accepts_graphify_status_and_setup(self) -> None:
        from src.cli import build_parser

        status = build_parser().parse_args(["graphify", "status"])
        setup = build_parser().parse_args(["graphify", "setup"])

        self.assertEqual("graphify", status.command)
        self.assertEqual("status", status.graphify_command)
        self.assertEqual("setup", setup.graphify_command)

    def test_parser_accepts_boost_status_setup_and_off(self) -> None:
        from src.cli import build_parser

        for command in ("status", "setup", "off"):
            args = build_parser().parse_args(["boost", command])
            self.assertEqual("boost", args.command)
            self.assertEqual(command, args.boost_command)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_graphify_status_is_read_only_and_prints_state(
        self, service_type, _default_paths
    ) -> None:
        from pathlib import Path

        from src.graphify import GraphifyStatus

        service = MagicMock()
        service.graphify_status.return_value = GraphifyStatus(
            "cli-only",
            None,
            None,
            Path("/tmp/.agents/skills/graphify/SKILL.md"),
            None,
            "missing",
            "missing",
            "Graphify CLI is installed; the Agent Skills integration is not set up.",
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "graphify", "status"])

        self.assertEqual(0, rc)
        self.assertIn("Agentbot › Graphify", stdout)
        self.assertIn("cli-only", stdout)
        service.graphify_status.assert_called_once_with()
        service.setup_graphify.assert_not_called()

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_graphify_setup_returns_success_for_ready_state(
        self, service_type, _default_paths
    ) -> None:
        from pathlib import Path

        from src.graphify import GraphifyStatus

        service = MagicMock()
        service.setup_graphify.return_value = GraphifyStatus(
            "ready",
            Path("/usr/local/bin/graphify"),
            "graphify 1.2.3",
            Path("/tmp/.agents/skills/graphify/SKILL.md"),
            "1.2.3",
            "linked",
            "linked",
            "Graphify CLI and Agent Skills integration are ready.",
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "graphify", "setup"])

        self.assertEqual(0, rc)
        self.assertIn("ready", stdout)
        service.setup_graphify.assert_called_once_with()

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_graphify_setup_fails_cleanly_when_cli_is_missing(
        self, service_type, _default_paths
    ) -> None:
        from pathlib import Path

        from src.graphify import GraphifyStatus

        service = MagicMock()
        service.setup_graphify.return_value = GraphifyStatus(
            "not-installed",
            None,
            None,
            Path("/tmp/.agents/skills/graphify/SKILL.md"),
            None,
            "missing",
            "missing",
            "Graphify CLI and Agent Skills integration are not installed. Install it separately, or run: uv tool install graphifyy",
        )
        service_type.return_value = service

        rc, stdout, _stderr = run_cli_main(["agentbot", "graphify", "setup"])

        self.assertEqual(1, rc)
        self.assertIn("uv tool install graphifyy", stdout)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    @patch("src.cli.Diagnostics")
    def test_status_and_update_use_hierarchical_breadcrumbs(
        self, diagnostics_type, service_type, _default_paths
    ) -> None:
        from src.models import DiagnosticsSnapshot
        from src.skill_reconcile import ReconcileResult

        service = MagicMock()
        diagnostics_type.return_value.collect.return_value = DiagnosticsSnapshot(
            installed_skills=("alpha",),
            enabled_sources=1,
            global_agents_exists=True,
            skills_sources_exists=True,
            global_lock_exists=True,
            global_lock_skills=1,
            managed_skill_count=1,
            manual_skill_count=0,
            claude_bridge_links=1,
            claude_statusline_state="ok",
            issues=(),
        )
        configure_update(service, ReconcileResult("preview", (), (), ()))
        service_type.return_value = service

        status_rc, status_stdout, _ = run_cli_main(["agentbot", "status"])
        update_rc, update_stdout, _ = run_cli_main(["agentbot", "update", "--dry-run"])

        self.assertEqual(0, status_rc)
        self.assertEqual(0, update_rc)
        self.assertIn("Agentbot › Check Status", status_stdout)
        self.assertIn("Agentbot › Update", update_stdout)

    def test_status_labels_trial_skills_outside_managed_sources(self) -> None:
        from src.ui import print_status_summary

        output = io.StringIO()
        with patch("sys.stdout", output):
            print_status_summary(
                installed_skills=4,
                global_agents_exists=True,
                skills_sources_exists=True,
                enabled_sources=1,
                global_lock_exists=True,
                global_lock_skills=4,
                claude_bridge_links=4,
                claude_statusline_state="ok",
                manual_skill_count=4,
                doctor_issue_count=4,
            )

        rendered = output.getvalue()
        self.assertIn("4 outside managed sources", rendered)
        self.assertNotIn("outside global lock", rendered)

    def test_install_progress_survives_a_pipe(self) -> None:
        """The stage lines exist because an install was minutes of silence.

        They were emitted only when stdout was a terminal, which switched them
        off for `dotfiles full-update` -- the piped run where the silence was
        longest and the reason the progress was added.

        They carry no cursor control, so a pipe degrades cleanly.
        """
        import io
        from unittest.mock import patch

        from src.ui.install_log import InstallLog

        captured = io.StringIO()
        log = InstallLog()
        with patch("sys.stdout", captured):
            log.stage("Installing skill sources", 0.0)
            log.stage("Refreshing managed outputs", 63.0)
        rendered = captured.getvalue()

        self.assertIn("[STEP] Installing skill sources", rendered)
        # The completion names the stage it closes; it used to read
        # "[OK] took 1m 03s", a duration attached to nothing.
        self.assertIn("[OK] Installing skill sources (1m 03s)", rendered)
        self.assertNotIn("\033", rendered)
        self.assertNotIn("\r", rendered)

    def test_install_can_be_narrowed_to_selected_components(self) -> None:
        """Roadmap 4.1: skills, Graphify and Boost were all-or-nothing.

        A deselected integration is still inspected rather than ignored --
        declining to configure Boost is not a reason to stop reporting what
        Boost is currently doing.
        """
        from src.cli import build_parser
        from src.lifecycle import Lifecycle

        self.assertEqual(("skills", "graphify", "boost"), Lifecycle.SELECTABLE_COMPONENTS)

        args = build_parser().parse_args(["install", "--components", "skills,boost"])
        self.assertEqual("skills,boost", args.components)

        # Omitting the flag must stay indistinguishable from the behaviour
        # before selection existed.
        args = build_parser().parse_args(["install"])
        self.assertEqual("", args.components)

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    @patch("src.cli.Diagnostics")
    def test_status_doctor_renders_one_snapshot(
        self, diagnostics_type, _lifecycle_type, _default_paths
    ) -> None:
        from src.models import DiagnosticsSnapshot, DoctorIssue

        diagnostics = diagnostics_type.return_value
        diagnostics.collect.return_value = DiagnosticsSnapshot(
            (),
            1,
            True,
            True,
            False,
            0,
            0,
            0,
            0,
            "missing",
            (DoctorIssue("error", "global", "baseline is missing"),),
        )

        rc, stdout, _stderr = run_cli_main(["agentbot", "status", "--doctor"])

        self.assertEqual(1, rc)
        diagnostics.collect.assert_called_once_with()
        # The status table carries no section rule of its own: the L10 contract
        # gives a rule to a surface with two or more groups, and this one is a
        # single table under a header that already names it. Assert the rows are
        # there rather than a label that the contract deliberately removed.
        self.assertIn("Installed skills", stdout)
        self.assertIn("Doctor issues", stdout)
        self.assertIn("baseline is missing", stdout)

    def test_table_model_exposes_json_compatible_rows(self) -> None:
        from src.models import Table, TableSection
        from src.ui import table_rows

        table = Table(
            "Status",
            "Agentbot › Check Status",
            (TableSection("Health", (("Agentbot", "current", "ok"),)),),
        )

        self.assertEqual(
            [{"section": "Health", "component": "Agentbot", "detail": "current", "result": "ok"}],
            table_rows(table),
        )

    @patch("src.cli.default_paths")
    @patch("src.cli.Lifecycle")
    def test_update_reconciliation_report_has_one_table_header(
        self, service_type, _default_paths
    ) -> None:
        from pathlib import Path

        from src.skill_reconcile import ReconcileResult

        service = MagicMock()
        configure_update(
            service, ReconcileResult("preview", (Path("AGENTS.md"),), ("added",), ("removed",))
        )
        service_type.return_value = service

        rc, stdout, _ = run_cli_main(["agentbot", "update", "--dry-run"])

        self.assertEqual(0, rc)
        # Counting a padded literal pinned one column split; the guarantee is
        # that the report carries exactly one table header, at any width.
        header_lines = [
            line
            for line in stdout.splitlines()
            if re.match(r"^\s*component\s+\|\s*detail\s+\|\s*result\s*$", line)
        ]
        self.assertEqual(1, len(header_lines))

    def test_reconciliation_report_shows_every_skill_in_each_delta(self) -> None:
        from src.skill_reconcile import ReconcileResult
        from src.ui import print_reconciliation_report

        result = ReconcileResult(
            "applied",
            (),
            tuple(f"removed-skill-{index}" for index in range(1, 8)),
            tuple(f"added-skill-{index}" for index in range(1, 8)),
            updated_skills=tuple(f"updated-skill-{index}" for index in range(1, 8)),
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            print_reconciliation_report(result)

        rendered = output.getvalue()
        for skill in (*result.updated_skills, *result.added_skills, *result.removed_skills):
            self.assertIn(skill, rendered)

    def test_preview_result_uses_informational_color(self) -> None:
        import os

        from src.ui import color_result

        with patch.dict(os.environ, {"AGENTBOT_TUI": "1"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertEqual("\033[36mpreview\033[0m", color_result("preview"))

    def test_shared_palette_hierarchy(self) -> None:
        import os

        from src.ui import print_header, print_section

        output = io.StringIO()
        with patch.dict(os.environ, {"AGENTBOT_TUI": "1"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            with patch("sys.stdout", output):
                print_header("Status", "Agentbot › Status")
                print_section("── Sources ──")

        rendered = output.getvalue()
        self.assertIn("\033[1m\033[38;5;208m=== Status ===\033[0m", rendered)
        self.assertIn("\033[1m\033[33m── Sources ──\033[0m", rendered)

    def test_reports_use_hierarchical_breadcrumbs(self) -> None:
        from src.ui import print_doctor_summary, print_skills_report

        output = io.StringIO()
        with patch("sys.stdout", output):
            print_doctor_summary([])
            print_skills_report([], title="Skills install")

        rendered = output.getvalue()
        self.assertIn("Agentbot › Doctor", rendered)
        self.assertIn("Agentbot › Skills install", rendered)

    def test_doctor_report_wraps_full_issue_details(self) -> None:
        from src.models import DoctorIssue
        from src.ui import print_doctor_summary

        message = (
            "Manual skill 'brainstorming' is available but outside managed sources; "
            "add a manifest source to make it reproducible"
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            print_doctor_summary([DoctorIssue("warning", "reproducibility", message)])

        from src.ui.table import strip_ansi

        rendered = output.getvalue()
        # The guarantee is that the whole message survives, wrapped across as
        # many rows as it needs. Asserting on individual wrapped fragments
        # pinned one column split instead, so a width change broke a test about
        # truncation. Reassemble the detail column and compare the whole thing.
        detail = " ".join(
            strip_ansi(line).split("|")[1].strip()
            for line in rendered.splitlines()
            if line.count("|") >= 1 and "component" not in line and "---" not in line
        )
        self.assertEqual(message, " ".join(detail.split()))
        self.assertNotIn("...", rendered)
        self.assertNotIn("…", rendered)

    def test_python_reports_fit_tui_widths(self) -> None:
        import os

        from src.models import DoctorIssue
        from src.ui import print_doctor_summary, strip_ansi

        issue = DoctorIssue(
            "warning",
            "reproducibility",
            "A deliberately long diagnostic remains width-safe at every supported terminal size",
        )
        for columns in (48, 80, 120):
            output = io.StringIO()
            with patch.dict(
                os.environ,
                {"AGENTBOT_TUI": "1", "AGENTBOT_MENU_COLS": str(columns), "NO_COLOR": "1"},
                clear=False,
            ):
                with patch("sys.stdout", output):
                    print_doctor_summary([issue])
            for line in strip_ansi(output.getvalue()).splitlines():
                self.assertLessEqual(len(line), columns, (columns, line))

    def test_doctor_highlights_manual_skill_names_without_shifting_columns(self) -> None:
        import os

        from src.models import DoctorIssue
        from src.ui import print_doctor_summary, strip_ansi

        issues = [
            DoctorIssue(
                "warning",
                "reproducibility",
                "Manual skill 'writing-clearly-and-concisely' is outside managed sources",
            ),
            DoctorIssue("warning", "token", "saved token path 'example' needs attention"),
        ]
        output = io.StringIO()
        with patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            with patch("sys.stdout", output):
                print_doctor_summary(issues)

        rendered = output.getvalue()
        self.assertIn(
            "\033[1m\033[36mwriting-clearly-and-concisely\033[0m",
            rendered,
        )
        self.assertEqual(1, rendered.count("\033[1m\033[36m"))
        highlighted_line = next(
            line for line in rendered.splitlines() if "writing-clearly-and-concisely" in line
        )
        # The point is that highlighting does not move the columns, so compare
        # against the table's own header rather than hardcoding positions that
        # only describe one column split.
        header_line = next(
            line for line in rendered.splitlines() if "component" in line and "|" in line
        )
        separators = [
            index for index, char in enumerate(strip_ansi(highlighted_line)) if char == "|"
        ]
        header_separators = [
            index for index, char in enumerate(strip_ansi(header_line)) if char == "|"
        ]
        self.assertEqual(header_separators, separators)

        plain_output = io.StringIO()
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            with patch("sys.stdout", plain_output):
                print_doctor_summary(issues)
        self.assertNotIn("\033[", plain_output.getvalue())
        self.assertIn("'writing-clearly-and-concisely'", plain_output.getvalue())

    def test_parser_and_paths_use_agentbot_product_contract(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        from src.cli import build_parser
        from src.paths import AgentbotPaths, default_paths

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "agentbot"
            xdg = Path(temp_dir) / "config"
            with patch.dict(
                os.environ,
                {"AGENTBOT_HOME": str(root), "XDG_CONFIG_HOME": str(xdg)},
                clear=False,
            ):
                args = build_parser().parse_args(["status"])
                paths = default_paths()

        self.assertEqual("agentbot", build_parser().prog)
        self.assertEqual(root, Path(args.root))
        self.assertIsInstance(paths, AgentbotPaths)
        self.assertEqual(root, paths.root)
        self.assertEqual(xdg / "agentbot", paths.config_home)
        self.assertEqual(root / "agentos.yaml", paths.workspace_profiles_file)
        self.assertEqual(xdg / "agentbot" / "workspaces.json", paths.workspace_state_file)


if __name__ == "__main__":
    unittest.main()
