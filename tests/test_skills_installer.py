import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.support import agentbot_paths


class SkillsInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._write_sources()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _paths(self):
        return agentbot_paths(self.root)

    def _write_sources(self) -> None:
        (self.root / "skills.sources.yaml").write_text(
            """\
version: 1
agents:
  - cursor
  - codex
  - claude-code
scope: global
sources:
  - id: superpowers
    repo: obra/superpowers
    skills:
      - brainstorming
  - id: disabled-pack
    repo: owner/disabled
    skills:
      - ignored
    enabled: false
""",
            encoding="utf-8",
        )

    def _success_result(self, source_id: str, command: list[str]):
        from src.skills_installer import InstallResult

        return InstallResult(
            source_id=source_id,
            command=command,
            returncode=0,
            stdout="",
            stderr="",
        )

    def test_build_add_argv_enables_full_depth_skill_discovery(self) -> None:
        from src.skills_installer import build_add_argv
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="deslop",
            repo="brycewang-stanford/Auto-Empirical-Research-Skills",
            skills=["deslop"],
        )

        command = build_add_argv(source, agents=["codex"])

        self.assertIn("--full-depth", command)

    @patch("src.skills_installer._clone_remote_source")
    @patch("src.skills_installer.run_install_command")
    def test_install_skills_runs_npx_for_active_sources(self, mock_run, mock_clone) -> None:
        from src.skills_installer import build_add_argv, install_skills
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="superpowers",
            repo="obra/superpowers",
            skills=["brainstorming"],
        )
        expected_argv = build_add_argv(
            source,
            agents=["cursor", "codex", "claude-code"],
        )
        mock_run.return_value = self._success_result("superpowers", expected_argv)

        paths = self._paths()
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        with patch.object(
            type(paths),
            "global_skill_lock",
            new_callable=lambda: property(lambda _self: global_lock),
        ):
            results = install_skills(paths)

        self.assertEqual(1, mock_run.call_count)
        command = mock_run.call_args.args[0]
        self.assertEqual("npx", command[0])
        self.assertEqual("--yes", command[1])
        self.assertEqual("skills", command[2])
        self.assertEqual("add", command[3])
        self.assertEqual("superpowers", Path(command[4]).name)
        self.assertIn("--skill", command)
        self.assertIn("brainstorming", command)
        self.assertIn("-a", command)
        self.assertIn("claude-code", command)
        self.assertEqual(1, len(results))
        self.assertEqual("superpowers", results[0].source_id)
        mock_clone.assert_called_once()

    def test_install_source_clones_github_sources_before_invoking_npx(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import InstallResult, install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="superpowers",
            repo="obra/superpowers",
            skills=["brainstorming"],
        )
        runner = MagicMock()
        runner.run.side_effect = [
            CommandResult(0),
            CommandResult(0, stdout="ok"),
        ]
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"

        result = install_source(
            source,
            agents=["codex"],
            global_lock_file=lock_file,
            runner=runner,
        )

        self.assertIsInstance(result, InstallResult)
        self.assertEqual(2, runner.run.call_count)
        clone_command = runner.run.call_args_list[0].args[0]
        install_command = runner.run.call_args_list[1].args[0]
        self.assertEqual(
            ["git", "clone", "--depth=1", "https://github.com/obra/superpowers.git"],
            clone_command[:4],
        )
        self.assertEqual("npx", install_command[0])
        self.assertNotEqual("obra/superpowers", install_command[4])

    @patch("src.skills_installer._clone_remote_source")
    @patch("src.skills_installer.run_install_command")
    def test_install_source_uses_verified_checkout_without_recloning(
        self, mock_run, mock_clone
    ) -> None:
        from src.skills_installer import install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="superpowers",
            repo="obra/superpowers",
            skills=["brainstorming"],
        )
        checkout = self.root / "verified-checkout"
        skill = checkout / "skills" / "brainstorming" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: brainstorming\n---\n", encoding="utf-8")
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        installed = lock_file.parent / "skills" / "brainstorming"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text("# installed\n", encoding="utf-8")
        mock_run.return_value = self._success_result("superpowers", [])

        install_source(
            source,
            agents=["codex"],
            global_lock_file=lock_file,
            checkout=checkout,
        )

        mock_clone.assert_not_called()
        self.assertEqual(str(checkout), mock_run.call_args.args[0][4])

    @patch("src.skills_installer.run_install_command")
    def test_install_source_records_remote_provenance_after_local_checkout(
        self, mock_install
    ) -> None:
        from src.skills_installer import install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="superpowers",
            repo="obra/superpowers",
            skills=["brainstorming"],
        )
        mock_install.return_value = self._success_result(
            "superpowers", ["npx", "skills", "add", "local"]
        )
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        installed_skill = lock_file.parent / "skills" / "brainstorming"
        installed_skill.mkdir(parents=True)
        (installed_skill / "SKILL.md").write_text("# brainstorming\n", encoding="utf-8")

        def clone_with_skill(_repo: str, destination: Path, **_kwargs) -> None:
            skill_dir = destination / "skills" / "brainstorming"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: brainstorming\n---\n", encoding="utf-8")

        with patch("src.skills_installer._clone_remote_source", side_effect=clone_with_skill):
            install_source(source, agents=["codex"], global_lock_file=lock_file)

        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual("obra/superpowers", lock["skills"]["brainstorming"]["source"])
        self.assertEqual("github", lock["skills"]["brainstorming"]["sourceType"])

    def test_lock_records_revision_and_resolved_wildcard_selection(self) -> None:
        from src.skills_installer import _record_checkout_lock
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="wildcard", repo="owner/skills", skills=["*"], exclude=["fixture"]
        )
        checkout = self.root / "checkout"
        for name in ("alpha", "fixture"):
            skill = checkout / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        installed = lock_file.parent / "skills" / "alpha"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text("# alpha\n", encoding="utf-8")

        _record_checkout_lock(source, checkout, lock_file, source_revision="a" * 40)

        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual("a" * 40, lock["skills"]["alpha"]["sourceRevision"])
        self.assertEqual(
            {
                "repo": "owner/skills",
                "revision": "a" * 40,
                "installed": ["alpha"],
                "excluded": ["fixture"],
            },
            lock["agentbotSources"]["wildcard"],
        )
        _record_checkout_lock(source, checkout, lock_file, source_revision="b" * 40)
        updated = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(
            "a" * 40, updated["agentbotPreviousSources"]["wildcard"]["revision"]
        )

    @patch("src.skills_installer.run_install_command")
    def test_install_from_git_checkout_pins_its_commit(self, mock_install) -> None:
        from src.skills_installer import install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(id="source", repo="owner/skills", skills=["alpha"])
        checkout = self.root / "checkout"
        skill = checkout / "skills" / "alpha" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: alpha\n---\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "add", "skills"], check=True)
        subprocess.run(
            [
                "git", "-C", str(checkout), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
            ],
            check=True,
        )
        revision = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
        ).strip()
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        installed = lock_file.parent / "skills" / "alpha"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_text(skill.read_text(), encoding="utf-8")
        mock_install.return_value = self._success_result("source", [])

        install_source(
            source, agents=["codex"], checkout=checkout, global_lock_file=lock_file
        )

        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertEqual(revision, lock["skills"]["alpha"]["sourceRevision"])

    def test_pinned_clone_restores_the_reviewed_commit(self) -> None:
        from src.skills_installer import _clone_pinned_source

        remote = self.root / "remote"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", str(remote)], check=True)
        skill = remote / "skills" / "alpha" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote), "add", "skills"], check=True)
        commit = [
            "git", "-C", str(remote), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
        ]
        subprocess.run(commit, check=True)
        first = subprocess.check_output(
            ["git", "-C", str(remote), "rev-parse", "HEAD"], text=True
        ).strip()
        skill.write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote), "add", "skills"], check=True)
        subprocess.run(commit, check=True)

        restored = self.root / "restored"
        with patch("src.skills_installer.source_clone_url", return_value=str(remote)):
            _clone_pinned_source("owner/skills", first, restored)

        self.assertEqual("first\n", (restored / "skills" / "alpha" / "SKILL.md").read_text())
        self.assertEqual(first, subprocess.check_output(
            ["git", "-C", str(restored), "rev-parse", "HEAD"], text=True
        ).strip())

    @patch("src.skills_installer.run_install_command")
    def test_restore_uses_lock_revision_and_preserves_manual_skill(self, mock_install) -> None:
        from src.skills_installer import restore_skills

        remote = self.root / "remote"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", str(remote)], check=True)
        skill = remote / "skills" / "alpha" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote), "add", "skills"], check=True)
        commit = [
            "git", "-C", str(remote), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
        ]
        subprocess.run(commit, check=True)
        reviewed = subprocess.check_output(
            ["git", "-C", str(remote), "rev-parse", "HEAD"], text=True
        ).strip()
        skill.write_text("second\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote), "add", "skills"], check=True)
        subprocess.run(commit, check=True)
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        manual = lock_file.parent / "skills" / "manual" / "SKILL.md"
        manual.parent.mkdir(parents=True)
        manual.write_text("manual\n", encoding="utf-8")
        lock_file.write_text(json.dumps({
            "version": 3,
            "skills": {"alpha": {"source": "obra/superpowers", "sourceRevision": reviewed}},
            "agentbotSources": {"superpowers": {
                "repo": "obra/superpowers", "revision": reviewed,
                "installed": ["alpha"], "excluded": [],
            }},
        }), encoding="utf-8")

        def install_from_checkout(argv, **_kwargs):
            checkout = Path(argv[4])
            installed = lock_file.parent / "skills" / "alpha" / "SKILL.md"
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_text((checkout / "skills" / "alpha" / "SKILL.md").read_text())
            return self._success_result("superpowers", argv)

        mock_install.side_effect = install_from_checkout
        paths = self._paths()
        with (
            patch.object(type(paths), "global_skill_lock", new_callable=lambda: property(lambda _: lock_file)),
            patch("src.skills_installer.source_clone_url", return_value=str(remote)),
        ):
            snapshots = restore_skills(paths, apply=False)
            self.assertEqual(reviewed, snapshots[0].revision)
            self.assertFalse((lock_file.parent / "skills" / "alpha").exists())
            restore_skills(paths, apply=True)

        self.assertEqual("first\n", (lock_file.parent / "skills" / "alpha" / "SKILL.md").read_text())
        self.assertEqual("manual\n", manual.read_text())

    def test_restore_can_preview_previous_reviewed_selection(self) -> None:
        from src.skills_installer import restore_skills

        paths = self._paths()
        (self.root / "skills.sources.yaml").write_text(
            "version: 1\nagents: [codex]\nscope: global\nsources:\n"
            "  - id: superpowers\n    repo: obra/superpowers\n    skills: [brainstorming]\n"
            "  - id: other\n    repo: owner/other\n    skills: [other]\n",
            encoding="utf-8",
        )
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        lock_file.parent.mkdir(parents=True)
        lock_file.write_text(json.dumps({
            "skills": {"brainstorming": {
                "source": "obra/superpowers", "sourceRevision": "b" * 40
            }, "other": {"source": "owner/other", "sourceRevision": "c" * 40}},
            "agentbotSources": {"superpowers": {
                "repo": "obra/superpowers", "revision": "b" * 40,
                "installed": ["brainstorming"], "excluded": [],
            }, "other": {
                "repo": "owner/other", "revision": "c" * 40,
                "installed": ["other"], "excluded": [],
            }},
            "agentbotPreviousSources": {"superpowers": {
                "repo": "obra/superpowers", "revision": "a" * 40,
                "installed": ["brainstorming"], "excluded": [],
            }},
        }), encoding="utf-8")

        with patch.object(
            type(paths), "global_skill_lock", new_callable=lambda: property(lambda _: lock_file)
        ):
            snapshots = restore_skills(paths, previous=True)
            self.assertEqual(("a" * 40, "c" * 40), tuple(s.revision for s in snapshots))

    def test_skill_update_rechecks_revision_before_install(self) -> None:
        from src.skill_catalog import StaleSourceCatalogError
        from src.skills_installer import apply_skill_update, plan_skill_update

        remote = self.root / "remote"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", str(remote)], check=True)
        skill = remote / "skills" / "brainstorming" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote), "add", "skills"], check=True)
        commit = [
            "git", "-C", str(remote), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
        ]
        subprocess.run(commit, check=True)
        paths = self._paths()

        with patch("src.skill_catalog.source_clone_url", return_value=str(remote)):
            plan = plan_skill_update(paths)
            skill.write_text("second\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(remote), "add", "skills"], check=True)
            subprocess.run(commit, check=True)
            with patch("src.skills_installer.install_skills") as install:
                with self.assertRaises(StaleSourceCatalogError):
                    apply_skill_update(paths, plan)
                install.assert_not_called()

    def test_update_review_id_changes_with_source_revision(self) -> None:
        from src.skill_catalog import SourceCatalog
        from src.skills_installer import SkillUpdatePlan

        first = SkillUpdatePlan(
            (SourceCatalog("source", "owner/skills", "a" * 40, ("alpha",)),),
            (("source", None),), "m" * 64, None,
        )
        second = SkillUpdatePlan(
            (SourceCatalog("source", "owner/skills", "b" * 40, ("alpha",)),),
            (("source", None),), "m" * 64, None,
        )

        self.assertRegex(first.review_id, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first.review_id, second.review_id)

    def test_update_preview_rejects_invalid_recorded_revision_before_network(self) -> None:
        from src.skills_installer import SkillsInstallError, plan_skill_update

        paths = self._paths()
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        lock_file.parent.mkdir(parents=True)
        lock_file.write_text(json.dumps({
            "skills": {},
            "agentbotSources": {"superpowers": {
                "repo": "obra/superpowers", "revision": "invalid-secret-like-value"
            }},
        }), encoding="utf-8")

        with (
            patch.object(type(paths), "global_skill_lock", new_callable=lambda: property(lambda _: lock_file)),
            patch("src.skills_installer.discover_remote_catalogs") as discover,
        ):
            with self.assertRaisesRegex(SkillsInstallError, "invalid recorded revision"):
                plan_skill_update(paths)
            discover.assert_not_called()

    def test_revision_operations_reject_symlinked_lock_before_network(self) -> None:
        from hashlib import sha256

        from src.skills_installer import (
            SkillsInstallError,
            SkillUpdatePlan,
            apply_skill_update,
            plan_skill_update,
            restore_skills,
        )

        paths = self._paths()
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        lock_file.parent.mkdir(parents=True)
        target = self.root / "outside.json"
        target.write_text('{"skills": {}}\n', encoding="utf-8")
        lock_file.symlink_to(target)
        plan = SkillUpdatePlan(
            (),
            (),
            sha256(paths.skills_sources_file.read_bytes()).hexdigest(),
            sha256(target.read_bytes()).hexdigest(),
        )

        with (
            patch.object(type(paths), "global_skill_lock", new_callable=lambda: property(lambda _: lock_file)),
            patch("src.skills_installer.discover_remote_catalogs") as discover,
            patch("src.skills_installer.verified_source_checkouts") as verify,
        ):
            with self.assertRaisesRegex(SkillsInstallError, "symlink"):
                plan_skill_update(paths)
            with self.assertRaisesRegex(SkillsInstallError, "symlink"):
                apply_skill_update(paths, plan)
            with self.assertRaisesRegex(SkillsInstallError, "symlink"):
                restore_skills(paths, apply=True)
            discover.assert_not_called()
            verify.assert_not_called()

    @patch("src.skills_installer.run_install_command")
    def test_install_source_wildcard_does_not_lock_checkout_test_fixtures(
        self, mock_install
    ) -> None:
        from src.skills_installer import install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="wildcard-source",
            repo="owner/skills",
            skills=["*"],
        )
        mock_install.return_value = self._success_result(
            "wildcard-source", ["npx", "skills", "add", "local"]
        )
        lock_file = self.root / "home" / ".agents" / ".skill-lock.json"
        lock_file.parent.mkdir(parents=True)
        lock_file.write_text(
            json.dumps({"version": 3, "skills": {"stale": {"source": "owner/skills"}}}),
            encoding="utf-8",
        )
        installed_skill = lock_file.parent / "skills" / "real-skill"
        installed_skill.mkdir(parents=True)
        (installed_skill / "SKILL.md").write_text("# real skill\n", encoding="utf-8")

        def clone_with_skill_and_fixture(_repo: str, destination: Path, **_kwargs) -> None:
            real_skill = destination / "skills" / "real-skill"
            real_skill.mkdir(parents=True)
            (real_skill / "SKILL.md").write_text("---\nname: real-skill\n---\n", encoding="utf-8")
            fixture_skill = destination / "tests" / "fixtures" / "skills" / "alpha"
            fixture_skill.mkdir(parents=True)
            (fixture_skill / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")

        with patch(
            "src.skills_installer._clone_remote_source", side_effect=clone_with_skill_and_fixture
        ):
            install_source(source, agents=["codex"], global_lock_file=lock_file)

        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        self.assertIn("real-skill", lock["skills"])
        self.assertNotIn("alpha", lock["skills"])
        self.assertNotIn("stale", lock["skills"])

    @patch("src.skills_installer.run_install_command")
    def test_update_skills_runs_npx_update(self, mock_run) -> None:
        from src.skills_installer import build_update_argv, update_skills

        expected_argv = build_update_argv()
        mock_run.return_value = self._success_result("update", expected_argv)

        result = update_skills(self._paths())

        self.assertEqual(1, mock_run.call_count)
        command = mock_run.call_args.args[0]
        self.assertEqual(["npx", "--yes", "skills", "update", "-g", "-y"], command)
        self.assertEqual("update", result.source_id)

    def test_parse_update_output_reports_updated_and_deleted_skills(self) -> None:
        from src.skills_installer import parse_update_output

        output = (
            "\x1b[38;5;145mChecking skills from source: owner/repo\x1b[0m\n"
            "\x1b[38;5;145mWarning:\x1b[0m The following skills from owner/repo appear to have been deleted upstream:\n"
            "  \x1b[38;5;145m•\x1b[0m removed-skill\n"
            "  \x1b[38;5;145m•\x1b[0m another-removed-skill\n"
            "  \x1b[38;5;145m✓\x1b[0m Updated changed-skill\n"
            "\x1b[38;5;145m✓ Updated 1 skill(s)\x1b[0m\n"
        )

        report = parse_update_output(output)

        self.assertEqual(("changed-skill",), report.updated_skills)
        self.assertEqual(
            (("owner/repo", ("another-removed-skill", "removed-skill")),),
            report.deleted_by_source,
        )

    def test_list_installed_skills_reads_agents_home(self) -> None:
        from src.skills_installer import list_installed_skills

        paths = self._paths()
        agents_home = self.root / "agents-home"
        (agents_home / "alpha").mkdir(parents=True)
        (agents_home / "beta").mkdir(parents=True)

        with patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: agents_home),
        ):
            installed = list_installed_skills(paths)

        self.assertEqual(["alpha", "beta"], installed)

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/npx")
    def test_doctor_skills_reports_missing_sources_file(self, _mock_which) -> None:
        from src.skills_installer import doctor_skills

        paths = self._paths()
        (self.root / "skills.sources.yaml").unlink()

        issues = doctor_skills(paths)
        messages = [issue.message for issue in issues]

        self.assertTrue(any("Missing skills sources file" in message for message in messages))

    @patch("src.skills_installer.shutil.which", return_value=None)
    def test_doctor_skills_reports_missing_npx(self, _mock_which) -> None:
        from src.skills_installer import doctor_skills

        issues = doctor_skills(self._paths())
        messages = [issue.message for issue in issues]

        self.assertTrue(any("npx is not available" in message for message in messages))

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/npx")
    def test_doctor_skills_warns_on_unreadable_lockfile(self, _mock_which) -> None:
        from src.skills_installer import doctor_skills

        lock_file = self.root / "skills-lock.json"
        lock_file.write_text(json.dumps({"version": 1}), encoding="utf-8")
        lock_file.chmod(0o000)

        try:
            issues = doctor_skills(self._paths())
        finally:
            lock_file.chmod(0o644)

        messages = [issue.message for issue in issues]
        self.assertTrue(
            any("Unable to read project skills lock file" in message for message in messages)
        )

    def test_run_install_command_uses_command_runner(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import run_install_command

        completed = CommandResult(0, stdout="ok")
        runner = MagicMock()
        runner.run.return_value = completed
        result = run_install_command(
            ["npx", "skills", "update"],
            source_id="update",
            runner=runner,
        )

        runner.run.assert_called_once()
        self.assertEqual(0, result.returncode)
        self.assertEqual("ok", result.stdout)
        self.assertEqual("update", result.source_id)

    def test_tui_install_command_keeps_child_output_hidden(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import run_install_command

        runner = MagicMock()
        runner.run.return_value = CommandResult(0, stdout="ok", stderr="full child log")
        with (
            patch.dict(os.environ, {"AGENTBOT_TUI": "1"}, clear=False),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            result = run_install_command(
                ["npx", "skills", "add", "owner/repo"],
                source_id="repo",
                runner=runner,
            )

        runner.run.assert_called_once()
        self.assertEqual(0, result.returncode)
        self.assertEqual("ok", result.stdout)
        self.assertEqual("", output.getvalue())

    @patch("src.skills_installer._clone_remote_source")
    @patch("src.skills_installer.run_install_command")
    def test_install_source_reports_start_and_finish_progress(self, mock_run, mock_clone) -> None:
        from src.skills_installer import install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(
            id="superpowers",
            repo="obra/superpowers",
            skills=["brainstorming"],
        )
        mock_run.return_value = self._success_result("superpowers", ["npx", "--yes"])
        progress: list[str] = []

        install_source(
            source,
            agents=["codex"],
            global_scope=False,
            progress=progress.append,
        )

        mock_clone.assert_called_once()
        self.assertEqual(2, len(progress))
        self.assertEqual(
            "[STEP] Installing skill source: superpowers (obra/superpowers)",
            progress[0],
        )
        # The completion line carries how long the clone took, so a slow source
        # reads as slow rather than stuck. The duration itself is not pinned.
        self.assertRegex(
            progress[1],
            r"^\[OK\] Skill source installed: superpowers \((\d+s|\d+m \d{2}s)\)$",
        )

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/npx")
    def test_doctor_reports_manifest_skills_missing_from_global_lock(self, _mock_which) -> None:
        from src.skills_installer import doctor_skills

        paths = self._paths()
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True)
        global_lock.write_text(json.dumps({"skills": {}}), encoding="utf-8")

        with patch.object(
            type(paths),
            "global_skill_lock",
            new_callable=lambda: property(
                lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
            ),
        ):
            messages = [issue.message for issue in doctor_skills(paths)]

        self.assertTrue(
            any("brainstorming" in message and "absent" in message for message in messages)
        )

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/npx")
    def test_doctor_ignores_global_lock_skills_not_declared_by_manifest(self, _mock_which) -> None:
        from src.skills_installer import doctor_skills

        paths = self._paths()
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True)
        global_lock.write_text(
            json.dumps({"skills": {"brainstorming": {}, "personal": {}}}), encoding="utf-8"
        )

        with patch.object(
            type(paths),
            "global_skill_lock",
            new_callable=lambda: property(
                lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
            ),
        ):
            messages = [issue.message for issue in doctor_skills(paths)]

        self.assertFalse(
            any("personal" in message and "not declared" in message for message in messages)
        )

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/npx")
    def test_doctor_treats_lock_entries_from_an_all_source_as_managed(self, _mock_which) -> None:
        from src.skills_installer import doctor_skills

        (self.root / "skills.sources.yaml").write_text(
            """\
version: 1
agents:
  - codex
scope: global
sources:
  - id: all-source
    repo: owner/all-skills
    skills: all
""",
            encoding="utf-8",
        )
        paths = self._paths()
        global_lock = self.root / "home" / ".agents" / ".skill-lock.json"
        global_lock.parent.mkdir(parents=True)
        global_lock.write_text(
            json.dumps({"skills": {"upstream-skill": {"source": "owner/all-skills"}}}),
            encoding="utf-8",
        )

        with patch.object(
            type(paths),
            "global_skill_lock",
            new_callable=lambda: property(
                lambda self: self.root / "home" / ".agents" / ".skill-lock.json"
            ),
        ):
            messages = [issue.message for issue in doctor_skills(paths)]

        self.assertFalse(any("'*'" in message for message in messages))
        self.assertFalse(
            any("upstream-skill" in message and "not declared" in message for message in messages)
        )

    def test_run_install_command_uses_a_timeout(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import run_install_command

        completed = CommandResult(0, stdout="ok")
        runner = MagicMock()
        runner.run.return_value = completed
        run_install_command(
            ["npx", "skills", "update"],
            source_id="update",
            runner=runner,
        )

        self.assertIn("timeout_seconds", runner.run.call_args.kwargs)

    def test_run_install_command_allows_a_longer_timeout_for_fresh_machine_installs(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import run_install_command

        completed = CommandResult(0, stdout="ok")
        runner = MagicMock()
        runner.run.return_value = completed
        with patch.dict(os.environ, {"AGENTBOT_NPX_TIMEOUT_SECONDS": "1200"}):
            run_install_command(
                ["npx", "skills", "add"],
                source_id="superpowers",
                runner=runner,
            )

        self.assertEqual(1200, runner.run.call_args.kwargs["timeout_seconds"])

    @patch("src.skills_installer.shutil.which", return_value="/usr/bin/git")
    def test_github_clone_timeout_can_be_overridden(self, _mock_which) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import _clone_remote_source

        runner = MagicMock()
        runner.run.return_value = CommandResult(0)
        with patch.dict(os.environ, {"AGENTBOT_GITHUB_CLONE_TIMEOUT_SECONDS": "600"}):
            _clone_remote_source("owner/repo", self.root / "checkout", runner=runner)

        self.assertEqual(600, runner.run.call_args.kwargs["timeout_seconds"])

    def test_run_install_command_reports_timeouts_with_source_context(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import SkillsInstallError, run_install_command

        runner = MagicMock()
        runner.run.return_value = CommandResult(124, timed_out=True)
        with self.assertRaisesRegex(SkillsInstallError, "superpowers.*timed out"):
            run_install_command(
                ["npx", "skills", "add"],
                source_id="superpowers",
                runner=runner,
            )

    def test_install_command_failure_keeps_only_a_short_diagnostic(self) -> None:
        from src.command_runner import CommandResult
        from src.skills_installer import SkillsInstallError, run_install_command

        child_output = "\n".join(f"child log line {index}" for index in range(20))
        completed = CommandResult(
            1,
            stdout=child_output,
            stderr="npm notice run npx\nnpm notice run 'skills' update -g -y",
        )
        runner = MagicMock()
        runner.run.return_value = completed
        result = run_install_command(
            ["npx", "skills", "add"],
            source_id="superpowers",
            runner=runner,
        )

        self.assertEqual(child_output, result.stdout)

        from src.skills_installer import install_source
        from src.skills_sources import SkillSourceEntry

        source = SkillSourceEntry(id="superpowers", repo="local-source", skills=["skill"])
        with patch("src.skills_installer.run_install_command", return_value=result):
            with self.assertRaises(SkillsInstallError) as raised:
                install_source(source, agents=["codex"])
        self.assertIn("child log line 19", str(raised.exception))
        self.assertNotIn("child log line 0", str(raised.exception))
        self.assertNotIn("npm notice", str(raised.exception))


class InstallProgressTests(unittest.TestCase):
    """Break caught: per-source progress was gated on AGENTBOT_TUI.

    install.sh never sets it, so the one path that runs unattended for minutes
    -- a dozen network clones during bootstrap -- was the one path that
    reported nothing at all.
    """

    def test_progress_is_emitted_when_a_terminal_is_attached(self) -> None:
        from src import skills_installer

        with tempfile.TemporaryDirectory() as temporary:
            paths = agentbot_paths(Path(temporary))
            paths.skills_sources_file.parent.mkdir(parents=True, exist_ok=True)
            paths.skills_sources_file.write_text(
                "version: 1\nagents: [claude-code]\nscope: global\nsources: []\n",
                encoding="utf-8",
            )
            seen = {}

            def capture(config, **kwargs):
                seen["progress"] = kwargs.get("progress")
                return []

            for isatty, tui, expected in (
                (True, None, True),
                (False, None, False),
                (False, "1", True),
            ):
                with self.subTest(isatty=isatty, tui=tui):
                    seen.clear()
                    environment = {"AGENTBOT_TUI": tui} if tui else {}
                    with (
                        patch.object(skills_installer, "install_all", capture),
                        patch.object(
                            skills_installer.sys.stdout,
                            "isatty",
                            # Bound now: a bare lambda would read the loop
                            # variable at call time.
                            (lambda value: lambda: value)(isatty),
                        ),
                        patch.dict(os.environ, environment, clear=not tui),
                    ):
                        skills_installer.install_skills(paths)
                    self.assertEqual(expected, seen["progress"] is not None)

    def test_elapsed_reads_as_time_not_a_raw_count(self) -> None:
        from src.skills_installer import _elapsed

        now = 1000.0
        with patch("src.skills_installer.time.monotonic", return_value=now + 9):
            self.assertEqual("9s", _elapsed(now))
        with patch("src.skills_installer.time.monotonic", return_value=now + 125):
            self.assertEqual("2m 05s", _elapsed(now))


class RenamedSourceMigrationTests(unittest.TestCase):
    """A renamed upstream repository must not orphan the skills it owns.

    Ownership is keyed on the exact `owner/repo` string in each lock entry, so
    changing the manifest alone makes `plan_prune` classify those skills
    `orphaned` and offer them for deletion, and drops them from the managed
    name list in `render.py`.
    """

    OLD = "PamuduW/agent_bootstrap_skills"
    NEW = "PamuduW/agentbot_skills"

    def _lock(self, root: Path, skills: dict) -> Path:
        lock_file = root / ".skill-lock.json"
        lock_file.write_text(json.dumps({"version": 3, "skills": skills}), encoding="utf-8")
        return lock_file

    def test_entries_pinned_to_the_old_name_are_rewritten(self) -> None:
        from src.skills_installer import migrate_renamed_lock_sources

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_file = self._lock(
                root,
                {
                    "co-council": {
                        "source": self.OLD,
                        "sourceType": "github",
                        "sourceUrl": f"https://github.com/{self.OLD}.git",
                    }
                },
            )

            self.assertEqual(("co-council",), migrate_renamed_lock_sources(lock_file))

            entry = json.loads(lock_file.read_text(encoding="utf-8"))["skills"]["co-council"]
            self.assertEqual(self.NEW, entry["source"])
            self.assertEqual(f"https://github.com/{self.NEW}.git", entry["sourceUrl"])

    def test_migration_is_idempotent(self) -> None:
        from src.skills_installer import migrate_renamed_lock_sources

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_file = self._lock(root, {"co-council": {"source": self.OLD}})
            migrate_renamed_lock_sources(lock_file)
            before = lock_file.read_text(encoding="utf-8")

            self.assertEqual((), migrate_renamed_lock_sources(lock_file))
            self.assertEqual(before, lock_file.read_text(encoding="utf-8"))

    def test_unrelated_sources_are_untouched(self) -> None:
        from src.skills_installer import migrate_renamed_lock_sources

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_file = self._lock(
                root,
                {
                    "keep": {"source": "obra/superpowers", "sourceUrl": "https://x/y.git"},
                    "move": {"source": self.OLD},
                },
            )

            self.assertEqual(("move",), migrate_renamed_lock_sources(lock_file))

            skills = json.loads(lock_file.read_text(encoding="utf-8"))["skills"]
            self.assertEqual("obra/superpowers", skills["keep"]["source"])
            self.assertEqual("https://x/y.git", skills["keep"]["sourceUrl"])

    def test_an_unreadable_lock_fails_closed(self) -> None:
        """Break caught: rebuilding a malformed lock would invent ownership."""
        from src.skills_installer import migrate_renamed_lock_sources

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for body in ("{not json", '{"skills": []}', "[]"):
                with self.subTest(body=body):
                    lock_file = root / ".skill-lock.json"
                    lock_file.write_text(body, encoding="utf-8")
                    self.assertEqual((), migrate_renamed_lock_sources(lock_file))
                    self.assertEqual(body, lock_file.read_text(encoding="utf-8"))

    def test_an_absent_lock_is_a_no_op(self) -> None:
        from src.skills_installer import migrate_renamed_lock_sources

        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual((), migrate_renamed_lock_sources(Path(temporary) / "missing.json"))

    def test_the_manifest_no_longer_names_the_old_repository(self) -> None:
        manifest = Path(__file__).resolve().parents[1] / "skills.sources.yaml"
        self.assertNotIn(self.OLD, manifest.read_text(encoding="utf-8"))


class ManagedStateWriteTests(unittest.TestCase):
    """Managed state files are replaced atomically and never written through a link.

    Break caught: the global skill lock was written with a bare `write_text`,
    which opens through a symlink and truncates in place. A link planted at the
    lock path redirected Agentbot's write into an unrelated file, and an
    interrupted write left the lock truncated.
    """

    def _fixture(self, root: Path) -> tuple[Path, Path]:
        agents_home = root / "home" / ".agents"
        (agents_home / "skills" / "demo").mkdir(parents=True)
        (agents_home / "skills" / "demo" / "SKILL.md").write_text(
            "---\nname: demo\n---\n", encoding="utf-8"
        )
        checkout = root / "checkout"
        (checkout / "demo").mkdir(parents=True)
        (checkout / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        return agents_home, checkout

    def _source(self):
        from src.skills_sources import SkillSourceEntry

        return SkillSourceEntry(id="demo-source", repo="owner/repo", skills=["demo"])

    def test_lock_write_refuses_a_symlinked_destination(self) -> None:
        from src.skills_installer import _record_checkout_lock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agents_home, checkout = self._fixture(root)
            victim = root / "victim.json"
            victim.write_text('{"important": "user data"}\n', encoding="utf-8")
            lock_file = agents_home / ".skill-lock.json"
            lock_file.symlink_to(victim)

            with self.assertRaises(ValueError) as raised:
                _record_checkout_lock(self._source(), checkout, lock_file)

            self.assertIn("symlink", str(raised.exception))
            self.assertEqual('{"important": "user data"}\n', victim.read_text(encoding="utf-8"))
            self.assertTrue(lock_file.is_symlink())

    def test_lock_write_replaces_the_file_atomically(self) -> None:
        from src.skills_installer import _record_checkout_lock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agents_home, checkout = self._fixture(root)
            lock_file = agents_home / ".skill-lock.json"
            lock_file.write_text('{"skills": {}}\n', encoding="utf-8")

            _record_checkout_lock(self._source(), checkout, lock_file)

            recorded = json.loads(lock_file.read_text(encoding="utf-8"))
            self.assertIn("demo", recorded["skills"])
            self.assertFalse(
                [entry.name for entry in agents_home.iterdir() if ".agentbot-" in entry.name],
                "atomic write left a temporary file behind",
            )

    def test_a_failed_lock_write_leaves_the_original_intact(self) -> None:
        from src.skills_installer import _record_checkout_lock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agents_home, checkout = self._fixture(root)
            lock_file = agents_home / ".skill-lock.json"
            original = '{"skills": {"kept": {"source": "owner/other"}}}\n'
            lock_file.write_text(original, encoding="utf-8")

            with patch("src.skills_installer.write_text_atomic", side_effect=OSError("no space")):
                with self.assertRaises(OSError):
                    _record_checkout_lock(self._source(), checkout, lock_file)

            self.assertEqual(original, lock_file.read_text(encoding="utf-8"))

    def test_managed_state_is_never_written_with_a_bare_write_text(self) -> None:
        """Break caught: a new writer reintroduces the symlink-following path."""
        source_root = Path(__file__).resolve().parents[1] / "src"
        # Writes into a freshly created private backup directory, which cannot
        # be a caller-controlled path.
        allowed = {("skill_reconcile.py", '(backup / "paths.tsv").write_text')}
        offenders = []
        for module in sorted(source_root.rglob("*.py")):
            if module.name == "atomic_io.py":
                continue
            for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), start=1):
                if ".write_text(" not in line:
                    continue
                if any(module.name == name and marker in line for name, marker in allowed):
                    continue
                offenders.append(f"{module.name}:{number}: {line.strip()}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
