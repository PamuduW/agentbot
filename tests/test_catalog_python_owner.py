from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import load_snapshot

from . import test_catalog_context as contexts
from . import test_software_catalog as fixtures


class PythonOwnerLauncherTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write
    sources = contexts.QualificationContextTests.sources

    def prepare(self):
        roots = self.sources()
        selected = self.directory / "selected-python"
        selected.write_text("#!/bin/sh\nexit 0\n")
        selected.chmod(0o700)
        helper = roots["agentbot"] / "scripts/catalog/python-runtime.py"
        helper.parent.mkdir(parents=True)
        helper.write_text(
            """import json,os,pathlib,sys
assert 'GITHUB_TOKEN' not in os.environ
assert 'AGENTBOT_QUALIFICATION_CONTEXT' not in os.environ
args=sys.argv[1:]
assert args[0]=='--digest'
if '--qualification-context' in args:
    data=json.loads(pathlib.Path(args[args.index('--qualification-context')+1]).read_text())
    assert data['digest']==args[1]
print("""
            + repr(str(selected) + "\t" + "d" * 64)
            + """)
"""
        )
        import subprocess

        for args in (["add", "."], ["commit", "-qm", "frozen owner helper"]):
            subprocess.run(["/usr/bin/git", "-C", str(roots["agentbot"]), *args], check=True)
        self.data["sources"]["agentbot"] = subprocess.check_output(
            ["/usr/bin/git", "-C", str(roots["agentbot"]), "rev-parse", "HEAD"], text=True
        ).strip()
        self.data["components"]["python_runtime"] = {
            **self.data["components"]["fixture"],
            "owner": "agentbot",
            "kind": "software",
            "source": "astral-sh/python-build-standalone",
        }
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        context = self.directory / "context.json"
        context.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        return snapshot, roots, context, selected

    def module(self):
        try:
            return importlib.import_module("catalog_python_owner")
        except ImportError:
            self.fail("shared Python owner launcher is missing")

    def test_explicit_qualification_invokes_only_the_frozen_owner_without_forwarding_credentials(
        self,
    ):
        module = self.module()
        snapshot, roots, context, selected = self.prepare()
        before = (self.directory / "software.json").read_bytes()
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "fixture-secret-not-forwarded",
                "AGENTBOT_QUALIFICATION_CONTEXT": "ambient-context-not-forwarded",
            },
        ):
            actual = module.selected_python_runtime(
                snapshot, roots["dotfiles-shared"], qualification_context=context
            )
        self.assertEqual(selected, actual)
        self.assertEqual(before, (self.directory / "software.json").read_bytes())
        self.assertFalse((self.directory / "approval").exists())
        self.assertFalse((self.directory / "sources").exists())

    def test_normal_call_uses_an_approved_git_bound_snapshot_without_a_qualification_flag(self):
        import shutil
        import subprocess

        from software_catalog import freeze_approved_snapshot

        module = self.module()
        _snapshot, roots, _context, selected = self.prepare()
        self.write(approve=True)
        authority = roots["dotfiles-shared"]
        source_revision = self.data["sources"]["dotfiles-shared"]
        catalog = authority / "catalog"
        catalog.mkdir()
        for name in ("software.json", "software.tsv", "approval", "approval.json"):
            shutil.copyfile(self.directory / name, catalog / name)
        for args in (
            ["add", "."],
            ["commit", "-qm", "approved catalog"],
            ["update-ref", "refs/remotes/origin/main", "HEAD"],
            ["checkout", "--detach", "--quiet", source_revision],
        ):
            subprocess.run(["/usr/bin/git", "-C", str(authority), *args], check=True)
        approved = freeze_approved_snapshot(authority, self.directory / "cache")
        original = subprocess.run

        def no_network(argv, **kwargs):
            self.assertFalse(
                argv[0] == "git" and any(value in argv for value in ("fetch", "clone")),
                "fixture source acquisition attempted the network",
            )
            return original(argv, **kwargs)

        with patch("catalog_source_lease.subprocess.run", side_effect=no_network):
            self.assertEqual(selected, module.selected_python_runtime(approved, authority))

    def test_unapproved_normal_call_cannot_invoke_the_owner(self):
        module = self.module()
        snapshot, roots, context, _selected = self.prepare()
        with patch.dict(os.environ, {"AGENTBOT_QUALIFICATION_CONTEXT": str(context)}):
            with self.assertRaises(ValueError):
                module.selected_python_runtime(snapshot, roots["dotfiles-shared"])
        self.assertFalse((self.directory / "sources").exists())

    def test_dirty_owner_cannot_invoke_the_helper(self):
        module = self.module()
        snapshot, roots, context, _selected = self.prepare()
        (roots["agentbot"] / "source.txt").write_text("dirty")
        with self.assertRaises(ValueError):
            module.selected_python_runtime(
                snapshot, roots["dotfiles-shared"], qualification_context=context
            )
        self.assertFalse((self.directory / "sources").exists())

    def test_changed_context_cannot_invoke_the_helper(self):
        module = self.module()
        snapshot, roots, context, _selected = self.prepare()
        data = json.loads(context.read_text())
        data["digest"] = "0" * 64
        context.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            module.selected_python_runtime(
                snapshot, roots["dotfiles-shared"], qualification_context=context
            )
        self.assertFalse((self.directory / "sources").exists())
