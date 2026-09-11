import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import agentbot_paths


class LifecycleTests(unittest.TestCase):
    def test_one_injected_runner_reaches_lifecycle_installer_and_workspace(self) -> None:
        """Break caught: a nested module silently creates or patches its own command runner."""
        from src.command_runner import CommandResult
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.workspace_service import WorkspaceReport

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / "skills.sources.yaml").write_text(
                "version: 1\nagents: [codex]\nscope: global\nsources: []\n",
                encoding="utf-8",
            )
            (root / "agentos.yaml").write_text(
                "version: 1\nactive_profile: safe\nprofiles:\n"
                "  safe:\n    description: test\n"
                "    default_targets: [agents]\n"
                "    allowed_targets: [agents]\n"
                "    allow_community_skill_scripts: false\n",
                encoding="utf-8",
            )
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            (root / "base").mkdir()
            (root / "base" / "AGENTS.md").write_text(
                (Path(__file__).parents[1] / "base" / "AGENTS.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            paths = agentbot_paths(root)
            runner = mock.Mock()

            def run(argv, **_kwargs):
                if argv[:4] == ["git", "-C", str(workspace), "rev-parse"]:
                    return CommandResult(1)
                if argv[:3] == ["git", "-C", str(root)]:
                    return CommandResult(0, stdout="head123\n")
                return CommandResult(0, stdout="updated\n")

            runner.run.side_effect = run
            graphify = mock.Mock()
            graphify.status.return_value = GraphifyStatus(
                "not-installed",
                None,
                None,
                root / "agents/skills/graphify/SKILL.md",
                None,
                "missing",
                "missing",
                "not installed",
            )
            lifecycle = Lifecycle(
                paths,
                graphify=graphify,
                command_runner=runner,
                workspace_preview=lambda: WorkspaceReport(()),
            )

            lifecycle.update_skills()
            lifecycle.workspace_service.preview(
                workspace,
                profile_name=None,
                targets=("agents",),
            )
            plan = lifecycle.plan_update()

            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertTrue(any(command[:4] == ["npx", "--yes", "skills", "update"] for command in commands))
            self.assertTrue(any(command[:3] == ["git", "-C", str(workspace)] for command in commands))
            self.assertEqual("head123", plan.snapshot.repository_head)

    def test_install_reports_each_stage_as_it_starts(self) -> None:
        """Break caught: install printed nothing until every stage had finished.

        A dozen network clones behind a silent prompt is indistinguishable from
        a hang, which is exactly what an unattended bootstrap looked like.
        `install` passes these results straight through, so the stand-ins only
        need to be distinguishable.
        """
        from src.lifecycle import Lifecycle

        with tempfile.TemporaryDirectory() as temporary:
            paths = agentbot_paths(Path(temporary))
            announced: list[str] = []
            sentinel = object()

            lifecycle = Lifecycle(
                paths,
                diagnostics=mock.Mock(collect=mock.Mock(return_value=sentinel)),
                graphify=mock.Mock(refresh_if_enabled=mock.Mock(return_value=sentinel)),
                boost=mock.Mock(setup_if_cli_available=mock.Mock(return_value=sentinel)),
                install_skills=lambda _paths: [],
                refresh_outputs=lambda: sentinel,
            )
            lifecycle.install(progress=lambda message, _elapsed: announced.append(message))

        self.assertEqual(
            [
                "Installing skill sources",
                "Refreshing Graphify integration",
                "Configuring Boost integration",
                "Refreshing managed outputs",
                # The editor and CLI surfaces, which were a Platform submenu
                # until they became install components.
                "Refreshing VS Code",
                "Refreshing the Cursor statusline",
                "Refreshing CLI configuration",
                "Running diagnostics",
                # Closes off the last stage, so its duration is reported too.
                "Install complete",
            ],
            announced,
        )

    def test_install_orders_each_stage_once_and_returns_typed_outcome(self) -> None:
        from src.boost import BoostStatus
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.models import DiagnosticsSnapshot, OutputRefreshOutcome

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = agentbot_paths(root)
            events: list[str] = []
            graphify_status = GraphifyStatus(
                "not-installed",
                None,
                None,
                root / "agents/skills/graphify/SKILL.md",
                None,
                "missing",
                "missing",
                "Graphify CLI and Agent Skills integration are not installed.",
            )
            diagnostics_snapshot = DiagnosticsSnapshot(
                installed_skills=(),
                enabled_sources=0,
                global_agents_exists=False,
                skills_sources_exists=False,
                global_lock_exists=False,
                global_lock_skills=0,
                managed_skill_count=0,
                manual_skill_count=0,
                claude_bridge_links=0,
                claude_statusline_state="missing",
                issues=(),
            )
            boost_status = BoostStatus(
                "not-installed",
                None,
                None,
                root / ".boost/config.toml",
                False,
                False,
                "missing",
                "missing",
                "missing",
                "absent",
                "Boost CLI is not installed.",
            )

            class FakeGraphify:
                def refresh_if_enabled(self):
                    events.append("graphify")
                    return graphify_status

            class FakeDiagnostics:
                def collect(self):
                    events.append("diagnostics")
                    return diagnostics_snapshot

            class FakeBoost:
                def setup_if_cli_available(self):
                    events.append("boost")
                    return boost_status

            def install_skills(_paths):
                events.append("skills")
                return []

            def refresh_outputs():
                events.append("outputs")
                return OutputRefreshOutcome(0, 0, 0)

            lifecycle = Lifecycle(
                paths,
                diagnostics=FakeDiagnostics(),
                graphify=FakeGraphify(),
                boost=FakeBoost(),
                install_skills=install_skills,
                refresh_outputs=refresh_outputs,
            )
            outcome = lifecycle.install()

            self.assertEqual(["skills", "graphify", "boost", "outputs", "diagnostics"], events)
            self.assertEqual((), outcome.skills)
            self.assertIs(graphify_status, outcome.graphify)
            self.assertIs(boost_status, outcome.boost)
            self.assertIs(diagnostics_snapshot, outcome.diagnostics)


if __name__ == "__main__":
    unittest.main()
