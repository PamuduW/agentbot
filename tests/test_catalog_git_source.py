from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import unittest

from . import test_catalog_context as contexts
from . import test_software_catalog as fixtures


class CatalogGitSourceTests(unittest.TestCase):
    sources = contexts.QualificationContextTests.sources

    def setUp(self):
        fixtures.SoftwareCatalogTests.setUp(self)
        self.root = self.sources()["agentbot"]
        self.revision = self.git("rev-parse", "HEAD")

    def git(self, *arguments):
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(self.root), *arguments], text=True
        ).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "source fixture")
        self.revision = self.git("rev-parse", "HEAD")

    def verify(self, **kwargs):
        return importlib.import_module("catalog_git_source").verify_checkout(
            self.root, self.revision, **kwargs
        )

    def test_clean_source_and_ignored_cache_policy(self):
        self.verify()
        (self.root / ".gitignore").write_text("cache/\n")
        self.commit()
        (self.root / "cache").mkdir()
        (self.root / "cache/generated").write_text("runtime data")
        self.verify()
        with self.assertRaises(ValueError):
            self.verify(allow_ignored=False)

    def test_assume_unchanged_cannot_hide_modified_bytes(self):
        self.git("update-index", "--assume-unchanged", "source.txt")
        (self.root / "source.txt").write_text("modified source")
        with self.assertRaises(ValueError):
            self.verify()

    def test_staged_change_is_rejected_even_if_worktree_matches_head(self):
        source = self.root / "source.txt"
        original = source.read_bytes()
        source.write_text("staged source")
        self.git("add", "source.txt")
        source.write_bytes(original)
        with self.assertRaises(ValueError):
            self.verify()

    def test_clean_filter_is_never_executed(self):
        (self.root / ".gitattributes").write_text("source.txt filter=legacy\n")
        self.commit()
        marker = self.directory / "filter-executed"
        self.git("config", "filter.legacy.clean", "touch " + shlex.quote(str(marker)) + "; cat")
        source = self.root / "source.txt"
        metadata = source.stat()
        source.write_bytes(b"modified")
        os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000))
        with self.assertRaises(ValueError):
            self.verify()
        self.assertFalse(marker.exists())

    def test_fsmonitor_is_never_executed(self):
        marker = self.directory / "fsmonitor-executed"
        hook = self.directory / "fsmonitor"
        hook.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        self.verify()
        self.assertFalse(marker.exists())

    def test_nonignored_untracked_file_is_rejected(self):
        (self.root / "unqualified.py").write_text("untracked")
        with self.assertRaises(ValueError):
            self.verify()

    def test_executable_bit_and_symlink_replacement_are_rejected(self):
        source = self.root / "source.txt"
        original_mode = source.stat().st_mode
        source.chmod(original_mode | 0o100)
        with self.assertRaises(ValueError):
            self.verify()
        source.chmod(original_mode)
        outside = self.directory / "outside"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.verify()

    def test_tracked_symlink_hash_uses_its_literal_target(self):
        link = self.root / "shortcut"
        link.symlink_to("source.txt")
        self.commit()
        self.verify()
        link.unlink()
        link.symlink_to("./source.txt")
        with self.assertRaises(ValueError):
            self.verify()

    def test_owner_execute_bit_cannot_be_replaced_by_group_or_other_execute(self):
        script = self.root / "run"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.commit()
        script.chmod(0o655)
        with self.assertRaises(ValueError):
            self.verify()

    def test_group_execute_bit_does_not_change_a_nonexecutable_git_mode(self):
        source = self.root / "source.txt"
        source.chmod(0o654)
        self.verify()

    def test_uninitialized_gitlink_requires_an_empty_physical_placeholder(self):
        self.git("update-index", "--add", "--cacheinfo", "160000", "a" * 40, "fixture")
        self.git("commit", "-qm", "uninitialized source fixture")
        self.revision = self.git("rev-parse", "HEAD")
        self.verify()
        placeholder = self.root / "fixture"
        placeholder.mkdir()
        self.verify()
        (placeholder / "unqualified.py").write_text("unverified submodule code")
        with self.assertRaises(ValueError):
            self.verify()
