from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src import software_versions


class SoftwareVersionTests(unittest.TestCase):
    def test_target_distinguishes_wsl_and_architecture(self) -> None:
        with patch("platform.machine", return_value="aarch64"):
            self.assertEqual("linux-arm64", software_versions.operation_target("Linux normal"))
            self.assertEqual(
                "wsl2-arm64", software_versions.operation_target("microsoft-standard-WSL2")
            )
        with patch("platform.machine", return_value="unknown"):
            with self.assertRaises(ValueError):
                software_versions.operation_target("linux")

    def test_npm_lock_rejects_version_mismatch_before_running_installer(self) -> None:
        import json
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        from src.command_runner import CommandRunner

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "locks/npm/package-lock.json"
            lock.parent.mkdir(parents=True)
            lock.write_text(
                json.dumps(
                    {"lockfileVersion": 3, "packages": {"": {"dependencies": {"skills": "9.9.9"}}}}
                )
            )
            snapshot = SimpleNamespace(directory=root, digest="a" * 64)
            artifact = {
                "package": "skills",
                "version": "1.2.3",
                "lock": "locks/npm/package-lock.json",
                "binary": "skills",
            }
            with (
                patch.object(software_versions, "operation_snapshot", return_value=snapshot),
                patch.object(software_versions, "resolve_entry", return_value=artifact),
                patch.object(CommandRunner, "run") as run,
            ):
                with self.assertRaisesRegex(ValueError, "lock"):
                    software_versions.approved_npm_executable(root, "skills_cli", CommandRunner())
                run.assert_not_called()

    def test_source_clone_uses_catalog_commit(self) -> None:
        from src.skills_installer import _clone_remote_source

        with (
            patch.object(software_versions, "approved_source_revision", return_value="a" * 40),
            patch("src.skills_installer._clone_pinned_source") as clone,
        ):
            _clone_remote_source("owner/repo", Path("/fixture/checkout"))
            self.assertEqual(
                ("owner/repo", "a" * 40, Path("/fixture/checkout")), clone.call_args.args
            )

    def test_skills_cli_executes_owned_frozen_package_instead_of_npx(self) -> None:
        from src.command_runner import CommandResult, CommandRunner
        from src.skills_installer import SkillsInstallError, run_install_command

        runner = CommandRunner()
        executable = Path("/fixture/approved/node_modules/.bin/skills")
        with (
            patch.object(software_versions, "approved_npm_executable", return_value=executable),
            patch.object(runner, "run", return_value=CommandResult(0)) as run,
        ):
            run_install_command(
                ["npx", "--yes", "skills", "add", "/fixture/source", "-y"], runner=runner
            )
            self.assertEqual(
                [str(executable), "add", "/fixture/source", "-y"], run.call_args.args[0]
            )
            run.reset_mock()
            with self.assertRaises(SkillsInstallError):
                run_install_command(["npx", "--yes", "skills", "update"], runner=runner)
            run.assert_not_called()


class ApprovedNpmInstallTests(unittest.TestCase):
    def test_install_uses_frozen_closure_and_reuses_only_verified_cache(self) -> None:
        import base64
        import hashlib
        import json
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import Mock

        from src.command_runner import CommandResult

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot_dir = root / "snapshot"
            lock = snapshot_dir / "locks/npm/package-lock.json"
            lock.parent.mkdir(parents=True)
            integrity = "sha512-" + base64.b64encode(bytes(64)).decode()
            lock.write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {"dependencies": {"skills": "1.2.3"}},
                            "node_modules/skills": {
                                "version": "1.2.3",
                                "resolved": "https://registry.npmjs.org/skills/-/skills-1.2.3.tgz",
                                "integrity": integrity,
                            },
                        },
                    }
                )
            )
            snapshot = SimpleNamespace(
                directory=snapshot_dir, digest=hashlib.sha256(lock.read_bytes()).hexdigest()
            )
            artifact = {
                "package": "skills",
                "version": "1.2.3",
                "binary": "skills",
                "lock": "locks/npm/package-lock.json",
            }
            runner = Mock()

            def npm_ci(argv, *, cwd, **_kwargs):
                self.assertEqual(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], argv)
                self.assertEqual(
                    {"skills": "1.2.3"},
                    json.loads((cwd / "package.json").read_text())["dependencies"],
                )
                package = cwd / "node_modules/skills"
                package.mkdir(parents=True)
                (package / "package.json").write_text(json.dumps({"version": "1.2.3"}))
                executable = package / "skills"
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o700)
                bins = cwd / "node_modules/.bin"
                bins.mkdir()
                (bins / "skills").symlink_to("../skills/skills")
                return CommandResult(0)

            runner.run.side_effect = npm_ci
            with (
                patch.object(software_versions, "operation_snapshot", return_value=snapshot),
                patch.object(software_versions, "resolve_entry", return_value=artifact),
            ):
                installed = software_versions.approved_npm_executable(root, "skills_cli", runner)
                self.assertTrue(installed.is_file())
                self.assertEqual(
                    installed, software_versions.approved_npm_executable(root, "skills_cli", runner)
                )
                self.assertEqual(1, runner.run.call_count)
                package_manifest = installed.resolve().parent / "package.json"
                package_manifest.write_text(json.dumps({"version": "9.9.9"}))
                with self.assertRaisesRegex(ValueError, "differs"):
                    software_versions.approved_npm_executable(root, "skills_cli", runner)
                self.assertEqual(1, runner.run.call_count)
