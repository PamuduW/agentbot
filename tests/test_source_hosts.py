import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class SplitSourceRepoTests(unittest.TestCase):
    def test_bare_owner_name_stays_github(self) -> None:
        from src.skills_sources import split_source_repo

        self.assertEqual(("github.com", "obra/superpowers"), split_source_repo("obra/superpowers"))

    def test_gitlab_host_prefix_allows_nested_groups(self) -> None:
        from src.skills_sources import split_source_repo

        self.assertEqual(
            ("gitlab.com", "gitlab-org/ci-cd/gitlab-ci-skill"),
            split_source_repo("gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill"),
        )

    def test_explicit_github_host_prefix_is_accepted(self) -> None:
        from src.skills_sources import split_source_repo

        self.assertEqual(
            ("github.com", "obra/superpowers"),
            split_source_repo("github.com/obra/superpowers"),
        )

    def test_bare_nested_path_is_rejected(self) -> None:
        """Nested paths are GitLab-only, so they must name the host explicitly."""
        from src.skills_sources import split_source_repo

        self.assertIsNone(split_source_repo("gitlab-org/ci-cd/gitlab-ci-skill"))

    def test_rejects_unusable_values(self) -> None:
        from src.skills_sources import split_source_repo

        for value in (
            "",
            "superpowers",
            "owner/",
            "/name",
            "owner name/repo",
            "gitlab.com/only-one-segment",
            "gitlab.com/group//name",
            "gitlab.com/group/name/",
        ):
            with self.subTest(value=value):
                self.assertIsNone(split_source_repo(value))


class SourceCloneUrlTests(unittest.TestCase):
    def test_github_clone_url(self) -> None:
        from src.skills_sources import source_clone_url

        self.assertEqual(
            "https://github.com/obra/superpowers.git",
            source_clone_url("obra/superpowers"),
        )

    def test_gitlab_clone_url_keeps_nested_path(self) -> None:
        from src.skills_sources import source_clone_url

        self.assertEqual(
            "https://gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill.git",
            source_clone_url("gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill"),
        )

    def test_invalid_source_has_no_clone_url(self) -> None:
        from src.skills_sources import source_clone_url

        self.assertIsNone(source_clone_url("superpowers"))


class SourceTypeTests(unittest.TestCase):
    def test_source_type_per_host(self) -> None:
        from src.skills_sources import source_type

        self.assertEqual("github", source_type("obra/superpowers"))
        self.assertEqual("gitlab", source_type("gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill"))
        self.assertIsNone(source_type("superpowers"))


class CloneCommandTests(unittest.TestCase):
    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/git")
    def test_installer_clones_gitlab_source_from_gitlab_host(self, _mock_which) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import _clone_remote_source

        runner = MagicMock()
        runner.run.return_value = CommandResult(0)
        with tempfile.TemporaryDirectory() as temporary:
            _clone_remote_source(
                "gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill",
                Path(temporary) / "checkout",
                runner=runner,
            )

        self.assertIn(
            "https://gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill.git",
            runner.run.call_args.args[0],
        )

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/git")
    def test_installer_still_clones_bare_source_from_github(self, _mock_which) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import _clone_remote_source

        runner = MagicMock()
        runner.run.return_value = CommandResult(0)
        with tempfile.TemporaryDirectory() as temporary:
            _clone_remote_source("obra/superpowers", Path(temporary) / "checkout", runner=runner)

        self.assertIn("https://github.com/obra/superpowers.git", runner.run.call_args.args[0])

    def test_installer_rejects_unsupported_source(self) -> None:
        from src.skills_installer import _clone_remote_source

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                _clone_remote_source("superpowers", Path(temporary) / "checkout")

    def test_catalog_clone_uses_gitlab_host(self) -> None:
        from src.command_runner import CommandResult
        from src.skill_catalog import _clone_source

        runner = MagicMock()
        runner.run.return_value = CommandResult(0)
        with tempfile.TemporaryDirectory() as temporary:
            _clone_source(
                "gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill",
                Path(temporary) / "checkout",
                runner=runner,
            )

        self.assertIn(
            "https://gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill.git",
            runner.run.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
