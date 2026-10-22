import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import agentbot_paths


class SlimBootstrapEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        (self.root / "global").mkdir()
        (self.root / "global" / "AGENTS.md").write_text(
            "# Global Baseline\n\nShared global instructions.\n",
            encoding="utf-8",
        )
        (self.root / "skills.sources.yaml").write_text(
            "version: 1\nagents: [cursor]\nscope: global\nsources: []\n",
            encoding="utf-8",
        )
        (self.root / "skills-lock.json").write_text(
            json.dumps({"skills": {"alpha-skill": {}, "beta-skill": {}}}, indent=2),
            encoding="utf-8",
        )

        agents_home = self.root / "home" / ".agents" / "skills"
        for name in ("alpha-skill", "beta-skill"):
            skill_dir = agents_home / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

        self.codex_home = self.root / "home" / ".codex"
        self.claude_home = self.root / "home" / ".claude"
        self.agents_home = agents_home

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _paths(self):
        return agentbot_paths(self.root)

    def test_render_global_outputs_syncs_codex_skill_links(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        with (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / "skills"
                ),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                ),
            ),
        ):
            render_global_outputs(paths)

        codex_agents = paths.codex_home / "AGENTS.md"
        self.assertTrue(codex_agents.exists())
        self.assertIn("Global Baseline", codex_agents.read_text(encoding="utf-8"))

        codex_skills = paths.codex_home / "skills"
        self.assertTrue((codex_skills / "alpha-skill").is_symlink())
        self.assertTrue((codex_skills / "beta-skill").is_symlink())

    def test_render_global_outputs_includes_boost_awareness_only_while_boost_md_exists(
        self,
    ) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        paths.codex_home.mkdir(parents=True, exist_ok=True)
        boost_md = paths.codex_home / "BOOST.md"
        boost_md.write_text("# Boost\n", encoding="utf-8")

        render_global_outputs(paths)
        agents = paths.codex_home / "AGENTS.md"
        self.assertIn(
            "Boost is installed with shell-output filtering off.",
            agents.read_text(encoding="utf-8"),
        )

        boost_md.unlink()
        render_global_outputs(paths)
        self.assertNotIn(
            "Boost is installed with shell-output filtering off.",
            agents.read_text(encoding="utf-8"),
        )

    def test_install_replaces_the_boost_created_codex_stub_with_the_baseline(self) -> None:
        from src.lifecycle import Lifecycle
        from src.render import GENERATED_AGENTS_HEADER, render_global_outputs

        paths = self._paths()

        def setup_boost():
            paths.codex_home.mkdir(parents=True, exist_ok=True)
            policy = paths.codex_home / "AGENTS.md"
            existing = policy.read_text(encoding="utf-8") if policy.exists() else ""
            if "@BOOST.md" not in existing:
                policy.write_text(existing + "@BOOST.md\n", encoding="utf-8")
            (paths.codex_home / "BOOST.md").write_text(
                "# Boost (Codex CLI)\n# boost-skill-version: v0.16.5\n", encoding="utf-8"
            )

        boost = mock.Mock()
        boost.setup_if_cli_available.side_effect = setup_boost
        lifecycle = Lifecycle(paths, boost=boost, graphify=mock.Mock(), diagnostics=mock.Mock())
        with mock.patch.object(lifecycle, "_read_platform"):
            lifecycle.install(components=("boost",))

        agents = paths.codex_home / "AGENTS.md"
        actual = agents.read_text(encoding="utf-8")
        self.assertTrue(actual.startswith(GENERATED_AGENTS_HEADER))
        self.assertIn("Shared global instructions.", actual)
        self.assertIn("Boost is installed with shell-output filtering off.", actual)
        self.assertFalse((paths.codex_home / "AGENTS_temp.md").exists())
        render_global_outputs(paths)
        self.assertEqual(actual, agents.read_text(encoding="utf-8"))

    def test_boost_import_with_custom_instructions_is_preserved(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        paths.codex_home.mkdir(parents=True)
        agents = paths.codex_home / "AGENTS.md"
        custom = "@BOOST.md\n\n# My rules\nKeep my instructions.\n"
        agents.write_text(custom, encoding="utf-8")
        (paths.codex_home / "BOOST.md").write_text("# Boost\n", encoding="utf-8")
        render_global_outputs(paths)
        self.assertEqual(custom, agents.read_text(encoding="utf-8"))

    def test_boost_import_without_its_target_is_preserved(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        paths.codex_home.mkdir(parents=True)
        agents = paths.codex_home / "AGENTS.md"
        agents.write_text("@BOOST.md\n", encoding="utf-8")
        render_global_outputs(paths)
        self.assertEqual("@BOOST.md\n", agents.read_text(encoding="utf-8"))

    def test_render_global_outputs_syncs_claude_skill_links(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: self.root / "home" / ".agents" / "skills"),
        ):
            render_global_outputs(paths)

        claude_skills = paths.claude_home / "skills"
        self.assertTrue((claude_skills / "alpha-skill").is_symlink())
        self.assertTrue((claude_skills / "beta-skill").is_symlink())

    def test_render_global_outputs_preserves_unowned_instruction_files(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        outputs = (
            paths.codex_home / "AGENTS.md",
            paths.claude_home / "CLAUDE.md",
        )
        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(f"# User-owned {output.name}\n", encoding="utf-8")

        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: self.root / "home" / ".agents" / "skills"),
        ):
            render_global_outputs(paths)

        for output in outputs:
            self.assertEqual(
                f"# User-owned {output.name}\n",
                output.read_text(encoding="utf-8"),
            )
            review = output.with_name(f"{output.stem}_temp{output.suffix}")
            self.assertTrue(review.is_file())
            self.assertIn("sha256=", review.read_text(encoding="utf-8"))

    def test_render_global_outputs_refreshes_untouched_review_template(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        codex_agents = paths.codex_home / "AGENTS.md"
        codex_agents.parent.mkdir(parents=True, exist_ok=True)
        codex_agents.write_text("# User-owned\n", encoding="utf-8")

        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: self.root / "home" / ".agents" / "skills"),
        ):
            render_global_outputs(paths)
            review = codex_agents.with_name("AGENTS_temp.md")
            old_review = review.read_text(encoding="utf-8")
            (self.root / "global" / "AGENTS.md").write_text(
                "# Global Baseline\n\nNew global instructions.\n",
                encoding="utf-8",
            )
            render_global_outputs(paths)

        self.assertNotEqual(old_review, review.read_text(encoding="utf-8"))
        self.assertIn("New global instructions.", review.read_text(encoding="utf-8"))
        self.assertEqual("# User-owned\n", codex_agents.read_text(encoding="utf-8"))

    def test_render_global_outputs_rolls_edited_stale_review_template(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        codex_agents = paths.codex_home / "AGENTS.md"
        codex_agents.parent.mkdir(parents=True, exist_ok=True)
        codex_agents.write_text("# User-owned\n", encoding="utf-8")

        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: self.root / "home" / ".agents" / "skills"),
        ):
            render_global_outputs(paths)
            review = codex_agents.with_name("AGENTS_temp.md")
            review.write_text(
                review.read_text(encoding="utf-8") + "\nMy notes.\n", encoding="utf-8"
            )
            (self.root / "global" / "AGENTS.md").write_text(
                "# Global Baseline\n\nNew global instructions.\n",
                encoding="utf-8",
            )
            render_global_outputs(paths)

        self.assertIn("My notes.", review.read_text(encoding="utf-8"))
        self.assertTrue(codex_agents.with_name("AGENTS_temp_1.md").is_file())

    def test_render_preserves_unmanaged_codex_skill_links(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        codex_skills = paths.codex_home / "skills"
        codex_skills.mkdir(parents=True)
        manual_source = self.root / "manual-skill"
        manual_source.mkdir()
        (manual_source / "SKILL.md").write_text("# manual\n", encoding="utf-8")
        (codex_skills / "manual-skill").symlink_to(manual_source)

        with (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / "skills"
                ),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                ),
            ),
        ):
            render_global_outputs(paths)

        self.assertTrue((codex_skills / "manual-skill").is_symlink())
        self.assertEqual(manual_source.resolve(), (codex_skills / "manual-skill").resolve())

    def test_render_syncs_manual_skills_when_global_lock_is_malformed(self) -> None:
        from src.render import render_global_outputs

        paths = self._paths()
        (self.root / "skills-lock.json").write_text('{"sources": []}', encoding="utf-8")
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True, exist_ok=True)
        global_lock.write_text("not json", encoding="utf-8")
        manual = self.agents_home / "manual-skill"
        manual.mkdir()
        (manual / "SKILL.md").write_text("# manual\n", encoding="utf-8")

        with (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / "skills"
                ),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                ),
            ),
        ):
            render_global_outputs(paths)

        self.assertTrue((paths.codex_home / "AGENTS.md").is_file())
        self.assertTrue((paths.codex_home / "skills" / "manual-skill").is_symlink())

    def test_non_object_lock_roots_do_not_crash_render_doctor_or_status(self) -> None:
        from src.diagnostics import Diagnostics
        from src.render import render_global_outputs

        paths = self._paths()
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True, exist_ok=True)

        for lock_root in ("[]", "null", '"not an object"'):
            with self.subTest(lock_root=lock_root):
                (self.root / "skills-lock.json").write_text(lock_root, encoding="utf-8")
                global_lock.write_text(lock_root, encoding="utf-8")
                with (
                    mock.patch.object(
                        type(paths),
                        "agents_skills_home",
                        new_callable=lambda: property(
                            lambda self: self.root / "home" / ".agents" / "skills"
                        ),
                    ),
                    mock.patch.object(
                        type(paths),
                        "global_skill_lock",
                        new_callable=lambda: property(
                            lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                        ),
                    ),
                ):
                    render_global_outputs(paths)
                    service = Diagnostics(paths)
                    service.doctor_issues()
                    snapshot = service.collect()

                self.assertEqual(-1, snapshot.global_lock_skills)

    def test_doctor_reports_missing_managed_link_and_manual_provenance(self) -> None:
        from src.diagnostics import Diagnostics

        paths = self._paths()
        manual = self.agents_home / "manual-skill"
        manual.mkdir()
        (manual / "SKILL.md").write_text("# manual\n", encoding="utf-8")
        service = Diagnostics(paths)

        with (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / "skills"
                ),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                ),
            ),
        ):
            messages = [issue.message for issue in service.doctor_issues()]

        self.assertTrue(
            any("alpha-skill" in message and "missing" in message for message in messages)
        )
        self.assertTrue(
            any(
                "manual-skill" in message and "outside managed sources" in message
                for message in messages
            )
        )
        self.assertFalse(any("outside the global lock" in message for message in messages))

    def test_doctor_does_not_reclassify_declared_skill_missing_from_lock_as_manual(self) -> None:
        from src.diagnostics import Diagnostics

        paths = self._paths()
        (self.root / "skills.sources.yaml").write_text(
            """\
version: 1
agents: [cursor]
scope: global
sources:
  - id: superpowers
    repo: obra/superpowers
    skills:
      - brainstorming
""",
            encoding="utf-8",
        )
        declared = self.agents_home / "brainstorming"
        declared.mkdir()
        (declared / "SKILL.md").write_text("# brainstorming\n", encoding="utf-8")
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True, exist_ok=True)
        global_lock.write_text(json.dumps({"version": 3, "skills": {}}), encoding="utf-8")
        service = Diagnostics(paths)

        with (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / "skills"
                ),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                ),
            ),
        ):
            messages = [issue.message for issue in service.doctor_issues()]

        self.assertFalse(any("Manual skill 'brainstorming'" in message for message in messages))

    def test_doctor_ignores_stale_global_lock_skills_outside_the_manifest(self) -> None:
        from src.diagnostics import Diagnostics

        paths = self._paths()
        (self.root / "skills-lock.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True, exist_ok=True)
        global_lock.write_text(
            json.dumps({"version": 3, "skills": {"pitstop": {"source": "old/source"}}}),
            encoding="utf-8",
        )
        service = Diagnostics(paths)

        with (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / "skills"
                ),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(
                    lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
                ),
            ),
        ):
            messages = [issue.message for issue in service.doctor_issues()]

        self.assertFalse(any("pitstop" in message and "missing" in message for message in messages))

    def test_slim_doctor_reports_missing_global_agents(self) -> None:
        from src.diagnostics import Diagnostics

        paths = self._paths()
        (paths.global_agents).unlink()
        service = Diagnostics(paths)

        messages = [issue.message for issue in service.doctor_issues()]
        self.assertTrue(any("Missing global baseline" in message for message in messages))

    def test_slim_status_counts_installed_skills(self) -> None:
        from src.diagnostics import Diagnostics

        paths = self._paths()
        service = Diagnostics(paths)

        with mock.patch.object(service, "list_skills", return_value=["alpha-skill", "beta-skill"]):
            snapshot = service.collect()

        self.assertEqual(2, len(snapshot.installed_skills))
        self.assertTrue(snapshot.global_agents_exists)
        self.assertTrue(snapshot.skills_sources_exists)

    def test_status_counts_all_prune_candidates_except_official_graphify(self) -> None:
        from src.diagnostics import Diagnostics

        paths = self._paths()
        for name in ("deslop", "humanizer", "no-ai-slop", "writing-clearly-and-concisely"):
            skill = self.agents_home / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        graphify = self.agents_home / "graphify"
        graphify.mkdir()
        (graphify / "SKILL.md").write_text("# graphify\n", encoding="utf-8")
        (graphify / ".graphify_version").write_text("1.2.3\n", encoding="utf-8")

        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: self.root / "home" / ".agents" / "skills"),
        ):
            snapshot = Diagnostics(paths).collect()

        # Four manual directories plus two stale pins already present in this
        # fixture; the stamped Graphify integration is protected.
        self.assertEqual(6, snapshot.manual_skill_count)
        self.assertEqual(6, snapshot.prune_candidate_count)


if __name__ == "__main__":
    unittest.main()
