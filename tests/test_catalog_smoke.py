from __future__ import annotations

import copy
import importlib
import json
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_qualification import validate_results
from software_catalog import load_snapshot

from . import test_software_catalog as fixtures


class SmokeAuditTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write

    def bundle(self):
        self.data["components"]["fixture"]["targets"]["linux-amd64"]["archive"] = "tar.gz"
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        scopes = (
            "user-tools-smoke-only",
            "tooling-smoke-only",
            "vendor-package-smoke-only",
            "linux-font-smoke-only",
            "behavior-smoke-only",
            "integration-smoke-only",
            "docker-lifecycle-smoke-only",
        )
        receipts = {
            scope: {
                "schema_version": 1,
                "scope": scope,
                "approved": False,
                "catalog_digest": snapshot.digest,
                "sources": dict(snapshot.data["sources"]),
            }
            for scope in scopes
        }
        receipts[scopes[0]]["observations"] = {
            phase: {"fixture": "1.2.3"} for phase in ("fresh", "repeat")
        }
        receipts[scopes[1]].update(
            python_packages={},
            skills_cli_version="1",
            development={
                "packages": {},
                "fresh": "success",
                "repeat": "success",
                "ruff": "success",
                "mypy": "success",
                "python_suite": "success",
            },
            fresh="success",
            repeat="success",
        )
        receipts[scopes[2]].update(observations={"fresh": {}, "repeat": {}}, application_probes={})
        receipts[scopes[3]].update(fresh="success", repeat="success")
        receipts[scopes[4]]["checks"] = dict.fromkeys(
            ("node-execution", "nvm-execution", "go-compilation", "chrome-dom", "mermaid-svg"),
            "success",
        )
        receipts[scopes[5]].update(
            extensions={
                "versions": {},
                "ownership": {},
                "repeat_no_downloads_or_installs": True,
                "settings_verified": True,
                "wsl_certified": False,
            },
            skills={"sources": {}, "skills": {}, "repeat_content_and_provenance": True},
            fresh="success",
            repeat="success",
        )
        receipts[scopes[6]].update(
            fresh="success",
            repeat="success",
            failed_replacement_recovery="success",
            volume_data_preserved=True,
            local_status_api="success",
            upgrade_qualified=False,
        )
        return snapshot, receipts

    def audit(self, snapshot, receipts):
        try:
            module = importlib.import_module("catalog_smoke")
        except ImportError:
            self.fail("missing complete smoke inventory audit")
        return module.audit_smoke(snapshot, list(receipts.values()))

    def test_cli_invalid_context_cannot_write_or_replace_evidence(self):
        import subprocess

        script = (
            shared_python_path(Path(__file__).resolve().parents[1]).parents[3]
            / "scripts/catalog/audit-smoke.py"
        )
        output = self.directory / "audit.json"
        command = [
            sys.executable,
            str(script),
            str(self.directory / "missing-context"),
            "--receipt",
            str(self.directory / "missing-receipt"),
            "--output",
            str(output),
        ]
        first = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(2, first.returncode, first.stderr)
        self.assertFalse(output.exists())
        self.assertNotIn("Traceback", first.stderr)
        output.write_text("preserve")
        repeat = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(2, repeat.returncode)
        self.assertEqual("preserve", output.read_text())

    def test_complete_smoke_is_explicitly_insufficient_for_approval(self):
        snapshot, receipts = self.bundle()
        result = self.audit(snapshot, receipts)
        self.assertEqual({"fixture"}, set(result["observations"]))
        self.assertFalse(result["approved"])
        self.assertNotEqual("success", result["scenarios"]["upgrade"])
        with self.assertRaises(ValueError):
            validate_results(
                snapshot, [result], expected_sources=snapshot.data["sources"], run_id="fixture"
            )

    def test_missing_stale_duplicated_or_changed_receipts_hold(self):
        snapshot, receipts = self.bundle()
        for scope in receipts:
            for field, changed in (
                ("catalog_digest", "f" * 64),
                ("sources", {}),
                ("approved", True),
                ("schema_version", True),
            ):
                broken = copy.deepcopy(receipts)
                broken[scope][field] = changed
                with self.subTest(scope=scope, field=field), self.assertRaises(ValueError):
                    self.audit(snapshot, broken)
            missing = copy.deepcopy(receipts)
            del missing[scope]
            with self.subTest(missing=scope), self.assertRaises(ValueError):
                self.audit(snapshot, missing)
        duplicated = dict(receipts, duplicate=receipts["user-tools-smoke-only"])
        with self.assertRaises(ValueError):
            self.audit(snapshot, duplicated)

    def test_uncovered_future_component_or_wrong_installed_version_holds(self):
        snapshot, receipts = self.bundle()
        for phase in ("fresh", "repeat"):
            for changed in ({}, {"fixture": "1.2.4"}, {"fixture": "1.2.3", "extra": "1"}):
                broken = copy.deepcopy(receipts)
                broken["user-tools-smoke-only"]["observations"][phase] = changed
                with self.subTest(phase=phase, changed=changed), self.assertRaises(ValueError):
                    self.audit(snapshot, broken)
        self.data["components"]["future"] = copy.deepcopy(self.data["components"]["fixture"])
        self.write(approve=False)
        future = load_snapshot(self.directory, require_approved=False)
        for receipt in receipts.values():
            receipt["catalog_digest"] = future.digest
        with self.assertRaises(ValueError):
            self.audit(future, receipts)

    def full_bundle(self):
        import hashlib

        _, receipts = self.bundle()
        base = copy.deepcopy(self.data["components"]["fixture"])

        def add(key, owner, kind, artifact, source="fixture/tool", discovery=None):
            entry = copy.deepcopy(base)
            entry.update(owner=owner, kind=kind, source=source, discovery=discovery or {})
            entry["targets"]["linux-amd64"].update(artifact)
            self.data["components"][key] = entry
            return entry["targets"]["linux-amd64"]

        def lock(name, raw):
            path = self.directory / "locks" / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(raw)
            return {"lock": "locks/" + name, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}

        revision = "c" * 40
        add("manager", "dotfiles", "git-source", {"revision": revision})
        py = lock("python.txt", b"foo==1.2.3 --hash=sha256:" + b"b" * 64 + b"\n")
        add(
            "python.foo",
            "agentbot",
            "python-package",
            {**py, "archive": "python-lock", "package": "foo"},
        )
        vsix = {
            "version": "1.2.3",
            "digest": "sha256:" + "d" * 64,
            "url": "https://example.invalid/a.vsix",
            "requires": [],
        }
        ext = lock(
            "extension.json",
            json.dumps({"schema_version": 2, "extensions": {"fixture.editor": vsix}}).encode(),
        )
        add(
            "extension.fixture.editor",
            "agentbot",
            "extension",
            {**ext, "archive": "vsix-lock"},
            discovery={"hosts": "wsl"},
        )
        add(
            "skill-source.skills",
            "agentbot",
            "git-source",
            {"revision": revision},
            source="fixture/skills",
        )
        add("font", "dotfiles", "software", {"archive": "zip", "binary": "fonts"})
        for key in ("powershell", "obsidian", "docker.engine"):
            add(key, "dotfiles", "vendor-apt", {"archive": "deb", "package": key.replace(".", "-")})
        image = add(
            "portainer",
            "dotfiles",
            "software",
            {"archive": "oci", "package": "portainer/portainer-ce"},
        )
        add("skills_cli", "agentbot", "software", {})
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        for receipt in receipts.values():
            receipt["catalog_digest"] = snapshot.digest
        for phase in ("fresh", "repeat"):
            receipts["user-tools-smoke-only"]["observations"][phase]["manager"] = revision
            receipts["vendor-package-smoke-only"]["observations"][phase] = {
                key: {
                    "package": key.replace(".", "-"),
                    "version": "1.2.3",
                    "receipt_digest": snapshot.digest,
                    "hold": True,
                }
                for key in ("powershell", "obsidian", "docker.engine")
            }
        receipts["vendor-package-smoke-only"]["application_probes"] = {
            "powershell": {"local_pipeline_json": "success"},
            "obsidian": {"xvfb_visible_window": "success"},
        }
        tooling = receipts["tooling-smoke-only"]
        tooling["skills_cli_version"] = "1.2.3"
        tooling["python_packages"] = {"foo": "1.2.3"}
        tooling["development"]["packages"] = {"foo": "1.2.3"}
        integration = receipts["integration-smoke-only"]
        integration["extensions"]["versions"] = {"fixture.editor": "1.2.3"}
        integration["extensions"]["ownership"] = {
            "fixture.editor": {key: vsix[key] for key in ("version", "digest")}
        }
        integration["skills"]["sources"] = {
            "skills": {
                "repo": "fixture/skills",
                "revision": revision,
                "installed": ["fixture-skill"],
            }
        }
        integration["skills"]["skills"] = {
            "fixture-skill": {
                "source": "fixture/skills",
                "sourceRevision": revision,
                "sourceType": "github",
                "sourceUrl": "https://github.com/fixture/skills.git",
                "skillFolderHash": "a" * 64,
            }
        }
        receipts["linux-font-smoke-only"].update(
            version="1.2.3", fontconfig_match="/home/test/.local/share/fonts/font.ttf"
        )
        receipts["docker-lifecycle-smoke-only"].update(
            packages={"docker.engine": "1.2.3"},
            portainer={
                "reference": image["package"] + "@" + image["digest"],
                "image_id": "sha256:" + "a" * 64,
            },
        )
        return snapshot, receipts

    def test_all_managed_kinds_are_audited_and_tampered_evidence_holds(self):
        snapshot, receipts = self.full_bundle()
        result = self.audit(snapshot, receipts)
        self.assertEqual(len(self.data["components"]), len(result["observations"]))
        changes = (
            ("vendor-package-smoke-only", ("observations", "repeat", "powershell", "hold"), False),
            ("tooling-smoke-only", ("development", "packages", "foo"), "2.0.0"),
            (
                "integration-smoke-only",
                ("extensions", "ownership", "fixture.editor", "digest"),
                "sha256:" + "e" * 64,
            ),
            ("integration-smoke-only", ("skills", "sources", "skills", "revision"), "f" * 40),
            (
                "integration-smoke-only",
                ("skills", "skills", "fixture-skill", "sourceRevision"),
                "f" * 40,
            ),
            ("integration-smoke-only", ("skills", "sources", "skills", "installed"), []),
            ("linux-font-smoke-only", ("fontconfig_match",), "relative.ttf"),
            (
                "docker-lifecycle-smoke-only",
                ("portainer", "reference"),
                "portainer/portainer-ce:latest",
            ),
            ("docker-lifecycle-smoke-only", ("volume_data_preserved",), False),
            ("behavior-smoke-only", ("checks", "mermaid-svg"), "skipped"),
        )
        for scope, path, value in changes:
            broken = copy.deepcopy(receipts)
            destination = broken[scope]
            for key in path[:-1]:
                destination = destination[key]
            destination[path[-1]] = value
            with self.subTest(scope=scope, path=path), self.assertRaises(ValueError):
                self.audit(snapshot, broken)
