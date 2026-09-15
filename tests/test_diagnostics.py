import io
import json
import unittest
from unittest import mock

from tests.support import (
    create_skill,
    isolated_agentbot_paths,
    write_global_lock,
    write_skills_manifest,
)


class DiagnosticsTests(unittest.TestCase):
    def test_invalid_mcp_catalog_is_a_doctor_error(self) -> None:
        from src.diagnostics import Diagnostics

        with isolated_agentbot_paths() as (root, paths):
            (root / "mcp").mkdir()
            (root / "mcp" / "catalog.json").write_text(
                '{"version":2,"entries":[]}', encoding="utf-8"
            )
            issues = Diagnostics(paths).doctor_issues()

        self.assertTrue(any(issue.level == "error" and issue.scope == "mcp" for issue in issues))

    def test_disabled_source_skill_is_reported_as_orphaned_and_prunable(self) -> None:
        """Break caught: Doctor calls a lock-pinned orphan a manual skill."""
        from src.diagnostics import Diagnostics

        with isolated_agentbot_paths() as (root, paths):
            write_skills_manifest(
                root,
                sources=(
                    "sources:\n"
                    "  - id: retired\n"
                    "    repo: owner/retired\n"
                    "    skills: [leftover]\n"
                    "    enabled: false\n"
                ),
            )
            skill = paths.agents_skills_home / "leftover"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# leftover\n", encoding="utf-8")
            paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
            paths.global_skill_lock.write_text(
                json.dumps({"version": 3, "skills": {"leftover": {"source": "owner/retired"}}}),
                encoding="utf-8",
            )

            snapshot = Diagnostics(paths).collect()

            messages = [issue.message for issue in snapshot.issues]
            self.assertTrue(any("Orphaned skill 'leftover'" in item for item in messages))
            self.assertFalse(any("Manual skill 'leftover'" in item for item in messages))
            self.assertEqual(0, snapshot.manual_skill_count)
            self.assertEqual(1, snapshot.prune_candidate_count)

    def test_status_counts_only_manual_prune_candidates(self) -> None:
        from src.cli import print_status_json
        from src.diagnostics import Diagnostics

        with isolated_agentbot_paths() as (root, paths):
            write_skills_manifest(
                root,
                sources=(
                    "sources:\n"
                    "  - id: owner\n"
                    "    repo: owner/repo\n"
                    "    skills: all\n"
                    "    exclude:\n"
                    "      - dropped\n"
                ),
            )
            for name in ("keeper", "dropped", "leftover", "handmade"):
                create_skill(paths, name)
            write_global_lock(
                paths,
                {
                    "keeper": {"source": "owner/repo"},
                    "dropped": {"source": "owner/repo"},
                    "leftover": {"source": "gone/repo"},
                    "ghost": {"source": "owner/repo"},
                },
            )

            diagnostics = Diagnostics(paths)
            snapshot = diagnostics.collect()
            summary = diagnostics.status_summary()
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                print_status_json(diagnostics)
            payload = json.loads(stdout.getvalue())

            self.assertEqual(1, snapshot.manual_skill_count)
            self.assertEqual(4, snapshot.prune_candidate_count)
            self.assertEqual(1, summary["manual_skill_count"])
            self.assertEqual(4, summary["prune_candidate_count"])
            self.assertEqual(1, payload["manual_skill_count"])
            self.assertEqual(4, payload["prune_candidate_count"])

    def test_collect_reads_shared_general_diagnostics_facts_once(self) -> None:
        from src.claude_statusline import StatuslineState
        from src.diagnostics import Diagnostics
        from src.skills_sources import load_skills_sources

        with isolated_agentbot_paths() as (root, paths):
            write_skills_manifest(
                root,
                sources=(
                    "sources:\n"
                    "  - id: curated\n"
                    "    repo: https://example.invalid/skills.git\n"
                    "    skills: [alpha]\n"
                ),
            )
            statusline = StatuslineState(True, True, True, True, True, True)
            diagnostics = Diagnostics(paths)

            with (
                mock.patch(
                    "src.diagnostics.load_skills_sources", wraps=load_skills_sources
                ) as manifest_load,
                mock.patch(
                    "src.diagnostics.managed_skill_names", return_value={"alpha"}
                ) as managed_names,
                mock.patch(
                    "src.diagnostics.inspect_claude_statusline", return_value=statusline
                ) as statusline_inspect,
                mock.patch(
                    "src.claude_statusline.inspect_claude_statusline",
                    return_value=statusline,
                ) as doctor_statusline_inspect,
                mock.patch.object(
                    diagnostics, "graphify_status", return_value=mock.Mock(state="ready")
                ) as graphify_status,
                mock.patch.object(diagnostics, "skills_doctor_issues", return_value=[]),
            ):
                snapshot = diagnostics.collect()

            self.assertEqual(1, snapshot.enabled_sources)
            self.assertEqual(1, snapshot.managed_skill_count)
            self.assertEqual("ok", snapshot.claude_statusline_state)
            self.assertEqual(1, manifest_load.call_count)
            self.assertEqual(1, managed_names.call_count)
            self.assertEqual(1, statusline_inspect.call_count)
            self.assertEqual(0, doctor_statusline_inspect.call_count)
            self.assertEqual(1, graphify_status.call_count)

    def test_statusline_doctor_uses_an_already_inspected_state(self) -> None:
        from src.claude_statusline import StatuslineState, doctor_claude_statusline

        with isolated_agentbot_paths() as (_, paths):
            state = StatuslineState(
                source_exists=True,
                installed=False,
                in_sync=False,
                managed=False,
                settings_wired=False,
                jq_available=True,
            )
            with mock.patch(
                "src.claude_statusline.inspect_claude_statusline",
                side_effect=AssertionError("state was inspected twice"),
            ):
                issues = doctor_claude_statusline(paths, state=state)

            self.assertEqual(1, len(issues))
            self.assertEqual("claude-statusline", issues[0].scope)
            self.assertIn("not installed", issues[0].message)

    def test_collect_combines_general_and_skills_issues_once(self) -> None:
        from src.diagnostics import Diagnostics
        from src.models import DoctorIssue

        with isolated_agentbot_paths() as (_root, paths):
            diagnostics = Diagnostics(paths)
            general = (DoctorIssue("warning", "token", "unsafe"),)
            skills = (DoctorIssue("error", "skills", "broken"),)
            statusline = mock.Mock(status_label="ready")

            with (
                mock.patch.object(
                    diagnostics, "_doctor_issues", return_value=list(general)
                ) as doctor,
                mock.patch.object(
                    diagnostics, "skills_doctor_issues", return_value=list(skills)
                ) as skills_doctor,
                mock.patch.object(
                    diagnostics, "list_skills", return_value=["alpha", "beta"]
                ) as list_skills,
                mock.patch("src.diagnostics.managed_skill_names", return_value={"alpha"}),
                mock.patch("src.diagnostics.inspect_claude_statusline", return_value=statusline),
            ):
                snapshot = diagnostics.collect()

            self.assertEqual(("alpha", "beta"), snapshot.installed_skills)
            self.assertEqual(general + skills, snapshot.issues)
            self.assertEqual(1, snapshot.managed_skill_count)
            self.assertEqual(0, snapshot.manual_skill_count)
            self.assertEqual("ready", snapshot.claude_statusline_state)
            doctor.assert_called_once()
            skills_doctor.assert_called_once_with()
            list_skills.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
