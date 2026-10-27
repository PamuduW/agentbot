from __future__ import annotations

import copy
import importlib
import unittest
from urllib.parse import quote


class PythonRuntimeDiscoveryTests(unittest.TestCase):
    def example(self):
        name = "cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
        discovery = {
            "provider": "python-standalone",
            "locator": "astral-sh/python-build-standalone",
            "constraint": "3.12",
            "archive": "tar.gz",
            "binary": "python/bin/python3.12",
        }
        release = {
            "tag_name": "20261026",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": name,
                    "size": 34505639,
                    "digest": "sha256:" + "a" * 64,
                    "browser_download_url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
                    + quote(name, safe=""),
                }
            ],
        }
        return discovery, release

    def resolve(self, discovery, release, target="linux-amd64"):
        try:
            module = importlib.import_module("src.catalog_python_runtime_discovery")
        except ImportError:
            self.fail("private Python runtime discovery is missing")

        def fetch(url):
            self.assertEqual(
                "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest",
                url,
            )
            return release

        return module.resolve_python_runtime(discovery, target, fetch=fetch)

    def test_exact_baseline_cpython_build_is_frozen_with_its_publisher_digest(self):
        discovery, release = self.example()
        result = self.resolve(discovery, release)
        self.assertEqual("3.12.16+20261026", result["version"])
        self.assertEqual(release["assets"][0]["digest"], result["digest"])
        self.assertEqual("python/bin/python3.12", result["binary"])
        self.assertEqual("cpython", result["package"])

    def test_candidate_dispatch_requires_the_agentbot_owner(self):
        discovery, release = self.example()
        artifact = self.resolve(discovery, release)
        from catalog_discovery import _resolve_owned_targets

        entry = {"owner": "agentbot", "kind": "software", "discovery": discovery, "targets": {}}
        result = _resolve_owned_targets(
            None,
            "python_runtime",
            entry,
            ["linux-amd64"],
            None,
            lambda key, metadata, target: self.resolve(metadata, release, target),
            {},
        )
        self.assertEqual(artifact, result["linux-amd64"])
        for owner, kind in (("dotfiles", "software"), ("agentbot", "vendor-apt")):
            with self.subTest(owner=owner, kind=kind), self.assertRaises(ValueError):
                _resolve_owned_targets(
                    None,
                    "python_runtime",
                    {**entry, "owner": owner, "kind": kind},
                    ["linux-amd64"],
                    None,
                    lambda *args: artifact,
                    {},
                )

    def test_ambiguous_changed_or_unsupported_metadata_is_held(self):
        discovery, release = self.example()
        for change in (
            lambda value: value.update(draft=True),
            lambda value: value.update(prerelease=True),
            lambda value: value.update(tag_name="latest"),
            lambda value: value["assets"][0].update(digest=None),
            lambda value: value["assets"][0].update(size=200_000_000),
            lambda value: value["assets"][0].update(
                browser_download_url="https://example.invalid/python.tgz"
            ),
            lambda value: value["assets"][0].update(
                name="cpython-3.13.1+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
            ),
            lambda value: value["assets"].append(copy.deepcopy(value["assets"][0])),
        ):
            value = copy.deepcopy(release)
            change(value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.resolve(discovery, value)
        for changed in (
            {"locator": "other/python"},
            {"constraint": "3.14"},
            {"binary": "../python"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.resolve({**discovery, **changed}, release)
        with self.assertRaises(ValueError):
            self.resolve(discovery, release, target="linux-arm64")
