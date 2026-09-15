import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


class DoctorSeverityTests(unittest.TestCase):
    def test_installed_boost_with_unsafe_config_is_an_error(self) -> None:
        from src.boost import BoostStatus
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "global").mkdir()
            (root / "global/AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config_home=root / ".config" / "agentbot",
                agents_home=root / ".agents",
            )
            status = BoostStatus(
                "unsafe-config",
                root / "boost",
                "boost v0.12.6",
                root / ".boost/config.toml",
                False,
                False,
                "ready",
                "ready",
                "ready",
                "absent",
                "Boost privacy or update pinning is not safely configured.",
            )
            with patch("src.diagnostics.BoostIntegration.status", return_value=status):
                issues = Diagnostics(paths).doctor_issues()

            self.assertTrue(
                any(issue.level == "error" and issue.scope == "boost" for issue in issues)
            )

    def test_missing_boost_does_not_create_a_doctor_issue(self) -> None:
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "global").mkdir()
            (root / "global/AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config_home=root / ".config" / "agentbot",
                agents_home=root / ".agents",
            )
            # `_find_cli` falls back to PATH, which is the developer's real PATH
            # here -- without this the result depends on whether the machine
            # running the suite happens to have Boost installed.
            with patch("src.boost.shutil.which", return_value=None):
                issues = Diagnostics(paths).doctor_issues()
            self.assertFalse(any(issue.scope == "boost" for issue in issues))

    def test_unsafe_saved_token_is_actionable_warning_without_value_leak(self) -> None:
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            config = root / "config" / "agentbot"
            config.mkdir(parents=True)
            token_file = config / "github.env"
            token_file.write_text("GITHUB_TOKEN=short\n", encoding="utf-8")
            token_file.chmod(0o600)
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config,
                agents_home=root / ".agents",
            )
            issues = Diagnostics(paths).doctor_issues()
            self.assertTrue(
                any(issue.level == "warning" and issue.scope == "token" for issue in issues)
            )
            self.assertFalse(any("short" in issue.message for issue in issues))

    def test_stateless_installation_token_is_healthy(self) -> None:
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            config = root / "config" / "agentbot"
            config.mkdir(parents=True)
            token_file = config / "github.env"
            token = f"ghs_12345_{'a' * 250}.{'b' * 200}.{'c' * 55}-x"
            token_file.write_text(f"GITHUB_TOKEN={token}\n", encoding="utf-8")
            token_file.chmod(0o600)
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config,
                agents_home=root / ".agents",
            )

            issues = Diagnostics(paths).doctor_issues()

            self.assertFalse(any(issue.scope == "token" for issue in issues))

    def test_warning_only_doctor_is_success(self) -> None:
        from src.models import DoctorIssue
        from src.ui import print_doctor_summary

        with redirect_stdout(io.StringIO()):
            rc = print_doctor_summary(
                [DoctorIssue(level="warning", scope="skills", message="manual skill")]
            )
        self.assertEqual(0, rc)

    def test_errors_fail_even_with_warnings(self) -> None:
        from src.models import DoctorIssue
        from src.ui import print_doctor_summary

        with redirect_stdout(io.StringIO()):
            rc = print_doctor_summary(
                [
                    DoctorIssue(level="warning", scope="skills", message="manual skill"),
                    DoctorIssue(level="error", scope="global", message="missing baseline"),
                ]
            )
        self.assertEqual(1, rc)

    def test_official_graphify_skill_uses_graphify_scope_instead_of_manual_warning(self) -> None:
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            graphify = home / ".agents" / "skills" / "graphify"
            graphify.mkdir(parents=True)
            (graphify / "SKILL.md").write_text("# graphify\n", encoding="utf-8")
            (graphify / ".graphify_version").write_text("1.2.3\n", encoding="utf-8")
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config_home=home / ".config" / "agentbot",
                agents_home=home / ".agents",
            )

            with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                issues = Diagnostics(paths).doctor_issues()

            self.assertTrue(any(issue.scope == "graphify" for issue in issues))
            self.assertFalse(any("Manual skill 'graphify'" in issue.message for issue in issues))

    def test_unstamped_graphify_directory_keeps_manual_skill_warning(self) -> None:
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            manual = home / ".agents" / "skills" / "graphify"
            manual.mkdir(parents=True)
            (manual / "SKILL.md").write_text("# user skill\n", encoding="utf-8")
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config_home=home / ".config" / "agentbot",
                agents_home=home / ".agents",
            )

            with patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                issues = Diagnostics(paths).doctor_issues()

            manual_messages = [
                issue.message for issue in issues if "Manual skill 'graphify'" in issue.message
            ]
            self.assertTrue(manual_messages)
            self.assertTrue(
                all("outside managed sources" in message for message in manual_messages)
            )

    def test_broken_official_graphify_state_is_an_error(self) -> None:
        from src.diagnostics import Diagnostics
        from src.graphify import GraphifyStatus
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config_home=home / ".config" / "agentbot",
                agents_home=home / ".agents",
            )
            service = Diagnostics(paths)
            graphify = home / ".agents/skills/graphify"
            graphify.mkdir(parents=True)
            (graphify / "SKILL.md").write_text("# graphify\n", encoding="utf-8")
            (graphify / ".graphify_version").write_text("1.2.3\n", encoding="utf-8")
            status = GraphifyStatus(
                "broken",
                None,
                None,
                graphify / "SKILL.md",
                None,
                "missing",
                "missing",
                "Graphify skill setup failed: subprocess failed",
            )
            with (
                patch.dict(os.environ, {"HOME": str(home)}, clear=False),
                patch.object(service, "graphify_status", return_value=status),
            ):
                issues = service.doctor_issues()

            self.assertTrue(
                any(issue.level == "error" and issue.scope == "graphify" for issue in issues)
            )

    def test_legacy_cursor_lock_does_not_override_the_universal_skill_store(self) -> None:
        """Cursor and Codex now consume the shared global skill store."""
        from src.diagnostics import Diagnostics
        from src.paths import AgentbotPaths

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            (root / "global").mkdir()
            (root / "global" / "AGENTS.md").write_text("# baseline\n", encoding="utf-8")
            agents = home / ".agents"
            agents.mkdir(parents=True)
            cursor_lock = agents / "cursor-skills-lock.json"
            global_lock = agents / ".skill-lock.json"
            cursor_lock.write_text(
                '{"skills":{"other":{"source":"owner/repo","updatedAt":"2026-08-27T08:00:00Z"}}}\n',
                encoding="utf-8",
            )
            global_lock.write_text(
                '{"skills":{"alpha":{"source":"owner/repo","updatedAt":"2026-08-26T08:00:00Z"}}}\n',
                encoding="utf-8",
            )
            paths = AgentbotPaths(
                root,
                root / "codex",
                root / "claude",
                root / "cursor",
                config_home=root / ".config" / "agentbot",
                agents_home=agents,
            )

            issues = Diagnostics(paths).doctor_issues()
            self.assertEqual([], [issue for issue in issues if issue.scope == "skills-cursor"])


if __name__ == "__main__":
    unittest.main()
