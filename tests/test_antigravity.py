import json
import tempfile
import unittest
from pathlib import Path

from tests.support import agentbot_paths


class AntigravityGlobalOutputTests(unittest.TestCase):
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
        self.paths = agentbot_paths(self.root)
        self.paths.agents_skills_home.mkdir(parents=True)
        self.config = self.root / "home" / ".gemini" / "config"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install_agy(self) -> None:
        bin_dir = self.root / "home" / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "agy").write_text("#!/bin/sh\n", encoding="utf-8")

    def _plan(self) -> dict:
        from src.render import plan_global_resync_actions

        return {action.relative_path: action for action in plan_global_resync_actions(self.paths)}

    def test_nothing_is_planned_or_written_without_antigravity(self) -> None:
        from src.render import resync_global_outputs

        self.assertNotIn("~/.gemini/config/AGENTS.md", self._plan())
        resync_global_outputs(self.paths, apply=True)
        self.assertFalse((self.root / "home" / ".gemini").exists())

    def test_installed_cli_gets_rules_and_skill_store(self) -> None:
        from src.render import resync_global_outputs

        self._install_agy()
        plan = self._plan()
        self.assertEqual("create", plan["~/.gemini/config/AGENTS.md"].kind)
        self.assertEqual("create", plan["~/.gemini/config/skills.json"].kind)

        resync_global_outputs(self.paths, apply=True)

        rules = (self.config / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# Global Baseline", rules)
        self.assertEqual(
            {"entries": [{"path": str(self.paths.agents_skills_home)}]},
            json.loads((self.config / "skills.json").read_text(encoding="utf-8")),
        )
        self.assertFalse((self.root / "home" / ".gemini" / "GEMINI.md").exists())
        self.assertFalse((self.root / "home" / ".gemini" / "AGENTS.md").exists())

        plan = self._plan()
        self.assertEqual("unchanged", plan["~/.gemini/config/AGENTS.md"].kind)
        self.assertEqual("unchanged", plan["~/.gemini/config/skills.json"].kind)

    def test_cli_state_folder_counts_as_present(self) -> None:
        (self.root / "home" / ".gemini" / "antigravity-cli").mkdir(parents=True)
        self.assertIn("~/.gemini/config/skills.json", self._plan())

    def test_user_skill_entries_and_keys_are_kept(self) -> None:
        from src.render import resync_global_outputs

        self.config.mkdir(parents=True)
        user = {
            "inherits": [{"path": "/team/skills.json"}],
            "entries": [{"path": "~/personal-skills", "exclude": ["wip-.*"]}],
        }
        (self.config / "skills.json").write_text(json.dumps(user), encoding="utf-8")

        self.assertEqual("update", self._plan()["~/.gemini/config/skills.json"].kind)
        resync_global_outputs(self.paths, apply=True)

        written = json.loads((self.config / "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(user["inherits"], written["inherits"])
        self.assertEqual(
            [*user["entries"], {"path": str(self.paths.agents_skills_home)}],
            written["entries"],
        )

    def test_unreadable_skill_config_is_preserved(self) -> None:
        from src.render import resync_global_outputs

        self.config.mkdir(parents=True)
        (self.config / "skills.json").write_text("{not json", encoding="utf-8")

        self.assertEqual("conflict", self._plan()["~/.gemini/config/skills.json"].kind)
        resync_global_outputs(self.paths, apply=True)
        self.assertEqual("{not json", (self.config / "skills.json").read_text(encoding="utf-8"))

    def test_skill_config_with_unexpected_shape_is_preserved(self) -> None:
        self.config.mkdir(parents=True)
        (self.config / "skills.json").write_text('{"entries": "x"}', encoding="utf-8")
        self.assertEqual("conflict", self._plan()["~/.gemini/config/skills.json"].kind)

    def test_symlinked_skill_config_is_not_followed(self) -> None:
        from src.render import resync_global_outputs

        self.config.mkdir(parents=True)
        target = self.root / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        (self.config / "skills.json").symlink_to(target)

        self.assertEqual("conflict", self._plan()["~/.gemini/config/skills.json"].kind)
        resync_global_outputs(self.paths, apply=True)
        self.assertEqual("{}", target.read_text(encoding="utf-8"))

    def test_user_owned_rules_file_is_preserved(self) -> None:
        from src.render import resync_global_outputs

        self.config.mkdir(parents=True)
        (self.config / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")

        resync_global_outputs(self.paths, apply=True)

        self.assertEqual("# Mine\n", (self.config / "AGENTS.md").read_text(encoding="utf-8"))

    def test_update_transaction_covers_antigravity_outputs(self) -> None:
        from src.lifecycle import Lifecycle
        from src.workspace_service import WorkspaceReport

        plan = type("Plan", (), {"workspace_report": WorkspaceReport(results=())})()
        affected = Lifecycle(self.paths)._update_transaction_paths(plan)  # type: ignore[arg-type]
        self.assertIn(self.config / "AGENTS.md", affected)
        self.assertIn(self.config / "skills.json", affected)

    def test_doctor_is_silent_without_antigravity(self) -> None:
        from src.antigravity import doctor_antigravity

        self.assertEqual([], doctor_antigravity(self.paths))

    def test_doctor_reports_missing_outputs_until_resync(self) -> None:
        from src.antigravity import doctor_antigravity
        from src.render import resync_global_outputs

        self._install_agy()
        issues = doctor_antigravity(self.paths)
        self.assertEqual(2, len(issues))
        self.assertTrue(all(issue.scope == "antigravity" for issue in issues))

        resync_global_outputs(self.paths, apply=True)
        self.assertEqual([], doctor_antigravity(self.paths))

    def test_doctor_reports_unreadable_skill_config(self) -> None:
        from src.antigravity import doctor_antigravity

        self._install_agy()
        self.config.mkdir(parents=True)
        (self.config / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
        (self.config / "skills.json").write_text("{not json", encoding="utf-8")
        messages = [issue.message for issue in doctor_antigravity(self.paths)]
        self.assertEqual(["preserving unreadable ~/.gemini/config/skills.json"], messages)


if __name__ == "__main__":
    unittest.main()
