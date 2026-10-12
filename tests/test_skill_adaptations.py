"""Adapted first-party skills report when their upstream skill has moved."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import skill_adaptations
from src.paths import AgentbotPaths
from src.skills_sources import validate_skills_sources

GIT = shutil.which("git", path="/usr/bin:/bin") or "/usr/bin/git"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class SkillAdaptationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
                "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.upstream = self.root / "upstream"
        (self.upstream / "skills" / "brainstorming").mkdir(parents=True)
        (self.upstream / "skills" / "brainstorming" / "SKILL.md").write_text("v1\n")
        git(self.upstream, "init", "--quiet", "-b", "main")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "--quiet", "-m", "v1")
        self.base = git(self.upstream, "rev-parse", "HEAD")
        self.tree = git(self.upstream, "rev-parse", "HEAD:skills/brainstorming")
        self.first_party = self.root / "first-party"
        self.first_party.mkdir()
        self._manifest(tree=self.tree)
        self.config = validate_skills_sources(
            {
                "version": 1,
                "agents": ["codex"],
                "scope": "global",
                "sources": [
                    {"id": "superpowers", "repo": "obra/superpowers", "skills": ["writing-plans"]},
                    {"id": "mine", "repo": "PamuduW/agentbot_skills", "skills": "all"},
                ],
            },
            path=self.root / "skills.sources.yaml",
        )
        self.checkouts = {"superpowers": self.upstream, "mine": self.first_party}
        self.paths = AgentbotPaths(
            root=self.root,
            codex_home=self.root / ".codex",
            claude_home=self.root / ".claude",
            cursor_home=self.root / ".cursor",
            config_home=self.root / "config",
            agents_home=self.root / ".agents",
        )

    def _manifest(self, **overrides: str) -> None:
        entry = {
            "skill": "brainstorming",
            "upstream": "obra/superpowers",
            "path": "skills/brainstorming",
            "commit": self.base,
            "tree": self.tree,
            **overrides,
        }
        lines = ["version: 1", "adaptations:", "  - skill: " + entry.pop("skill")]
        lines += [f"    {key}: {value}" for key, value in entry.items()]
        (self.first_party / "adaptations.yaml").write_text("\n".join(lines) + "\n")

    def test_an_unchanged_upstream_is_current(self) -> None:
        (result,) = skill_adaptations.check(self.config, self.checkouts)

        self.assertEqual(("brainstorming", "current"), (result.skill, result.state))

    def test_an_upstream_change_to_the_skill_is_reported_with_the_base_commit(self) -> None:
        (self.upstream / "skills" / "brainstorming" / "SKILL.md").write_text("v2\n")
        git(self.upstream, "commit", "--quiet", "-am", "v2")

        results = skill_adaptations.check(self.config, self.checkouts)
        skill_adaptations.record(self.paths, results)

        self.assertEqual("changed", results[0].state)
        detail, result = skill_adaptations.summary(self.paths)
        self.assertEqual("check", result)
        self.assertEqual(f"brainstorming: superpowers changed since {self.base[:7]}", detail)

    def test_a_change_elsewhere_upstream_does_not_count(self) -> None:
        (self.upstream / "README.md").write_text("unrelated\n")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "--quiet", "-m", "readme")

        (result,) = skill_adaptations.check(self.config, self.checkouts)

        self.assertEqual("current", result.state)

    def test_an_upstream_that_is_not_a_managed_source_is_unknown(self) -> None:
        self._manifest(upstream="someone/else")

        (result,) = skill_adaptations.check(self.config, self.checkouts)

        self.assertEqual(
            ("unknown", "upstream is not a managed source"), (result.state, result.detail)
        )

    def test_a_skill_removed_upstream_is_unknown(self) -> None:
        self._manifest(path="skills/gone")

        (result,) = skill_adaptations.check(self.config, self.checkouts)

        self.assertEqual("unknown", result.state)
        self.assertIn("skills/gone not found upstream", result.detail)

    def test_a_source_without_a_manifest_has_nothing_to_check(self) -> None:
        (self.first_party / "adaptations.yaml").unlink()

        self.assertEqual([], skill_adaptations.check(self.config, self.checkouts))

    def test_status_before_any_update_says_so(self) -> None:
        detail, result = skill_adaptations.summary(self.paths)

        self.assertEqual("info", result)
        self.assertIn("not checked yet", detail)

    def test_status_when_everything_is_current(self) -> None:
        skill_adaptations.record(self.paths, skill_adaptations.check(self.config, self.checkouts))

        self.assertEqual(("1 current with upstream", "ok"), skill_adaptations.summary(self.paths))


class WorkspaceManifestTests(unittest.TestCase):
    def test_the_first_party_manifest_matches_the_adapted_skills_when_checked_out(self) -> None:
        # Inside the workspace, agentbot_skills sits beside agentbot. Its
        # manifest must name only skills that exist there, with a patch each.
        repo = Path(__file__).resolve().parents[2] / "agentbot_skills"
        manifest = repo / skill_adaptations.MANIFEST
        if not manifest.is_file():
            self.skipTest("agentbot_skills is not checked out beside agentbot")
        entries = skill_adaptations._entries(repo)
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(skill=entry["skill"]):
                self.assertTrue((repo / "skills" / str(entry["skill"]) / "SKILL.md").is_file())
                self.assertTrue((repo / str(entry["patch"])).is_file())
                self.assertEqual(40, len(str(entry["commit"])))
                self.assertEqual(40, len(str(entry["tree"])))


if __name__ == "__main__":
    unittest.main()
