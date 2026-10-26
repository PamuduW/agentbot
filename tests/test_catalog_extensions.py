from __future__ import annotations

import unittest

from src.catalog_extensions import ExtensionCatalogError, validate_extension_lock


class ExtensionCatalogTests(unittest.TestCase):
    def test_closure_requires_every_dependency_and_one_exact_root_version(self) -> None:
        lock = {
            "schema_version": 1,
            "extensions": {
                "publisher.root": {
                    "version": "1.2.3",
                    "url": "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/publisher/vsextensions/root/1.2.3/vspackage",
                    "digest": "sha256:" + "a" * 64,
                    "requires": ["publisher.dependency"],
                },
                "publisher.dependency": {
                    "version": "4.5.6",
                    "url": "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/publisher/vsextensions/dependency/4.5.6/vspackage",
                    "digest": "sha256:" + "b" * 64,
                    "requires": [],
                },
            },
        }
        entries = validate_extension_lock(lock, "publisher.root", "1.2.3")
        self.assertEqual({"publisher.root", "publisher.dependency"}, set(entries))
        with self.assertRaises(ExtensionCatalogError):
            validate_extension_lock(lock, "publisher.root", "9.9.9")
        del lock["extensions"]["publisher.dependency"]
        with self.assertRaises(ExtensionCatalogError):
            validate_extension_lock(lock, "publisher.root", "1.2.3")

    def test_external_and_newer_owned_extensions_are_preserved(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from src.catalog_extensions import preflight_installs, record_installs

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            installed = directory / "publisher.root-2.0.0"
            installed.mkdir()
            (installed / "package.json").write_text(
                json.dumps({"publisher": "publisher", "name": "root", "version": "2.0.0"})
            )
            entry = {"version": "3.0.0", "digest": "sha256:" + "b" * 64}
            with self.assertRaisesRegex(ExtensionCatalogError, "externally"):
                preflight_installs(directory, {"publisher.root": entry})
            record_installs(
                directory, {"publisher.root": {**entry, "version": "2.0.0"}}, ("publisher.root",)
            )
            self.assertEqual(
                ("publisher.root",), preflight_installs(directory, {"publisher.root": entry})
            )
            with self.assertRaisesRegex(ExtensionCatalogError, "newer"):
                preflight_installs(directory, {"publisher.root": {**entry, "version": "1.0.0"}})
            self.assertEqual(
                "2.0.0", json.loads((installed / "package.json").read_text())["version"]
            )

    def test_installer_uses_verified_vsix_path_and_records_exact_version(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock

        from src.command_runner import CommandResult
        from src.vscode import VSCodeHost, _install_catalog_extensions

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            host = VSCodeHost(
                "wsl", directory / "settings.json", directory / "extensions", directory / "code", ""
            )
            package = directory / "publisher.root-1.2.3.vsix"
            package.write_bytes(b"fixture")
            entry = {"version": "1.2.3", "digest": "sha256:" + "b" * 64}
            runner = Mock()

            def install(argv, **_kwargs):
                self.assertIn("--do-not-include-pack-dependencies", argv)
                self.assertIn("--do-not-sync", argv)
                self.assertEqual(str(package), argv[argv.index("--install-extension") + 1])
                installed = host.extensions_dir / "publisher.root-1.2.3"
                installed.mkdir(parents=True)
                (installed / "package.json").write_text(
                    json.dumps({"publisher": "publisher", "name": "root", "version": "1.2.3"})
                )
                return CommandResult(0)

            runner.run.side_effect = install
            result = _install_catalog_extensions(
                host,
                {"publisher.root": package},
                {"publisher.root": entry},
                ("publisher.root",),
                runner,
            )
            self.assertEqual({"publisher.root": "installed"}, result)
            self.assertEqual(
                "1.2.3",
                json.loads((host.extensions_dir / ".agentbot-catalog.json").read_text())[
                    "extensions"
                ]["publisher.root"]["version"],
            )

    def test_platform_download_selection_is_exact_and_cannot_carry_other_query_data(self):
        import copy

        base = "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/publisher/vsextensions/root/1.2.3/vspackage"
        lock = {
            "schema_version": 1,
            "extensions": {
                "publisher.root": {
                    "version": "1.2.3",
                    "url": base + "?targetPlatform=linux-arm64",
                    "digest": "sha256:" + "a" * 64,
                    "requires": [],
                }
            },
        }
        self.assertEqual(
            "1.2.3",
            validate_extension_lock(lock, "publisher.root", "1.2.3")["publisher.root"]["version"],
        )
        for url in (
            base + "?token=fixture",
            base + "?targetPlatform=linux-arm64&redirect=true",
            base.replace("1.2.3", "latest"),
            base.replace("/root/", "/another/"),
            base + "?targetPlatform=unknown",
        ):
            changed = copy.deepcopy(lock)
            changed["extensions"]["publisher.root"]["url"] = url
            with self.subTest(url=url), self.assertRaises(ExtensionCatalogError):
                validate_extension_lock(changed, "publisher.root", "1.2.3")

    def test_windows_extensions_outside_linux_rollout_are_reported_manual_without_changing_selection(
        self,
    ):
        import json
        from unittest.mock import patch

        from software_catalog import load_snapshot

        from src import vscode
        from src.command_runner import CommandRunner

        from . import test_software_catalog as fixtures

        fixtures.SoftwareCatalogTests.setUp(self)
        fixtures.SoftwareCatalogTests.write(self)
        snapshot = load_snapshot(self.directory)
        root = self.directory / "agentbot"
        root.mkdir()
        manifest = "version: 1\nextensions:\n  windows: [publisher.windows]\n"
        (root / "vscode.yaml").write_text(manifest)
        editor = root / "editor"
        (editor / "extensions").mkdir(parents=True)
        (editor / "resources/app").mkdir(parents=True)
        (editor / "resources/app/package.json").write_text(json.dumps({"version": "1.127.0"}))
        (editor / "bin").mkdir()
        host = vscode.VSCodeHost(
            "windows",
            editor / "settings.json",
            editor / "extensions",
            editor / "bin/code",
            "fixture",
        )
        report = vscode.VSCodeReport(
            hosts={"windows": host},
            extensions={
                "windows": vscode.ExtensionPlan(host="windows", missing=("publisher.windows",))
            },
        )
        with (
            patch("src.vscode.preview", return_value=report),
            patch("src.catalog_extensions.operation_snapshot", return_value=snapshot),
        ):
            result = vscode.apply(self.directory / "home", root, CommandRunner())
        self.assertTrue(result.extensions["windows"].skipped)
        self.assertEqual(("publisher.windows",), result.extensions["windows"].unmanaged)
        self.assertEqual((), result.failures)
        self.assertEqual([], list(host.extensions_dir.iterdir()))
        self.assertEqual(manifest, (root / "vscode.yaml").read_text())

    def test_builtin_dependency_requires_the_selected_editor_manifest(self):
        import json
        import tempfile
        from pathlib import Path

        from src.catalog_extensions import verify_builtin_extensions

        entries = {"vscode.github-authentication": {"builtin": True, "requires": []}}
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary)
            cli = server / "bin/code-server"
            cli.parent.mkdir()
            cli.write_text("fixture")
            with self.assertRaisesRegex(ExtensionCatalogError, "built-in"):
                verify_builtin_extensions(entries, cli)
            manifest = server / "extensions/github-authentication/package.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {"publisher": "vscode", "name": "github-authentication", "version": "0.0.2"}
                )
            )
            verify_builtin_extensions(entries, cli)
            manifest.write_text(
                json.dumps(
                    {"publisher": "unrelated", "name": "github-authentication", "version": "0.0.2"}
                )
            )
            with self.assertRaisesRegex(ExtensionCatalogError, "built-in"):
                verify_builtin_extensions(entries, cli)

    def test_apply_installs_staged_dependencies_before_alphabetically_earlier_root(self):
        import json
        import tempfile
        from contextlib import contextmanager
        from pathlib import Path
        from unittest.mock import Mock, patch

        from src import vscode
        from src.command_runner import CommandResult

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "vscode.yaml").write_text("version: 1\nextensions:\n  wsl: [a.root]\n")
            extensions = root / "extensions"
            extensions.mkdir()
            host = vscode.VSCodeHost("wsl", root / "settings.json", extensions, root / "code", "")
            report = vscode.VSCodeReport(hosts={"wsl": host})
            entries = {
                key: {"version": "1.2.3", "digest": "sha256:" + "a" * 64}
                for key in ("a.root", "z.dependency")
            }
            files = {key: root / (key + ".vsix") for key in ("z.dependency", "a.root")}
            for path in files.values():
                path.write_bytes(b"already staged fixture")

            @contextmanager
            def stage(*args, **kwargs):
                yield files, entries

            calls = []

            def install(argv, **kwargs):
                identity = Path(argv[argv.index("--install-extension") + 1]).stem
                if identity == "a.root":
                    self.assertEqual(["z.dependency"], calls)
                calls.append(identity)
                directory = extensions / (identity + "-1.2.3")
                directory.mkdir()
                publisher, name = identity.split(".", 1)
                (directory / "package.json").write_text(
                    json.dumps({"publisher": publisher, "name": name, "version": "1.2.3"})
                )
                return CommandResult(0)

            runner = Mock()
            runner.run.side_effect = install
            with (
                patch("src.vscode.preview", return_value=report),
                patch("src.vscode._editor_version", return_value=(1, 127)),
                patch("src.catalog_extensions.staged_extensions", stage),
            ):
                result = vscode.apply(root / "home", root, runner)
            self.assertEqual(["z.dependency", "a.root"], calls)
            self.assertEqual((), result.failures)

    def test_pack_membership_does_not_create_a_false_hard_dependency_cycle(self):
        from src.catalog_extensions import _closure_order

        entries = {
            "publisher.root": {"requires": ["publisher.base"], "pack": []},
            "publisher.base": {"requires": [], "pack": ["publisher.member"]},
            "publisher.member": {"requires": ["publisher.root"], "pack": []},
        }
        self.assertEqual(
            ("publisher.base", "publisher.root", "publisher.member"),
            _closure_order(entries, ("publisher.root",)),
        )
        entries["publisher.root"]["requires"] = []
        entries["publisher.root"]["pack"] = ["publisher.base"]
        entries["publisher.base"]["requires"] = ["publisher.member"]
        entries["publisher.member"]["requires"] = ["publisher.base"]
        with self.assertRaisesRegex(ExtensionCatalogError, "cyclic"):
            _closure_order(entries, ("publisher.root",))
