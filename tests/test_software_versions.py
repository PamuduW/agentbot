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
    def test_dry_run_does_not_require_public_runtime_or_create_npm_cache(self) -> None:
        import base64
        import json
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "snapshot"
            directory.mkdir()
            (directory / "software.tsv").write_text("nodejs\tversion\tlinux-amd64\t24.1.0\n")
            lock = directory / "package-lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {"dependencies": {"skills": "1.2.3"}},
                            "node_modules/skills": {
                                "version": "1.2.3",
                                "resolved": "https://registry.npmjs.org/skills/-/skills-1.2.3.tgz",
                                "integrity": "sha512-" + base64.b64encode(bytes(64)).decode(),
                            },
                        },
                    }
                )
            )
            snapshot = SimpleNamespace(directory=directory, digest="a" * 64)
            artifact = {
                "package": "skills",
                "version": "1.2.3",
                "binary": "skills",
                "lock": lock.name,
            }
            runner = Mock()
            with (
                patch.object(software_versions, "operation_snapshot", return_value=snapshot),
                patch.object(software_versions, "resolve_entry", return_value=artifact),
            ):
                try:
                    command = software_versions.approved_npm_executable(
                        root, "skills_cli", runner, install=False
                    )
                except (ValueError, KeyError, OSError) as error:
                    self.fail(f"dry-run required a public Node/npm installation: {error}")
            self.assertEqual(root / "npm" / ("skills_cli-" + "a" * 64) / "bin/skills", command)
            self.assertFalse((root / "npm").exists())
            runner.run.assert_not_called()

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

            def npm_ci(argv, *, cwd, **kwargs):
                self.assertEqual("/dev/null", kwargs["env"].get("NPM_CONFIG_USERCONFIG"))
                global_config = Path(kwargs["env"]["NPM_CONFIG_GLOBALCONFIG"])
                self.assertEqual(cwd, global_config.parent)
                self.assertEqual("", global_config.read_text())
                self.assertEqual(
                    [
                        "/selected/node",
                        "/selected/npm-cli.js",
                        "ci",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                    ],
                    argv,
                )
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
                patch.object(software_versions, "shared_root", return_value=root),
                patch(
                    "src.catalog_node_runtime.private_npm_runtime",
                    return_value=(Path("/selected/node"), Path("/selected/npm-cli.js")),
                ),
            ):
                installed = software_versions.approved_npm_executable(root, "skills_cli", runner)
                self.assertTrue(installed.is_file())
                self.assertIn("/selected/node", installed.read_text())
                self.assertIn("unset NODE_OPTIONS NODE_PATH", installed.read_text())
                self.assertEqual(
                    installed, software_versions.approved_npm_executable(root, "skills_cli", runner)
                )
                self.assertEqual(1, runner.run.call_count)
                package_manifest = installed.parent.parent / "node_modules/skills/package.json"
                package_manifest.write_text(json.dumps({"version": "9.9.9"}))
                with self.assertRaisesRegex(ValueError, "differs"):
                    software_versions.approved_npm_executable(root, "skills_cli", runner)
                self.assertEqual(1, runner.run.call_count)


class SelectedNpmRuntimeTests(unittest.TestCase):
    def test_launcher_uses_selected_node_even_when_path_and_options_are_poisoned(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory(prefix="catalog runtime ") as temporary:
            directory = Path(temporary)
            node = directory / "selected node"
            node.write_text(
                '#!/bin/sh\n[ -z "${NODE_OPTIONS+x}" ] && [ -z "${NODE_PATH+x}" ] || exit 91\nprintf "%s\\n" "$@"\n'
            )
            node.chmod(0o700)
            package = directory / "skills entrypoint.js"
            package.write_text("fixture package\n")
            launcher = directory / "skills"
            launcher.write_text(software_versions._npm_launcher(node, package))
            launcher.chmod(0o700)
            result = subprocess.run(
                [str(launcher), "argument with spaces"],
                env={
                    "PATH": "/unapproved",
                    "NODE_OPTIONS": "--invalid",
                    "NODE_PATH": "/unapproved",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([str(package), "argument with spaces"], result.stdout.splitlines())

    def test_runtime_requires_catalog_receipts_paths_and_physical_versions(self) -> None:
        import hashlib
        import os
        import tempfile
        from types import SimpleNamespace

        from src.command_runner import CommandRunner

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            prefix = home / ".nvm/versions/node/v24.1.0"
            node = prefix / "bin/node"
            npm = prefix / "lib/node_modules/npm/bin/npm-cli.js"
            node.parent.mkdir(parents=True)
            npm.parent.mkdir(parents=True)
            npm.write_text("fixture npm entrypoint\n")
            node.write_text(
                '#!/bin/sh\n[ -z "${NODE_OPTIONS+x}" ] && [ -z "${NODE_PATH+x}" ] || exit 91\n'
                'if [ "$#" = 1 ]; then echo v24.1.0; else echo 11.1.0; fi\n'
            )
            node.chmod(0o700)
            (prefix / "bin/npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
            links = home / ".local/bin"
            links.mkdir(parents=True)
            for name in ("node", "npm"):
                (links / name).symlink_to(prefix / "bin" / name)
            table = b"nodejs\tversion\tlinux-amd64\t24.1.0\n"
            (home / "software.tsv").write_bytes(table)
            identity = hashlib.sha256(table).hexdigest()
            receipts = home / ".local/share/dotfiles-catalog"
            receipts.mkdir(parents=True)
            for key, version in (("nodejs", "24.1.0"), ("npm", "11.1.0")):
                (receipts / (key + ".native")).write_text(
                    f"native-runtime-v1\nnvm\n{version}\n{'a' * 64}\n{'b' * 40}\n{prefix}\n{identity}\n"
                )
            snapshot = SimpleNamespace(directory=home)
            with (
                patch.dict(
                    os.environ,
                    {
                        "HOME": str(home),
                        "NVM_DIR": str(home / ".nvm"),
                        "NODE_VERSION": "24.1.0",
                        "NPM_VERSION": "11.1.0",
                        "NODE_OPTIONS": "--invalid",
                        "NODE_PATH": "/unapproved",
                    },
                ),
                patch.object(
                    software_versions,
                    "resolve_entry",
                    side_effect=lambda _, key, _target: {
                        "version": {"nodejs": "24.1.0", "npm": "11.1.0"}[key]
                    },
                ),
            ):
                self.assertEqual(
                    (node, npm), software_versions._approved_npm_runtime(snapshot, CommandRunner())
                )
                receipt = receipts / "npm.native"
                original = receipt.read_text()
                receipt.write_text(original.replace(identity, "f" * 64))
                with self.assertRaisesRegex(ValueError, "ownership"):
                    software_versions._approved_npm_runtime(snapshot, CommandRunner())
                receipt.write_text(original)
                node.write_text("#!/bin/sh\necho v99.0.0\n")
                with self.assertRaisesRegex(ValueError, "physical"):
                    software_versions._approved_npm_runtime(snapshot, CommandRunner())
                receipt.unlink()
                with self.assertRaisesRegex(ValueError, "Dotfiles"):
                    software_versions._approved_npm_runtime(snapshot, CommandRunner())
