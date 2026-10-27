from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_context import load_qualification_context
from software_catalog import load_snapshot

from . import test_catalog_context as contexts
from . import test_software_catalog as fixtures

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/catalog/qualify-integrations.py"


def driver():
    if not SCRIPT.is_file():
        raise AssertionError("missing real integration smoke driver")
    spec = importlib.util.spec_from_file_location("catalog_integration_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IntegrationSmokeTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write
    sources = contexts.QualificationContextTests.sources

    def test_invalid_context_writes_no_evidence(self):
        driver()
        output = self.directory / "result.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.directory / "missing"), "--output", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertFalse(output.exists())
        self.assertNotIn("Traceback", result.stderr)

    def test_editor_metadata_binds_official_url_commit_and_checksum(self):
        module = driver()
        commit = "a" * 40
        value = {
            "version": commit,
            "productVersion": "1.143.1",
            "name": "1.143.1",
            "sha256hash": "b" * 64,
            "url": f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/{commit}/vscode-server-linux-x64.tar.gz",
        }
        parsed = module.editor_metadata(value)
        self.assertEqual(commit, parsed["commit"])
        self.assertEqual("b" * 64, parsed["sha256"])
        for changed in (
            {"url": "https://example.invalid/server.tgz"},
            {"version": "latest"},
            {"sha256hash": "invalid"},
            {"productVersion": "1.2.3-beta"},
            {"name": "1.2.3"},
            {"url": value["url"] + "?credential=fixture"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                module.editor_metadata({**value, **changed})

    def test_editor_version_probe_can_create_its_extension_store(self):
        import hashlib
        import io
        import os
        import tarfile
        from unittest.mock import patch

        module = driver()
        commit = "a" * 40
        archive = io.BytesIO()
        files = {
            "server/package.json": json.dumps({"version": "1.143.1"}).encode(),
            "server/bin/code-server": (
                '#!/bin/sh\nmkdir -p "$HOME/.vscode-server/extensions"\n'
                f"printf '%s\\n' 1.143.1 {commit} x64\n"
            ).encode(),
        }
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for name, raw in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                info.mode = 0o755 if name.endswith("code-server") else 0o644
                tar.addfile(info, io.BytesIO(raw))
        raw = archive.getvalue()
        metadata = {
            "version": commit,
            "name": "1.143.1",
            "productVersion": "1.143.1",
            "sha256hash": hashlib.sha256(raw).hexdigest(),
            "url": f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/{commit}/vscode-server-linux-x64.tar.gz",
        }
        home = self.directory / "home"
        work = self.directory / "work"
        home.mkdir()
        work.mkdir()

        def response(url, **kwargs):
            return io.BytesIO(raw if url == metadata["url"] else json.dumps(metadata).encode())

        with (
            patch.object(module.urllib.request, "build_opener") as opener,
            patch.dict(os.environ, {"HOME": str(home)}),
        ):
            opener.return_value.open.side_effect = response
            result = module.prepare_editor(
                home, work, {"dotfiles": SCRIPT.parents[3] / "dotfiles"}, module.RecordingRunner()
            )
        self.assertEqual(commit, result["commit"])
        self.assertTrue((home / ".vscode-server/extensions").is_dir())

    def test_private_context_copies_only_verified_data_without_forging_approval(self):
        module = driver()
        roots = self.sources()
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "context.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(path) for name, path in roots.items()},
                }
            )
        )
        private = module.private_context(manifest, self.directory / "private")
        copied, actual_roots, _ = load_qualification_context(private)
        self.assertEqual(snapshot.digest, copied.digest)
        self.assertEqual(roots, actual_roots)
        self.assertFalse(copied.approved)
        self.assertFalse((copied.directory / "approval").exists())
        self.assertFalse((copied.directory / "agentbot").exists())
        self.assertNotEqual(snapshot.directory, copied.directory)
        with self.assertRaises((ValueError, OSError)):
            module.private_context(manifest, self.directory / "private")
