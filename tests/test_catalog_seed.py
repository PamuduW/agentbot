from __future__ import annotations

import base64
import copy
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import load_snapshot

from . import test_software_catalog as fixtures


class SeedTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write

    def test_bootstrap_resolves_only_tooling_and_its_declared_dependencies_without_approval(self):
        from catalog_seed import build_bootstrap

        original = copy.deepcopy(self.data["components"]["fixture"])
        for key in ("nodejs", "npm", "uv", "nvm"):
            self.data["components"][key] = copy.deepcopy(original)
            self.data["components"][key]["targets"] = {}
        components = self.data["components"]
        components["nodejs"].update(
            discovery={"provider": "node-lts", "locator": "nodejs.org"}, requires=["nvm"]
        )
        components["npm"].update(
            source="npm",
            discovery={
                "provider": "npm",
                "locator": "npm",
                "archive": "tar",
                "binary": "package/bin/npm-cli.js",
            },
            requires=["nodejs"],
        )
        components["uv"]["discovery"] = {
            "provider": "github-release",
            "locator": "astral-sh/uv",
            "asset": "uv.tar.gz",
            "archive": "tar.gz",
            "binary": "uv",
        }
        components["nvm"].update(
            kind="git-source", discovery={"provider": "github-source", "locator": "nvm-sh/nvm"}
        )
        self.write(approve=False)

        def fetch(url):
            if url == "https://api.github.com/repos/nvm-sh/nvm/commits/HEAD":
                return {"sha": "c" * 40}
            if url == "https://registry.npmjs.org/npm/latest":
                return {
                    "version": "11.2.0",
                    "dist": {
                        "tarball": "https://registry.npmjs.org/npm/-/npm-11.2.0.tgz",
                        "integrity": "sha512-" + base64.b64encode(b"a" * 64).decode(),
                    },
                }
            if url == "https://api.github.com/repos/astral-sh/uv/releases/latest":
                return {
                    "draft": False,
                    "prerelease": False,
                    "tag_name": "v0.13.4",
                    "assets": [
                        {
                            "name": "uv.tar.gz",
                            "digest": "sha256:" + "d" * 64,
                            "browser_download_url": "https://github.com/astral-sh/uv/releases/download/v0.13.4/uv.tar.gz",
                        }
                    ],
                }
            self.fail("bootstrap touched a non-tooling publisher")

        def runtime(identity, discovery, target):
            self.assertEqual("nodejs", identity)
            return {
                "version": "24.10.0",
                "url": "https://nodejs.org/dist/v24.10.0/node-v24.10.0-linux-x64.tar.xz",
                "digest": "sha256:" + "e" * 64,
                "archive": "tar.xz",
                "binary": "node-v24.10.0-linux-x64/bin/node",
            }

        candidate = build_bootstrap(
            load_snapshot(self.directory, require_approved=False),
            fetch=fetch,
            runtime_resolver=runtime,
        )
        self.assertEqual(original, candidate["components"]["fixture"])
        self.assertEqual(set(self.data["components"]), set(candidate["components"]))
        self.assertFalse(candidate["auto_promote"])
        self.assertEqual(
            "24.10.0", candidate["components"]["nodejs"]["targets"]["linux-amd64"]["version"]
        )
        self.assertEqual("c" * 40, candidate["components"]["nvm"]["targets"]["any"]["revision"])
        self.assertFalse((self.directory / "approval").exists())

    def test_installed_bootstrap_tool_pins_cannot_move_during_full_discovery(self):
        from catalog_discovery import DiscoveryError
        from catalog_seed import verify_bootstrap_tools

        data = {
            "components": {
                key: {"requires": [], "targets": {"linux-amd64": {"version": "1.2.3"}}}
                for key in ("nodejs", "npm", "uv")
            }
        }
        verify_bootstrap_tools(data, copy.deepcopy(data))
        changed = copy.deepcopy(data)
        changed["components"]["uv"]["targets"]["linux-amd64"]["version"] = "1.2.4"
        with self.assertRaises(DiscoveryError):
            verify_bootstrap_tools(data, changed)

    def test_loaded_immutable_bootstrap_compares_equal_to_unchanged_candidate_data(self):
        from catalog_seed import verify_bootstrap_tools

        original = self.data["components"].pop("fixture")
        for key in ("nodejs", "npm", "uv"):
            self.data["components"][key] = copy.deepcopy(original)
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        verify_bootstrap_tools(snapshot.data, copy.deepcopy(self.data))
