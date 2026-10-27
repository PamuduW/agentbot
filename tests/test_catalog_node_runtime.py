from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import software_versions
from src.command_runner import CommandResult, CommandRunner


class PrivateNodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.public = self.root / "public"
        self.public.mkdir()
        (self.public / "node-marker").write_text("user runtime")
        public_bin = self.public / "bin"
        public_bin.mkdir()
        for command in ("node", "npm", "npx"):
            binary = public_bin / command
            binary.write_text('#!/bin/sh\ntouch "$HOME/external-runtime-executed"\nexit 91\n')
            binary.chmod(0o700)
        self.snapshot_dir = self.root / "cache/snapshot"
        self.snapshot_dir.mkdir(parents=True)
        self.sources = {}
        for name in ("dotfiles", "dotfiles-shared"):
            source = self.root / name
            source.mkdir()
            self.git(source, "init", "-q", "-b", "main")
            self.git(source, "remote", "add", "origin", f"https://github.com/PamuduW/{name}.git")
            (source / "marker").write_text(name)
            self.sources[name] = source
        helper = self.sources["dotfiles"] / "scripts/lib/catalog_runtimes.sh"
        helper.parent.mkdir(parents=True)
        helper.write_text("""dotfiles_catalog_node_install() {
    case ":$PATH:" in *:/usr/bin:*|*:/bin:*) return 95 ;; esac
    if command -v node >/dev/null || command -v npm >/dev/null || command -v npx >/dev/null; then return 96; fi
    mkdir -p "$HOME/.nvm/versions/node/v24.1.0/bin" "$HOME/.nvm/versions/node/v24.1.0/lib/node_modules/npm/bin" "$HOME/.local/bin" "$HOME/.local/share/dotfiles-catalog"
    local prefix="$HOME/.nvm/versions/node/v24.1.0"
    printf '%s\\n' '#!/bin/sh' 'if [ "$#" = 1 ]; then echo v24.1.0; else echo 11.1.0; fi' > "$prefix/bin/node"
    chmod 700 "$prefix/bin/node"
    printf 'npm fixture\\n' > "$prefix/lib/node_modules/npm/bin/npm-cli.js"
    ln -s ../lib/node_modules/npm/bin/npm-cli.js "$prefix/bin/npm"
    ln -s "$prefix/bin/node" "$HOME/.local/bin/node"
    ln -s "$prefix/bin/npm" "$HOME/.local/bin/npm"
    local key version
    for key in nodejs npm; do
        version=24.1.0; [ "$key" != npm ] || version=11.1.0
        printf 'native-runtime-v1\\nnvm\\n%s\\n%s\\n%s\\n%s\\n%s\\n' "$version" "$SOFTWARE_CATALOG_DIGEST" "$SOFTWARE_CATALOG_REVISION" "$prefix" "$TEST_RUNTIME_IDENTITY" > "$HOME/.local/share/dotfiles-catalog/$key.native"
    done
}
dotfiles_catalog_begin() { :; }
""")
        shared = self.sources["dotfiles-shared"] / "scripts/lib/shared/software_catalog.sh"
        shared.parent.mkdir(parents=True)
        shared.write_text("""software_catalog_begin() {
    [ -z "${AGENTBOT_QUALIFICATION_CONTEXT:-}" ] || return 93
    [ "$SOFTWARE_CATALOG_REVISION" = "$TEST_APPROVED_REF" ] || return 94
}
""")
        revisions = {}
        for name, source in self.sources.items():
            self.git(source, "add", ".")
            self.git(
                source,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            )
            revisions[name] = self.git(source, "rev-parse", "HEAD")
        shared = self.sources["dotfiles-shared"]
        self.git(shared, "update-ref", "refs/remotes/origin/main", revisions["dotfiles-shared"])
        from dataclasses import replace

        from software_catalog import load_snapshot

        from tests.test_software_catalog import SoftwareCatalogTests

        data = {
            "schema_version": 1,
            "generation": "fixture",
            "sources": {**revisions, "agentbot": "b" * 40},
            "required_targets": ["linux-amd64"],
            "auto_promote": False,
            "components": {},
        }
        for name, version in (("nodejs", "24.1.0"), ("npm", "11.1.0")):
            data["components"][name] = {
                "owner": "dotfiles",
                "kind": "software",
                "source": "fixture/" + name,
                "discovery": {},
                "requires": [],
                "blockers": [],
                "targets": {
                    "linux-amd64": {
                        "version": version,
                        "url": "https://example.invalid/fixture",
                        "digest": "sha256:" + "c" * 64,
                    }
                },
            }
        SoftwareCatalogTests.write(SimpleNamespace(directory=self.snapshot_dir, data=data))
        self.snapshot = replace(
            load_snapshot(self.snapshot_dir), revision=revisions["dotfiles-shared"]
        )
        self.rows = (self.snapshot_dir / "software.tsv").read_bytes().splitlines(keepends=True)
        identity = hashlib.sha256(
            b"".join(
                sorted(
                    row
                    for row in self.rows
                    if row.split(b"\t", 1)[0] in {b"nodejs", b"npm", b"nvm"}
                )
            )
        ).hexdigest()
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.public),
                "NVM_DIR": "/external/nvm",
                "PATH": str(public_bin) + os.pathsep + os.environ.get("PATH", ""),
                "TEST_RUNTIME_IDENTITY": identity,
                "TEST_APPROVED_REF": self.snapshot.revision,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.resolve = patch.object(
            software_versions,
            "resolve_entry",
            side_effect=lambda _, key, _target: {
                "version": {"nodejs": "24.1.0", "npm": "11.1.0"}[key]
            },
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def git(self, root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    def runtime(self, *, install=True, runner=None):
        from src.catalog_node_runtime import private_npm_runtime

        # The fixture runner only adds fixture controls to the sanitized child.
        class FixtureRunner(CommandRunner):
            def run(inner, argv, **kwargs):
                environment = kwargs.get("env")
                if environment is not None and argv[0] == "bash":
                    kwargs["env"] = {
                        **environment,
                        "TEST_RUNTIME_IDENTITY": os.environ["TEST_RUNTIME_IDENTITY"],
                        "TEST_APPROVED_REF": os.environ["TEST_APPROVED_REF"],
                    }
                return super().run(argv, **kwargs)

        return private_npm_runtime(
            self.snapshot,
            self.sources["dotfiles-shared"],
            runner or FixtureRunner(),
            install=install,
        )

    def test_agentbot_only_uses_qualified_helper_in_private_home_and_reuses_runtime(self):
        try:
            node, npm = self.runtime()
        except ImportError as error:
            self.fail(f"private catalog Node/npm provider is missing: {error}")
        self.assertTrue(node.is_file())
        self.assertTrue(npm.is_file())
        self.assertNotIn(self.public, node.parents)
        self.assertFalse((self.public / ".nvm").exists())
        self.assertFalse((self.public / "external-runtime-executed").exists())
        for command in ("node", "npm", "npx"):
            self.assertIn("exit 91", (self.public / "bin" / command).read_text())
        self.assertEqual("user runtime", (self.public / "node-marker").read_text())
        self.assertEqual((node, npm), self.runtime())

    def test_read_only_does_not_create_runtime_or_lease_sources(self):
        with self.assertRaisesRegex(ValueError, "private.*runtime|runtime.*missing"):
            self.runtime(install=False)
        self.assertFalse((self.snapshot_dir.parent / "node-runtime").exists())
        self.assertFalse((self.snapshot_dir.parent / "sources").exists())

    def test_wrong_source_revision_or_origin_cannot_run_helper(self):
        from dataclasses import replace

        data = {
            **self.snapshot.data,
            "sources": {**self.snapshot.data["sources"], "dotfiles": "f" * 40},
        }
        self.snapshot = replace(self.snapshot, data=data)
        from src.catalog_node_runtime import lease_catalog_sources

        class NoNetwork(CommandRunner):
            def run(inner, argv, **kwargs):
                if "fetch" in argv:
                    return CommandResult(2, stderr="fixture has no selected source revision")
                return super().run(argv, **kwargs)

        with self.assertRaises(ValueError):
            with lease_catalog_sources(self.snapshot, self.sources["dotfiles-shared"], NoNetwork()):
                self.fail("wrong checkout revision was leased")

    def test_runtime_tampering_is_rejected_without_adoption(self):
        node, _ = self.runtime()
        node.write_text(node.read_text() + "# modified\n")
        with self.assertRaisesRegex(ValueError, "integrity|differs"):
            self.runtime()

    def test_tampered_runtime_is_rejected_before_any_version_probe(self):
        import shlex

        node, _ = self.runtime()
        marker = self.public / "tampered-runtime-executed"
        first, remainder = node.read_text().split("\n", 1)
        node.write_text(first + "\ntouch " + shlex.quote(str(marker)) + "\n" + remainder)
        with self.assertRaisesRegex(ValueError, "integrity|differs"):
            self.runtime()
        self.assertFalse(marker.exists(), "unverified runtime executed before integrity check")

    def test_existing_unreceipted_private_home_is_not_adopted(self):
        from src.catalog_node_runtime import node_runtime_home

        home = node_runtime_home(self.snapshot)
        home.mkdir(mode=0o700, parents=True)
        (home / "foreign").write_text("keep")
        with self.assertRaisesRegex(ValueError, "unverified|unowned|receipt"):
            self.runtime()
        self.assertEqual("keep", (home / "foreign").read_text())

    def test_failed_helper_does_not_publish_owned_runtime(self):
        class FailureRunner(CommandRunner):
            def run(inner, argv, **kwargs):
                if argv[0] == "bash":
                    return CommandResult(27, stderr="fixture install failure")
                return super().run(argv, **kwargs)

        with self.assertRaisesRegex(ValueError, "provision|install"):
            self.runtime(runner=FailureRunner())
        from src.catalog_node_runtime import node_runtime_home

        self.assertFalse((node_runtime_home(self.snapshot).parent / "runtime.json").exists())

    def test_bootstrap_dependency_check_does_not_require_public_node_or_npx(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "bash",
                "-c",
                """
            export AGENTBOT_SOURCE_ONLY=1 DOTFILES_SHARED_DIR="$2"
            source "$1/install.sh"
            agentbot_python_ensure() { :; }
            check_python_deps() { :; }
            command() {
                if [[ "$*" == '-v node' || "$*" == '-v npx' ]]; then return 1; fi
                builtin command "$@"
            }
            check_skills_deps
        """,
                "deps",
                str(root),
                str(root.parent / "dotfiles-shared"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_normal_source_lease_rejects_unapproved_or_non_main_catalog(self):
        from dataclasses import replace

        from src.catalog_node_runtime import lease_catalog_sources

        for snapshot in (
            replace(self.snapshot, approved=False),
            replace(self.snapshot, revision="f" * 40),
        ):
            with self.subTest(snapshot=snapshot.revision, approved=snapshot.approved):
                with self.assertRaises(ValueError):
                    with lease_catalog_sources(
                        snapshot, self.sources["dotfiles-shared"], CommandRunner()
                    ):
                        self.fail("unapproved catalog revision was allowed")

    def test_explicit_qualification_source_origin_and_cleanliness_are_checked(self):
        from src.catalog_node_runtime import lease_catalog_sources

        self.git(
            self.sources["dotfiles"],
            "remote",
            "set-url",
            "origin",
            "https://example.invalid/foreign.git",
        )
        with self.assertRaisesRegex(ValueError, "origin"):
            with lease_catalog_sources(
                self.snapshot,
                self.sources["dotfiles-shared"],
                CommandRunner(),
                qualification_sources=self.sources,
            ):
                self.fail("foreign source was allowed")

    def test_cold_source_leases_fetch_only_exact_shas_and_preserve_public_sources(self):
        from src.catalog_node_runtime import lease_catalog_sources

        remote_dotfiles = self.root / "remote-dotfiles"
        self.sources["dotfiles"].rename(remote_dotfiles)
        authority = self.sources["dotfiles-shared"]
        (authority / "later").write_text("later approved authority")
        self.git(authority, "add", ".")
        self.git(
            authority,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "advance authority",
        )
        self.git(authority, "update-ref", "refs/remotes/origin/main", "HEAD")
        main = self.git(authority, "rev-parse", "HEAD")

        class LocalSources(CommandRunner):
            def run(inner, argv, **kwargs):
                argv = list(argv)
                if "fetch" in argv:
                    self.assertRegex(argv[-1], r"^[0-9a-f]{40}$")
                    checkout = Path(argv[argv.index("-C") + 1])
                    origin = self.git(checkout, "remote", "get-url", "origin")
                    repository = (
                        authority if origin.endswith("dotfiles-shared.git") else remote_dotfiles
                    )
                    argv[argv.index("origin")] = str(repository)
                return super().run(argv, **kwargs)

        with lease_catalog_sources(self.snapshot, authority, LocalSources()) as sources:
            self.assertIn(self.snapshot_dir.parent / "sources", sources["dotfiles"].parents)
            self.assertIn(self.snapshot_dir.parent / "sources", sources["dotfiles-shared"].parents)
            self.assertEqual(
                main, self.git(sources["dotfiles-shared"], "rev-parse", "refs/remotes/origin/main")
            )
            self.assertEqual(
                self.snapshot.data["sources"]["dotfiles-shared"],
                self.git(sources["dotfiles-shared"], "rev-parse", "HEAD"),
            )
        self.assertEqual(main, self.git(authority, "rev-parse", "HEAD"))
        (sources["dotfiles"] / "marker").write_text("changed cached source")
        with self.assertRaisesRegex(ValueError, "clean"):
            with lease_catalog_sources(self.snapshot, authority, LocalSources()):
                self.fail("dirty cached source was reused")

    def test_runtime_reuse_is_independent_of_public_home_and_unrelated_catalog_changes(self):
        from dataclasses import replace

        from software_catalog import load_snapshot

        from tests.test_software_catalog import SoftwareCatalogTests

        before = self.runtime()
        directory = self.snapshot_dir.parent / "other-snapshot"
        directory.mkdir()
        import json

        data = json.loads((self.snapshot_dir / "software.json").read_text())
        data["generation"] = "another-generation"
        SoftwareCatalogTests.write(SimpleNamespace(directory=directory, data=data))
        self.snapshot = replace(load_snapshot(directory), revision=self.snapshot.revision)
        with patch.dict(
            os.environ, {"HOME": str(self.root / "different-public-home"), "NVM_DIR": "/other/nvm"}
        ):
            self.assertEqual(before, self.runtime(install=False))

    def test_read_only_snapshot_requires_existing_approved_git_bound_cache(self):
        import shutil

        from software_catalog import freeze_approved_snapshot

        authority = self.sources["dotfiles-shared"]
        shutil.copytree(self.snapshot_dir, authority / "catalog")
        self.git(authority, "add", ".")
        self.git(
            authority,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "approved catalog",
        )
        revision = self.git(authority, "rev-parse", "HEAD")
        self.git(authority, "update-ref", "refs/remotes/origin/main", revision)
        xdg = self.root / "readonly-cache"
        with (
            patch.dict(
                os.environ, {"XDG_CACHE_HOME": str(xdg), "SOFTWARE_CATALOG_REVISION": revision}
            ),
            patch.object(software_versions, "shared_root", return_value=authority),
        ):
            with self.assertRaises(ValueError):
                software_versions.operation_snapshot(self.root, read_only=True)
            self.assertFalse(xdg.exists())
            frozen = freeze_approved_snapshot(authority, xdg / "software-catalog")
            before = sorted(xdg.rglob("*"))
            with patch.object(
                software_versions,
                "freeze_approved_snapshot",
                side_effect=AssertionError("read-only froze a cache"),
            ):
                observed = software_versions.operation_snapshot(self.root, read_only=True)
            self.assertEqual(frozen.digest, observed.digest)
            self.assertEqual(before, sorted(xdg.rglob("*")))
