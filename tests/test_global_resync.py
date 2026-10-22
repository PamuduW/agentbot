import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import agentbot_paths


class GlobalResyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "global").mkdir()
        (self.root / "global" / "AGENTS.md").write_text("# Global Baseline\n", encoding="utf-8")
        source_dir = self.root / "global" / "claude"
        source_dir.mkdir()
        (source_dir / "statusline-command.sh").write_text(
            "#!/bin/bash\n# Managed by Agentbot.\necho statusline\n",
            encoding="utf-8",
        )
        self.codex_home = self.root / "home" / ".codex"
        self.claude_home = self.root / "home" / ".claude"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _paths(self):
        return agentbot_paths(self.root)

    def test_plan_reports_missing_global_outputs_and_statusline(self) -> None:
        from src.render import plan_global_resync_actions

        actions = {
            action.relative_path: action for action in plan_global_resync_actions(self._paths())
        }
        self.assertEqual("create", actions["~/.codex/AGENTS.md"].kind)
        self.assertEqual("create", actions["~/.claude/CLAUDE.md"].kind)
        # Claude Code reads CLAUDE.md, not AGENTS.md, at the user scope.
        self.assertNotIn("~/.claude/AGENTS.md", actions)
        self.assertEqual("create", actions["~/.claude/statusline-command.sh"].kind)

    def test_resync_previews_and_repairs_a_boost_only_codex_policy(self) -> None:
        from src.render import plan_global_resync_actions, resync_global_outputs

        paths = self._paths()
        self.codex_home.mkdir(parents=True)
        agents = self.codex_home / "AGENTS.md"
        agents.write_text("@BOOST.md\n", encoding="utf-8")
        (self.codex_home / "BOOST.md").write_text("# Boost\n", encoding="utf-8")
        saved = self.codex_home / "AGENTS_temp.md"
        saved.write_text("# Saved user edits\n", encoding="utf-8")

        actions = {action.relative_path: action for action in plan_global_resync_actions(paths)}
        self.assertEqual("update", actions["~/.codex/AGENTS.md"].kind)
        self.assertEqual("@BOOST.md\n", agents.read_text(encoding="utf-8"))
        resync_global_outputs(paths, apply=True)
        self.assertIn("# Global Baseline", agents.read_text(encoding="utf-8"))
        self.assertIn(
            "Boost is installed with shell-output filtering off.",
            agents.read_text(encoding="utf-8"),
        )
        self.assertEqual("# Saved user edits\n", saved.read_text(encoding="utf-8"))
        actions = {action.relative_path: action for action in plan_global_resync_actions(paths)}
        self.assertEqual("unchanged", actions["~/.codex/AGENTS.md"].kind)

    def test_codex_guidance_includes_awareness_without_an_import_expander(self) -> None:
        from src.render import resync_global_outputs

        paths = self._paths()
        self.codex_home.mkdir(parents=True)
        awareness = self.codex_home / "BOOST.md"
        awareness.write_text("# Boost\nOld shell-piping instructions.\n", encoding="utf-8")
        resync_global_outputs(paths, apply=True)

        instructions = (self.codex_home / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Boost is installed with shell-output filtering off.", instructions)
        self.assertIn("Never assume a preview is complete.", instructions)
        self.assertNotIn("@BOOST.md", instructions)
        self.assertNotIn("Old shell-piping instructions.", instructions)
        self.assertEqual("# Boost\nOld shell-piping instructions.\n", awareness.read_text())
        actions = {
            action.relative_path: action for action in resync_global_outputs(paths, apply=False)
        }
        self.assertEqual("unchanged", actions["~/.codex/AGENTS.md"].kind)

    def test_resync_apply_writes_global_outputs_and_statusline(self) -> None:
        from src.render import resync_global_outputs

        paths = self._paths()
        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: self.root / "home" / ".agents" / "skills"),
        ):
            (paths.root / "home" / ".agents" / "skills").mkdir(parents=True)
            actions = resync_global_outputs(paths, apply=True)

        kinds = {action.relative_path: action.kind for action in actions}
        self.assertEqual("create", kinds["~/.codex/AGENTS.md"])
        self.assertEqual("create", kinds["~/.claude/statusline-command.sh"])
        self.assertTrue((self.codex_home / "AGENTS.md").is_file())
        self.assertFalse((self.claude_home / "AGENTS.md").exists())
        self.assertTrue((self.claude_home / "CLAUDE.md").is_file())
        self.assertTrue((self.claude_home / "statusline-command.sh").is_file())
        settings = json.loads((self.claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual("~/.claude/statusline-command.sh", settings["statusLine"]["command"])

    def test_service_resync_includes_global_actions(self) -> None:
        from src.lifecycle import Lifecycle
        from src.workspace_service import WorkspaceReport

        service = Lifecycle(self._paths())
        empty = WorkspaceReport(results=())
        with (
            mock.patch.object(service.workspace_service, "resync", return_value=empty),
            mock.patch(
                "src.lifecycle.resync_global_outputs",
                return_value=(),
            ) as global_resync,
        ):
            report = service.resync_workspaces(apply=True, paths=())

        global_resync.assert_called_once_with(service.paths, apply=True)
        self.assertEqual((), report.results)
        self.assertEqual((), report.global_actions)


if __name__ == "__main__":
    unittest.main()
