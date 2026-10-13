import tempfile
import unittest
from pathlib import Path


class SkillsSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_sources(self, content: str) -> Path:
        path = self.root / "skills.sources.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_load_skills_sources_parses_seed_file(self) -> None:
        from src.skills_sources import load_skills_sources

        repo_root = Path(__file__).resolve().parents[1]
        config = load_skills_sources(repo_root / "skills.sources.yaml")

        self.assertEqual(1, config.version)
        self.assertEqual("global", config.scope)
        self.assertIn("cursor", config.agents)
        self.assertIn("claude-code", config.agents)
        self.assertGreaterEqual(len(config.sources), 1)

        superpowers = next(source for source in config.sources if source.id == "superpowers")
        self.assertEqual("obra/superpowers", superpowers.repo)
        self.assertIn("writing-plans", superpowers.skills)
        # First-party adaptations since 2026-10-12, so superpowers must not
        # also own them.
        self.assertNotIn("brainstorming", superpowers.skills)
        self.assertNotIn("test-driven-development", superpowers.skills)

        self.assertFalse(any(source.id == "graphify" for source in config.sources))

    def test_load_skills_sources_allows_multiple_all_skill_sources(self) -> None:
        from src.skills_sources import validate_skills_sources

        config = validate_skills_sources(
            {
                "version": 1,
                "agents": ["codex"],
                "sources": [
                    {"id": "first", "repo": "owner/first", "skills": "all"},
                    {"id": "second", "repo": "owner/second", "skills": "all"},
                ],
            }
        )

        self.assertEqual(["*"], config.sources[0].skills)
        self.assertEqual(["*"], config.sources[1].skills)

    def test_source_ids_cannot_name_a_path(self) -> None:
        # Source IDs become folder names (setup review's source copies), so an
        # ID that is a path could write outside the folder (pilot 10.7).
        from src.skills_sources import SkillsSourcesError, validate_skills_sources

        for bad in ("../outside", "a/b", "/abs", "..", ".hidden", "with space", "-dash"):
            with self.subTest(id=bad), self.assertRaisesRegex(SkillsSourcesError, "id"):
                validate_skills_sources(
                    {
                        "version": 1,
                        "agents": ["codex"],
                        "sources": [{"id": bad, "repo": "owner/repo", "skills": "all"}],
                    }
                )
        for good in ("superpowers", "devops-anmol", "a.b_c-1"):
            with self.subTest(id=good):
                validate_skills_sources(
                    {
                        "version": 1,
                        "agents": ["codex"],
                        "sources": [{"id": good, "repo": "owner/repo", "skills": "all"}],
                    }
                )

    def test_active_sources_skips_disabled_empty_or_missing_repo(self) -> None:
        from src.skills_sources import load_skills_sources

        path = self._write_sources(
            """\
version: 1
agents:
  - cursor
  - codex
scope: global
sources:
  - id: enabled
    repo: owner/repo
    skills:
      - alpha
  - id: disabled
    repo: owner/disabled
    skills:
      - beta
    enabled: false
  - id: empty-skills
    repo: owner/empty
    skills: []
  - id: missing-repo
    repo: null
    skills:
      - gamma
"""
        )
        config = load_skills_sources(path)
        active = config.active_sources()

        self.assertEqual(1, len(active))
        self.assertEqual("enabled", active[0].id)

    def test_load_skills_sources_expands_all_to_the_skills_cli_wildcard(self) -> None:
        from src.skills_installer import build_add_argv
        from src.skills_sources import load_skills_sources

        path = self._write_sources(
            """\
version: 1
agents:
  - codex
scope: global
sources:
  - id: personal
    repo: owner/personal-skills
    skills: all
"""
        )

        source = load_skills_sources(path).active_sources()[0]

        self.assertEqual(["*"], source.skills)
        self.assertEqual(
            [
                "npx",
                "--yes",
                "skills",
                "add",
                "owner/personal-skills",
                "--full-depth",
                "--skill",
                "*",
                "-a",
                "codex",
                "-g",
                "-y",
            ],
            build_add_argv(source, agents=["codex"]),
        )

    def test_load_skills_sources_rejects_duplicate_active_skill_ownership(self) -> None:
        from src.skills_sources import SkillsSourcesError, load_skills_sources

        path = self._write_sources(
            """\
version: 1
agents:
  - codex
scope: global
sources:
  - id: first
    repo: owner/first
    skills:
      - shared-skill
  - id: second
    repo: owner/second
    skills:
      - shared-skill
"""
        )

        with self.assertRaisesRegex(
            SkillsSourcesError,
            "skill 'shared-skill' is declared by both active sources 'first' and 'second'",
        ):
            load_skills_sources(path)

    def test_load_skills_sources_raises_for_missing_file(self) -> None:
        from src.skills_sources import SkillsSourcesError, load_skills_sources

        with self.assertRaises(SkillsSourcesError):
            load_skills_sources(self.root / "missing.yaml")

    def test_build_add_argv_includes_repo_skills_and_agents(self) -> None:
        from src.skills_installer import build_add_argv
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="superpowers",
            repo="obra/superpowers",
            skills=["brainstorming", "writing-plans"],
        )
        argv = build_add_argv(
            source,
            agents=["cursor", "codex", "claude-code"],
        )

        self.assertEqual(
            [
                "npx",
                "--yes",
                "skills",
                "add",
                "obra/superpowers",
                "--full-depth",
                "--skill",
                "brainstorming",
                "--skill",
                "writing-plans",
                "-a",
                "cursor",
                "-a",
                "codex",
                "-a",
                "claude-code",
                "-g",
                "-y",
            ],
            argv,
        )


if __name__ == "__main__":
    unittest.main()
