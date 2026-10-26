from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
import catalog_discovery
from software_catalog import load_snapshot

from . import test_software_catalog as fixtures


class CandidateTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write

    def candidate(self, fetch):
        self.assertTrue(
            hasattr(catalog_discovery, "build_candidate"),
            "daily discovery has no whole-catalog candidate builder",
        )
        self.write(approve=False)
        return catalog_discovery.build_candidate(
            load_snapshot(self.directory, require_approved=False), fetch=fetch
        )

    def test_discovery_resolves_every_required_target_without_approving_or_mutating_baseline(self):
        self.data["required_targets"] = ["linux-amd64", "linux-arm64", "wsl2-amd64"]
        entry = self.data["components"]["fixture"]
        entry["discovery"] = {
            "provider": "github-release",
            "locator": "fixture/tool",
            "asset": "tool-{arch}.tgz",
            "archive": "tgz",
            "binary": "tool-{arch}/tool",
        }
        before = copy.deepcopy(self.data)
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "v1.2.4",
            "assets": [
                {
                    "name": f"tool-{arch}.tgz",
                    "digest": "sha256:" + "c" * 64,
                    "browser_download_url": f"https://github.com/fixture/tool/releases/download/v1.2.4/tool-{arch}.tgz",
                }
                for arch in ("amd64", "arm64")
            ],
        }
        fetch = Mock(return_value=release)
        candidate = self.candidate(fetch)
        self.assertEqual(before, self.data)
        self.assertEqual(
            set(self.data["required_targets"]), set(candidate["components"]["fixture"]["targets"])
        )
        self.assertEqual(
            "1.2.4", candidate["components"]["fixture"]["targets"]["wsl2-amd64"]["version"]
        )
        self.assertEqual(
            "tool-arm64/tool",
            candidate["components"]["fixture"]["targets"]["linux-arm64"]["binary"],
        )
        self.assertFalse(candidate["auto_promote"])
        self.assertFalse((self.directory / "approval").exists())
        fetch.assert_called_once()

    def test_incomplete_provider_inventory_holds_before_network(self):
        fetch = Mock()
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "fixture"):
            self.candidate(fetch)
        fetch.assert_not_called()

    def test_source_candidate_binds_full_revision_and_preserves_declared_policy(self):
        entry = self.data["components"]["fixture"]
        entry.update(kind="git-source", targets={})
        entry["discovery"] = {"provider": "github-source", "locator": "fixture/tool"}
        candidate = self.candidate(Mock(return_value={"sha": "d" * 40}))
        self.assertEqual(
            {"any": {"revision": "d" * 40}}, candidate["components"]["fixture"]["targets"]
        )
        self.assertEqual(entry["discovery"], candidate["components"]["fixture"]["discovery"])

    def test_changed_bytes_at_the_same_version_are_held(self):
        entry = self.data["components"]["fixture"]
        entry["discovery"] = {
            "provider": "github-release",
            "locator": "fixture/tool",
            "asset": "tool",
        }
        previous = entry["targets"]["linux-amd64"]
        previous["url"] = "https://github.com/fixture/tool/releases/download/v1.2.3/tool"
        fetch = Mock(
            return_value={
                "draft": False,
                "prerelease": False,
                "tag_name": "v1.2.3",
                "assets": [
                    {
                        "name": "tool",
                        "digest": "sha256:" + "c" * 64,
                        "browser_download_url": previous["url"],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "replaced bytes"):
            self.candidate(fetch)

    def test_release_asset_cannot_redirect_candidate_to_another_publisher(self):
        self.data["components"]["fixture"]["discovery"] = {
            "provider": "github-release",
            "locator": "fixture/tool",
            "asset": "tool",
        }
        fetch = Mock(
            return_value={
                "draft": False,
                "prerelease": False,
                "tag_name": "v1.2.4",
                "assets": [
                    {
                        "name": "tool",
                        "digest": "sha256:" + "c" * 64,
                        "browser_download_url": "https://unrelated.invalid/tool",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "fixture: .*publisher"):
            self.candidate(fetch)

    def test_npm_archive_requires_registry_integrity_and_frozen_package_version(self):
        import base64

        entry = self.data["components"]["fixture"]
        entry["source"] = "npm"
        entry["discovery"] = {
            "provider": "npm",
            "locator": "npm",
            "archive": "tgz",
            "binary": "package/bin/npm-cli.js",
        }
        fetch = Mock(
            return_value={
                "version": "11.2.0",
                "dist": {
                    "tarball": "https://registry.npmjs.org/npm/-/npm-11.2.0.tgz",
                    "integrity": "sha512-" + base64.b64encode(b"a" * 64).decode(),
                },
            }
        )
        candidate = self.candidate(fetch)
        artifact = candidate["components"]["fixture"]["targets"]["linux-amd64"]
        self.assertEqual("sha512:" + (b"a" * 64).hex(), artifact["digest"])
        self.assertEqual("11.2.0", artifact["version"])
        fetch.return_value["dist"]["tarball"] = "https://unrelated.invalid/npm.tgz"
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "fixture: .*publisher"):
            self.candidate(fetch)

    def test_source_release_binds_version_to_exact_tag_commit(self):
        entry = self.data["components"]["fixture"]
        entry.update(kind="git-source", targets={})
        entry["discovery"] = {"provider": "github-release", "locator": "fixture/tool"}
        fetch = Mock(
            side_effect=[
                {"draft": False, "prerelease": False, "tag_name": "v0.40.8"},
                {"sha": "f" * 40},
            ]
        )
        candidate = self.candidate(fetch)
        self.assertEqual(
            {"any": {"version": "0.40.8", "revision": "f" * 40}},
            candidate["components"]["fixture"]["targets"],
        )
        self.assertEqual(
            "https://api.github.com/repos/fixture/tool/commits/v0.40.8", fetch.call_args.args[0]
        )

    def test_npm_candidate_contains_hash_bound_complete_lock_instead_of_a_floating_package(self):
        import base64
        import hashlib
        import json
        from unittest.mock import patch

        entry = self.data["components"]["fixture"]
        entry["source"] = "cspell"
        entry["discovery"] = {
            "provider": "npm",
            "locator": "cspell",
            "archive": "npm-lock",
            "binary": "cspell",
        }
        self.write(approve=False)
        raw = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"cspell": "9.1.0"}},
                    "node_modules/cspell": {
                        "version": "9.1.0",
                        "resolved": "https://registry.npmjs.org/cspell/-/cspell-9.1.0.tgz",
                        "integrity": "sha512-" + base64.b64encode(b"a" * 64).decode(),
                    },
                },
            }
        ).encode()
        tools = {
            "npm": "/selected/npm",
            "node": "/selected/node",
            "npm_version": "11.2.0",
            "node_version": "24.1.0",
        }
        locks = {}
        with patch("catalog_dependencies.generate_npm_lock", return_value=raw) as generate:
            candidate = catalog_discovery.build_candidate(
                load_snapshot(self.directory, require_approved=False),
                fetch=Mock(return_value={"version": "9.1.0"}),
                npm_tools=tools,
                locks=locks,
            )
        artifact = candidate["components"]["fixture"]["targets"]["linux-amd64"]
        self.assertEqual(raw, locks[artifact["lock"]])
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), artifact["digest"])
        self.assertEqual("cspell", artifact["package"])
        self.assertNotIn("url", artifact)
        generate.assert_called_once_with("cspell", "9.1.0", **tools)

    def test_candidate_publication_never_overwrites_existing_data_or_copies_approval(self):
        self.assertTrue(
            hasattr(catalog_discovery, "write_candidate"),
            "discovery cannot materialize an isolated candidate",
        )
        self.write(approve=True)
        baseline = load_snapshot(self.directory)
        output = self.directory / "proposal"
        result = catalog_discovery.write_candidate(baseline, self.data, output, {})
        self.assertFalse(result.approved)
        self.assertFalse((output / "approval").exists())
        original = (output / "software.json").read_bytes()
        with self.assertRaises(catalog_discovery.DiscoveryError):
            catalog_discovery.write_candidate(baseline, self.data, output, {})
        self.assertEqual(original, (output / "software.json").read_bytes())
        self.assertTrue(load_snapshot(self.directory).approved)

    def test_debian_release_uses_inspected_package_version_instead_of_release_tag(self):
        from unittest.mock import patch

        entry = self.data["components"].pop("fixture")
        self.data["components"]["powershell"] = entry
        entry["kind"] = "vendor-apt"
        entry["discovery"] = {
            "provider": "github-release",
            "locator": "PowerShell/PowerShell",
            "asset": "package.deb",
            "archive": "deb",
        }
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "v7.6.3",
            "assets": [
                {
                    "name": "package.deb",
                    "digest": "sha256:" + "c" * 64,
                    "browser_download_url": "https://github.com/PowerShell/PowerShell/releases/download/v7.6.3/package.deb",
                }
            ],
        }
        inspected = {
            "version": "7.6.3-1~ubuntu.24.04",
            "package": "powershell",
            "url": release["assets"][0]["browser_download_url"],
            "digest": "sha256:" + "c" * 64,
            "archive": "deb",
        }
        with patch("catalog_discovery.inspect_deb", return_value=inspected) as inspect:
            candidate = self.candidate(Mock(return_value=release))
        self.assertEqual(inspected, candidate["components"]["powershell"]["targets"]["linux-amd64"])
        inspect.assert_called_once()

    def test_extension_candidates_use_declared_hosts_and_required_windows_artifacts(self):
        self.data["required_targets"] = ["linux-amd64", "wsl2-amd64"]
        entry = self.data["components"].pop("fixture")
        self.data["components"]["extension.publisher.root"] = entry
        entry.update(kind="extension", source="publisher.root", targets={})
        entry["discovery"] = {
            "provider": "vscode-marketplace",
            "locator": "publisher.root",
            "hosts": "windows,wsl",
        }
        self.write(approve=False)
        resolver = Mock(
            return_value={
                "version": "1.2.3",
                "url": "https://example.invalid/fixture.vsix",
                "digest": "sha256:" + "a" * 64,
            }
        )
        candidate = catalog_discovery.build_candidate(
            load_snapshot(self.directory, require_approved=False),
            fetch=Mock(),
            extension_resolver=resolver,
        )
        self.assertEqual(
            {"linux-amd64", "wsl2-amd64", "windows-amd64"},
            set(candidate["components"]["extension.publisher.root"]["targets"]),
        )
        self.assertEqual(
            {"linux-amd64", "wsl2-amd64", "windows-amd64"},
            {call.args[1] for call in resolver.call_args_list},
        )

    def test_linux_candidate_preserves_windows_only_inventory_without_resolving_it(self):
        self.data["components"]["fixture"]["discovery"] = {
            "provider": "github-release",
            "locator": "fixture/tool",
            "asset": "tool",
        }
        windows = {
            "owner": "agentbot",
            "kind": "extension",
            "source": "publisher.windows",
            "discovery": {
                "provider": "vscode-marketplace",
                "locator": "publisher.windows",
                "hosts": "windows",
            },
            "requires": [],
            "blockers": ["Windows host is outside this rollout"],
            "targets": {},
        }
        self.data["components"]["extension.publisher.windows"] = copy.deepcopy(windows)
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "v1.2.4",
            "assets": [
                {
                    "name": "tool",
                    "digest": "sha256:" + "c" * 64,
                    "browser_download_url": "https://github.com/fixture/tool/releases/download/v1.2.4/tool",
                }
            ],
        }
        candidate = self.candidate(Mock(return_value=release))
        self.assertEqual(windows, candidate["components"]["extension.publisher.windows"])
        self.assertEqual(
            "1.2.4", candidate["components"]["fixture"]["targets"]["linux-amd64"]["version"]
        )

    def test_python_candidate_uses_constraint_resolved_profile_and_hash_bound_lock(self):
        import hashlib

        from software_catalog import load_snapshot

        entry = self.data["components"]["fixture"]
        entry.update(kind="python-package", source="fixture")
        entry["discovery"] = {"provider": "pypi", "locator": "fixture"}
        self.write(approve=False)
        raw = b"fixture==1.5.0 --hash=sha256:" + b"a" * 64 + b"\n"
        locks = {"locks/python/runtime.txt": raw}
        artifact = {
            "version": "1.5.0",
            "package": "fixture",
            "archive": "python-lock",
            "lock": "locks/python/runtime.txt",
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
        candidate = catalog_discovery.build_candidate(
            load_snapshot(self.directory, require_approved=False),
            locks=locks,
            python_resolver=lambda package, target: dict(artifact),
            fetch=lambda url: self.fail(
                "Python profiles must resolve source constraints, not latest metadata"
            ),
        )
        self.assertEqual(artifact, candidate["components"]["fixture"]["targets"]["linux-amd64"])
        entry["targets"]["linux-amd64"] = dict(artifact)
        (self.directory / "locks/python").mkdir(parents=True)
        (self.directory / "locks/python/runtime.txt").write_bytes(raw.replace(b"a" * 64, b"b" * 64))
        entry["targets"]["linux-amd64"]["digest"] = (
            "sha256:"
            + hashlib.sha256((self.directory / "locks/python/runtime.txt").read_bytes()).hexdigest()
        )
        self.write(approve=False)
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "replaced dependency bytes"):
            catalog_discovery.build_candidate(
                load_snapshot(self.directory, require_approved=False),
                locks=locks,
                python_resolver=lambda package, target: dict(artifact),
            )

    def test_native_runtime_candidate_keeps_publisher_logic_in_dotfiles(self):
        entry = self.data["components"].pop("fixture")
        self.data["components"]["nodejs"] = entry
        entry["discovery"] = {"provider": "node-lts", "locator": "nodejs.org"}
        self.write(approve=False)
        artifact = {
            "version": "24.10.0",
            "url": "https://nodejs.org/dist/v24.10.0/node-v24.10.0-linux-x64.tar.xz",
            "digest": "sha256:" + "c" * 64,
            "archive": "tar.xz",
            "binary": "node-v24.10.0-linux-x64/bin/node",
        }

        def resolve(identity, discovery, target):
            if identity != "nodejs" or target != "linux-amd64":
                self.fail("runtime discovery selected an unrelated identity or target")
            return dict(artifact)

        candidate = catalog_discovery.build_candidate(
            load_snapshot(self.directory, require_approved=False), runtime_resolver=resolve
        )
        self.assertEqual(artifact, candidate["components"]["nodejs"]["targets"]["linux-amd64"])
        entry["targets"]["linux-amd64"] = dict(artifact)
        entry["targets"]["linux-amd64"]["digest"] = "sha256:" + "a" * 64
        self.write(approve=False)
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "replaced bytes"):
            catalog_discovery.build_candidate(
                load_snapshot(self.directory, require_approved=False), runtime_resolver=resolve
            )

    def test_release_tag_prefix_is_explicit_for_codex_native_bundle(self):
        entry = self.data["components"]["fixture"]
        entry["discovery"] = {
            "provider": "github-release",
            "locator": "openai/codex",
            "tag_prefix": "rust-v",
            "asset": "codex-package-x86_64-unknown-linux-musl.tar.gz",
            "archive": "tar.gz",
            "binary": "bin/codex",
        }
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "rust-v0.164.0",
            "assets": [
                {
                    "name": "codex-package-x86_64-unknown-linux-musl.tar.gz",
                    "digest": "sha256:" + "c" * 64,
                    "browser_download_url": "https://github.com/openai/codex/releases/download/rust-v0.164.0/codex-package-x86_64-unknown-linux-musl.tar.gz",
                }
            ],
        }
        candidate = self.candidate(Mock(return_value=release))
        artifact = candidate["components"]["fixture"]["targets"]["linux-amd64"]
        self.assertEqual("0.164.0", artifact["version"])
        self.assertEqual("bin/codex", artifact["binary"])

    def test_graphify_without_owner_resolver_holds_before_any_metadata(self):
        self.data["components"]["fixture"]["discovery"] = {
            "provider": "pypi-tool",
            "locator": "graphifyy",
            "binary": "graphify",
        }
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "candidate held"):
            self.candidate(lambda url: self.fail("missing owner resolver must hold before network"))

    def test_linux_only_discovery_preserves_existing_unqualified_platform_pins(self):
        entry = self.data["components"].pop("fixture")
        self.data["components"]["nodejs"] = entry
        entry["discovery"] = {"provider": "node-lts", "locator": "nodejs.org"}
        old_arm = copy.deepcopy(entry["targets"]["linux-amd64"])
        old_arm["version"] = "1.2.2"
        entry["targets"]["linux-arm64"] = copy.deepcopy(old_arm)
        self.write(approve=False)

        def resolve(identity, discovery, target):
            if target != "linux-amd64":
                self.fail("Linux-only qualification must not update unqualified platform artifacts")
            return {**entry["targets"]["linux-amd64"], "version": "1.2.4"}

        candidate = catalog_discovery.build_candidate(
            load_snapshot(self.directory, require_approved=False), runtime_resolver=resolve
        )
        self.assertEqual(old_arm, candidate["components"]["nodejs"]["targets"]["linux-arm64"])
        self.assertEqual(
            "1.2.4", candidate["components"]["nodejs"]["targets"]["linux-amd64"]["version"]
        )
