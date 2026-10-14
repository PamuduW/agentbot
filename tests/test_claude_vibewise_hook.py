import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.claude_vibewise_hook import (
    COMMAND,
    doctor_vibewise_hook,
    hook_destination,
    inspect_vibewise_hook,
    install_vibewise_hook,
)
from tests.support import agentbot_paths

REPO = Path(__file__).resolve().parents[1]
MANIFEST = """version: 1
agents: [claude-code]
scope: global
sources:
  - id: vibe-wise
    repo: nykooi1/vibe-wise
    skills: [learn, reset]
    enabled: {enabled}
"""
BOOST_HOOKS = {
    "PreToolUse": [
        {"hooks": [{"type": "command", "command": "~/.claude/hooks/boost-hook-claude.sh"}]}
    ],
    "SessionStart": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/other.sh"}]}],
}


class VibewiseHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        source = self.root / "global" / "claude" / "hooks" / "vibe-wise-session-start.py"
        source.parent.mkdir(parents=True)
        source.write_text((REPO / "global/claude/hooks/vibe-wise-session-start.py").read_text())
        self.paths = agentbot_paths(self.root)
        self.settings = self.paths.claude_home / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"theme": "dark", "hooks": BOOST_HOOKS}))

    def _manifest(self, enabled: bool) -> None:
        self.paths.skills_sources_file.write_text(MANIFEST.format(enabled=str(enabled).lower()))

    def _settings(self) -> dict:
        return json.loads(self.settings.read_text())

    def test_enabled_source_installs_script_and_one_entry_beside_other_hooks(self) -> None:
        self._manifest(True)
        self.assertEqual(("created", "updated"), install_vibewise_hook(self.paths))
        self.assertEqual(("unchanged", "unchanged"), install_vibewise_hook(self.paths))

        data = self._settings()
        self.assertEqual("dark", data["theme"])
        self.assertEqual(BOOST_HOOKS["PreToolUse"], data["hooks"]["PreToolUse"])
        session = data["hooks"]["SessionStart"]
        self.assertEqual(BOOST_HOOKS["SessionStart"][0], session[0])
        self.assertEqual(1, sum(entry["hooks"][0]["command"] == COMMAND for entry in session))
        self.assertTrue(hook_destination(self.paths).is_file())
        self.assertEqual("ok", inspect_vibewise_hook(self.paths).status_label)
        self.assertEqual([], doctor_vibewise_hook(self.paths))

    def test_disabled_source_removes_only_its_own_entry_and_script(self) -> None:
        self._manifest(True)
        install_vibewise_hook(self.paths)
        self._manifest(False)
        self.assertEqual(("removed", "removed"), install_vibewise_hook(self.paths))

        self.assertEqual({"theme": "dark", "hooks": BOOST_HOOKS}, self._settings())
        self.assertFalse(hook_destination(self.paths).exists())
        self.assertEqual("not wanted", inspect_vibewise_hook(self.paths).status_label)

    def test_a_file_agentbot_did_not_write_is_kept_and_never_wired(self) -> None:
        self._manifest(True)
        destination = hook_destination(self.paths)
        destination.parent.mkdir(parents=True)
        destination.write_text("print('mine')\n")

        self.assertEqual(("preserved", "unchanged"), install_vibewise_hook(self.paths))
        self.assertEqual("print('mine')\n", destination.read_text())
        self.assertNotIn(COMMAND, self.settings.read_text())
        self.assertTrue(doctor_vibewise_hook(self.paths))

    def test_a_stale_script_is_reported_and_refreshed(self) -> None:
        self._manifest(True)
        install_vibewise_hook(self.paths)
        destination = hook_destination(self.paths)
        destination.write_text("# Managed by Agentbot.\nold\n")
        self.assertEqual("script missing or stale", inspect_vibewise_hook(self.paths).status_label)
        self.assertEqual(("updated", "unchanged"), install_vibewise_hook(self.paths))
        self.assertEqual("ok", inspect_vibewise_hook(self.paths).status_label)

    def test_invalid_settings_are_refused_not_rewritten(self) -> None:
        self._manifest(True)
        self.settings.write_text("{not json")
        with self.assertRaises(ValueError):
            install_vibewise_hook(self.paths)
        self.assertEqual("{not json", self.settings.read_text())


class AdaptedScriptTests(unittest.TestCase):
    """The shipped script still behaves as upstream's: silent unless active."""

    SCRIPT = REPO / "global/claude/hooks/vibe-wise-session-start.py"

    def _run(self, cwd: Path, home: Path) -> str:
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": str(cwd)})
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            check=True,
        )
        return result.stdout

    def test_silent_without_notes_and_points_at_the_installed_guide_with_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, home = Path(temporary) / "project", Path(temporary) / "home"
            (project / ".git").mkdir(parents=True)
            self.assertEqual("", self._run(project, home))

            (project / ".vibe-wise").mkdir()
            (project / ".vibe-wise" / "profile.md").write_text("Learning mode: active\n")
            context = json.loads(self._run(project, home))["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn(str(home / ".claude/skills/learn/SKILL.md"), context)

            (project / ".vibe-wise" / "profile.md").write_text("Learning mode: paused\n")
            self.assertEqual("", self._run(project, home))


if __name__ == "__main__":
    unittest.main()
