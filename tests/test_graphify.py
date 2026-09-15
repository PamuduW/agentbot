import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class GraphifyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        (self.root / "global").mkdir()
        (self.root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
        self.paths = None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
        }

    def _write_graphify(self, *, version: str = "1.2.3") -> Path:
        executable = self.fake_bin / "graphify"
        executable.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == --version ]]; then printf 'graphify %s\\n' '"
            + version
            + "'; exit 0; fi\n"
            "if [[ \"$1 $2 $3\" == 'install --platform agents' ]]; then\n"
            '  mkdir -p "$HOME/.agents/skills/graphify"\n'
            "  printf '# graphify\\n' >\"$HOME/.agents/skills/graphify/SKILL.md\"\n"
            "  printf '%s\\n' '"
            + version
            + '\' >"$HOME/.agents/skills/graphify/.graphify_version"\n'
            '  printf \'%s\\n\' "$*" >>"$GRAPHIFY_TEST_LOG"\n'
            "  exit 0\n"
            "fi\n"
            "exit 23\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
        return executable

    def _paths(self):
        from src.paths import AgentbotPaths

        return AgentbotPaths(
            self.root,
            self.root / "codex",
            self.root / "claude",
            self.root / "cursor",
            config_home=self.home / ".config" / "agentbot",
            agents_home=self.home / ".agents",
        )

    def test_status_reports_not_installed_without_writing(self) -> None:
        from src.graphify import GraphifyIntegration

        paths = self._paths()
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        with (
            patch("src.graphify.shutil.which", return_value=None),
            patch.dict(os.environ, self._env(), clear=False),
        ):
            status = GraphifyIntegration(paths).status()
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))

        self.assertEqual("not-installed", status.state)
        self.assertEqual(before, after)
        self.assertIsNone(status.cli_version)

    def test_status_reports_cli_only(self) -> None:
        from src.graphify import GraphifyIntegration

        self._write_graphify()
        with patch.dict(os.environ, self._env(), clear=False):
            status = GraphifyIntegration(self._paths()).status()

        self.assertEqual("cli-only", status.state)
        self.assertEqual("graphify 1.2.3", status.cli_version)

    def test_setup_uses_only_generic_agent_skills_command(self) -> None:
        from src.graphify import GraphifyIntegration

        self._write_graphify()
        log = self.root / "graphify.log"
        with patch.dict(
            os.environ,
            {**self._env(), "GRAPHIFY_TEST_LOG": str(log)},
            clear=False,
        ):
            status = GraphifyIntegration(self._paths()).setup()

        self.assertEqual("ready", status.state)
        self.assertEqual("install --platform agents", log.read_text(encoding="utf-8").strip())
        self.assertTrue((self.home / ".agents/skills/graphify/SKILL.md").is_file())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".cursor").exists())

    def test_status_reports_stale_skill_version(self) -> None:
        from src.graphify import GraphifyIntegration

        self._write_graphify(version="2.0.0")
        skill_dir = self.home / ".agents/skills/graphify"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# graphify\n", encoding="utf-8")
        (skill_dir / ".graphify_version").write_text("1.0.0\n", encoding="utf-8")
        with patch.dict(os.environ, self._env(), clear=False):
            status = GraphifyIntegration(self._paths()).status()

        self.assertEqual("stale", status.state)
        self.assertEqual("1.0.0", status.skill_version)

    def test_status_preserves_and_reports_conflicting_assistant_target(self) -> None:
        from src.graphify import GraphifyIntegration

        self._write_graphify()
        skill_dir = self.home / ".agents/skills/graphify"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# graphify\n", encoding="utf-8")
        (skill_dir / ".graphify_version").write_text("1.2.3\n", encoding="utf-8")
        claude_target = self.root / "claude/skills/graphify"
        claude_target.mkdir(parents=True)
        (claude_target / "SKILL.md").write_text("# user-owned\n", encoding="utf-8")
        with patch.dict(os.environ, self._env(), clear=False):
            status = GraphifyIntegration(self._paths()).status()

        self.assertEqual("conflict", status.state)
        self.assertEqual("conflict", status.claude_state)
        self.assertEqual("# user-owned\n", (claude_target / "SKILL.md").read_text(encoding="utf-8"))

    def test_setup_fails_with_actionable_message_when_cli_is_absent(self) -> None:
        from src.graphify import GraphifyIntegration

        with (
            patch("src.graphify.shutil.which", return_value=None),
            patch.dict(os.environ, self._env(), clear=False),
        ):
            status = GraphifyIntegration(self._paths()).setup()

        self.assertEqual("not-installed", status.state)
        self.assertIn("uv tool install graphifyy", status.message)

    def test_setup_uses_bounded_timeout_and_short_diagnostic(self) -> None:
        from src.command_runner import CommandResult
        from src.graphify import GraphifyIntegration, GraphifyStatus

        runner = MagicMock()
        runner.run.return_value = CommandResult(
            1,
            stderr="\n".join(f"graphify failure line {index}" for index in range(40)),
        )
        integration = GraphifyIntegration(self._paths(), runner=runner)
        current = GraphifyStatus(
            "cli-only",
            Path("/usr/bin/graphify"),
            "graphify 1.0.0",
            integration.skill_path,
            None,
            "missing",
            "missing",
            "Graphify CLI is installed.",
        )

        with (
            patch.object(integration, "status", return_value=current),
            patch.dict(
                os.environ,
                {"AGENTBOT_GRAPHIFY_TIMEOUT_SECONDS": "17"},
                clear=False,
            ),
        ):
            status = integration.setup()

        self.assertEqual("broken", status.state)
        self.assertLessEqual(len(status.message), 240 + len("Graphify skill setup failed: "))
        self.assertNotIn("graphify failure line 0", status.message)
        self.assertEqual(17, runner.run.call_args.kwargs["timeout_seconds"])

    def test_lifecycle_setup_refreshes_existing_agent_outputs(self) -> None:
        from src.lifecycle import Lifecycle

        self._write_graphify()
        log = self.root / "graphify-service.log"
        service = Lifecycle(self._paths())
        with (
            patch.dict(
                os.environ,
                {**self._env(), "GRAPHIFY_TEST_LOG": str(log)},
                clear=False,
            ),
            patch.object(service, "refresh_outputs") as refresh,
        ):
            status = service.setup_graphify()

        self.assertEqual("ready", status.state)
        refresh.assert_called_once_with()

    def test_lifecycle_sync_enables_graphify_when_cli_exists(self) -> None:
        from src.lifecycle import Lifecycle

        self._write_graphify()
        log = self.root / "graphify-sync.log"
        service = Lifecycle(self._paths())
        with (
            patch.dict(
                os.environ,
                {**self._env(), "GRAPHIFY_TEST_LOG": str(log)},
                clear=False,
            ),
            patch.object(service, "refresh_outputs") as refresh,
        ):
            status = service.sync_graphify_if_cli_available()

        self.assertEqual("ready", status.state)
        self.assertEqual("install --platform agents", log.read_text(encoding="utf-8").strip())
        refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
