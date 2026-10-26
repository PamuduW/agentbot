from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_qualification import QualificationError, validate_results

from . import test_software_catalog as fixtures


class QualificationTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write

    def test_gate_requires_every_target_and_scenario(self) -> None:
        self.write(approve=False)
        from software_catalog import load_snapshot

        snapshot = load_snapshot(self.directory, require_approved=False)
        sources = dict(snapshot.data["sources"])
        receipt = {
            "catalog_digest": snapshot.digest,
            "sources": sources,
            "target": "linux-amd64",
            "run_id": "123",
            "runner_image": "fixture-image",
            "scenarios": dict.fromkeys(("fresh", "upgrade", "repeat", "failure-safety"), "success"),
            "checks": {
                "product-gates": "success",
                "behavior": "success",
                "updater-controls": "success",
            },
            "observations": {
                "fixture": {
                    "version": "1.2.3",
                    "integrity": "sha256:" + "b" * 64,
                    "owned": True,
                    "managed_path": True,
                }
            },
        }
        result = validate_results(snapshot, [receipt], expected_sources=sources, run_id="123")
        self.assertEqual(["linux-amd64"], result["required_targets"])
        mutations = (
            lambda r: r["scenarios"].update(upgrade="skipped"),
            lambda r: r["checks"].update(behavior="cancelled"),
            lambda r: r.update(catalog_digest="0" * 64),
            lambda r: r["sources"].update(agentbot="0" * 40),
            lambda r: r["observations"]["fixture"].update(owned=False),
            lambda r: r["observations"]["fixture"].update(version="9.9.9"),
            lambda r: r["observations"].clear(),
            lambda r: r.update(run_id="124"),
        )
        for mutate in mutations:
            changed = copy.deepcopy(receipt)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(QualificationError):
                validate_results(snapshot, [changed], expected_sources=sources, run_id="123")
        for results in ([], [receipt, receipt]):
            with self.assertRaises(QualificationError):
                validate_results(snapshot, results, expected_sources=sources, run_id="123")

    def test_wsl_requires_both_declared_extension_hosts_and_native_linux_only_its_host(self):
        from software_catalog import load_snapshot

        self.data["components"] = {
            "extension.publisher.tool": {
                "owner": "agentbot",
                "kind": "extension",
                "source": "publisher.tool",
                "discovery": {
                    "provider": "vscode-marketplace",
                    "locator": "publisher.tool",
                    "hosts": "windows,wsl",
                },
                "requires": [],
                "blockers": [],
                "targets": {},
            }
        }
        self.data["required_targets"] = ["linux-amd64", "wsl2-amd64"]
        entry = self.data["components"]["extension.publisher.tool"]
        for target in ("linux-amd64", "wsl2-amd64", "windows-amd64"):
            entry["targets"][target] = {
                "version": "1.2.3",
                "url": "https://example.invalid/extension.vsix",
                "digest": "sha256:" + "a" * 64,
            }
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        sources = dict(snapshot.data["sources"])
        observation = {
            "version": "1.2.3",
            "integrity": "sha256:" + "a" * 64,
            "owned": True,
            "managed_path": True,
        }

        def receipt(target, hosts):
            return {
                "catalog_digest": snapshot.digest,
                "sources": sources,
                "target": target,
                "run_id": "123",
                "runner_image": "fixture-image",
                "scenarios": dict.fromkeys(
                    ("fresh", "upgrade", "repeat", "failure-safety"), "success"
                ),
                "checks": dict.fromkeys(
                    ("product-gates", "behavior", "updater-controls"), "success"
                ),
                "observations": {
                    "extension.publisher.tool@" + host: dict(observation) for host in hosts
                },
            }

        linux = receipt("linux-amd64", ["wsl"])
        wsl = receipt("wsl2-amd64", ["windows", "wsl"])
        validate_results(snapshot, [linux, wsl], expected_sources=sources, run_id="123")
        del wsl["observations"]["extension.publisher.tool@windows"]
        with self.assertRaises(QualificationError):
            validate_results(snapshot, [linux, wsl], expected_sources=sources, run_id="123")

    def test_linux_qualification_excludes_windows_only_extensions_without_erasing_inventory(self):
        from software_catalog import load_snapshot

        self.data["components"]["extension.publisher.windows"] = {
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
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        sources = dict(snapshot.data["sources"])
        receipt = {
            "catalog_digest": snapshot.digest,
            "sources": sources,
            "target": "linux-amd64",
            "run_id": "123",
            "runner_image": "fixture-image",
            "scenarios": dict.fromkeys(("fresh", "upgrade", "repeat", "failure-safety"), "success"),
            "checks": dict.fromkeys(("product-gates", "behavior", "updater-controls"), "success"),
            "observations": {
                "fixture": {
                    "version": "1.2.3",
                    "integrity": "sha256:" + "b" * 64,
                    "owned": True,
                    "managed_path": True,
                }
            },
        }
        result = validate_results(snapshot, [receipt], expected_sources=sources, run_id="123")
        self.assertEqual(["linux-amd64"], result["required_targets"])
        self.assertIn("extension.publisher.windows", snapshot.data["components"])


if __name__ == "__main__":
    unittest.main()
