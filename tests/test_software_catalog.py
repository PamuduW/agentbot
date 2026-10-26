from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import (
    _KINDS,
    _UNVERSIONED,
    CatalogError,
    freeze_approved_snapshot,
    load_snapshot,
    render_table,
    resolve_entry,
)


class SoftwareCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.data = {
            "schema_version": 1,
            "generation": "fixture-1",
            "sources": dict.fromkeys(("dotfiles", "agentbot", "dotfiles-shared"), "a" * 40),
            "required_targets": ["linux-amd64"],
            "auto_promote": False,
            "components": {
                "fixture": {
                    "owner": "dotfiles",
                    "kind": "software",
                    "source": "fixture/tool",
                    "discovery": {},
                    "requires": [],
                    "blockers": [],
                    "targets": {
                        "linux-amd64": {
                            "version": "1.2.3",
                            "url": "https://example.invalid/1.2.3/tool",
                            "digest": "sha256:" + "b" * 64,
                        }
                    },
                }
            },
        }

    def write(self, *, approve: bool = True) -> None:
        for name in ("approval", "approval.json"):
            (self.directory / name).unlink(missing_ok=True)
        raw = (json.dumps(self.data, indent=2, sort_keys=True) + "\n").encode()
        (self.directory / "software.json").write_bytes(raw)
        snapshot = load_snapshot(self.directory, require_approved=False, check_table=False)
        table = render_table(snapshot)
        (self.directory / "software.tsv").write_text(table)
        if approve:
            receipt = b'{"fixture":"test approval only"}\n'
            (self.directory / "approval.json").write_bytes(receipt)
            (self.directory / "approval").write_text(
                "\n".join(
                    (
                        "software-approved-v1",
                        hashlib.sha256(raw).hexdigest(),
                        hashlib.sha256(table.encode()).hexdigest(),
                        hashlib.sha256(receipt).hexdigest(),
                    )
                )
                + "\n"
            )

    def test_wsl_uses_same_architecture_linux_artifact_with_explicit_target_precedence(self):
        self.write()
        snapshot = load_snapshot(self.directory)
        self.assertEqual("1.2.3", resolve_entry(snapshot, "fixture", "wsl2-amd64")["version"])
        shared = shared_python_path(Path(__file__).resolve().parents[1]).parent
        command = [
            "bash",
            "-c",
            'source "$1/software_catalog.sh"; software_catalog_load "$2"; software_catalog_get fixture version "$3"',
            "artifact-target",
            str(shared),
            str(self.directory),
        ]
        result = subprocess.run(
            [*command, "wsl2-amd64"], capture_output=True, text=True, check=False
        )
        self.assertEqual((0, "1.2.3\n"), (result.returncode, result.stdout), result.stderr)
        for target in ("wsl2-arm64", "windows-amd64"):
            with self.assertRaises(CatalogError):
                resolve_entry(snapshot, "fixture", target)
            result = subprocess.run([*command, target], capture_output=True, text=True, check=False)
            self.assertNotEqual(0, result.returncode)
        self.data["components"]["fixture"]["targets"]["wsl2-amd64"] = {
            **self.data["components"]["fixture"]["targets"]["linux-amd64"],
            "version": "1.2.4",
        }
        self.write()
        snapshot = load_snapshot(self.directory)
        self.assertEqual("1.2.4", resolve_entry(snapshot, "fixture", "wsl2-amd64")["version"])
        result = subprocess.run(
            [*command, "wsl2-amd64"], capture_output=True, text=True, check=False
        )
        self.assertEqual((0, "1.2.4\n"), (result.returncode, result.stdout), result.stderr)

    def test_dependency_graph_rejects_cycles_and_duplicate_edges(self) -> None:
        self.data["components"]["second"] = json.loads(
            json.dumps(self.data["components"]["fixture"])
        )
        for first, second in (
            (["fixture"], []),
            (["second"], ["fixture"]),
            (["second", "second"], []),
        ):
            self.data["components"]["fixture"]["requires"] = first
            self.data["components"]["second"]["requires"] = second
            with self.subTest(first=first, second=second), self.assertRaises(CatalogError):
                self.write(approve=False)
        self.data["components"]["fixture"]["requires"] = ["second"]
        self.data["components"]["second"]["requires"] = []
        self.write(approve=False)
        self.assertFalse(load_snapshot(self.directory, require_approved=False).approved)

    def test_every_declared_classification_has_a_valid_schema_shape(self) -> None:
        """invariant: every catalog classification has an explicit valid representation."""
        self.assertGreaterEqual(len(_KINDS), 10)
        original = json.dumps(self.data)
        for kind in _KINDS:
            self.data = json.loads(original)
            entry = self.data["components"]["fixture"]
            entry["kind"] = kind
            if kind in _UNVERSIONED:
                entry["targets"] = {}
            elif kind == "git-source":
                entry["targets"] = {"any": {"revision": "a" * 40}}
            with self.subTest(kind=kind):
                self.write()
                self.assertEqual(
                    kind, load_snapshot(self.directory).data["components"]["fixture"]["kind"]
                )

    def test_approved_entry_returns_exact_version_and_frozen_data(self) -> None:
        self.write()
        snapshot = load_snapshot(self.directory)
        entry = resolve_entry(snapshot, "fixture", "linux-amd64")
        self.assertEqual("1.2.3", entry["version"])
        with self.assertRaises(TypeError):
            entry["version"] = "9.9.9"
        self.data["components"]["fixture"]["targets"]["linux-amd64"]["version"] = "9.9.9"
        self.assertEqual("1.2.3", resolve_entry(snapshot, "fixture", "linux-amd64")["version"])

    def test_a_candidate_is_not_an_approved_snapshot(self) -> None:
        self.write(approve=False)
        with self.assertRaisesRegex(CatalogError, "approval"):
            load_snapshot(self.directory)
        candidate = load_snapshot(self.directory, require_approved=False)
        with self.assertRaises(CatalogError):
            resolve_entry(candidate, "fixture", "linux-amd64")

    def test_modified_table_or_payload_is_rejected(self) -> None:
        self.write()
        table = self.directory / "software.tsv"
        table.write_text(table.read_text().replace("1.2.3", "9.9.9"))
        with self.assertRaises(CatalogError):
            load_snapshot(self.directory)
        self.write()
        raw = self.directory / "software.json"
        raw.write_text(raw.read_text().replace("1.2.3", "9.9.9"))
        with self.assertRaises(CatalogError):
            load_snapshot(self.directory)

    def test_unknown_fields_missing_integrity_and_control_characters_are_rejected(self) -> None:
        changes = (
            lambda d: d.update(surprise="unknown"),
            lambda d: d["components"]["fixture"]["targets"]["linux-amd64"].pop("digest"),
            lambda d: d["components"]["fixture"].update(source="bad\tfield"),
        )
        original = json.dumps(self.data)
        for change in changes:
            self.data = json.loads(original)
            change(self.data)
            with self.subTest(change=change), self.assertRaises(CatalogError):
                self.write()

    def test_unknown_component_target_and_blocked_capability_fail_closed(self) -> None:
        self.write()
        snapshot = load_snapshot(self.directory)
        for key, target in (("missing", "linux-amd64"), ("fixture", "windows-arm64")):
            with self.assertRaises(CatalogError):
                resolve_entry(snapshot, key, target)
        self.data["components"]["fixture"]["blockers"] = ["No supported updater control"]
        self.write()
        with self.assertRaisesRegex(CatalogError, "updater"):
            resolve_entry(load_snapshot(self.directory), "fixture", "linux-amd64")

    def test_bash_reader_matches_python_and_rejects_tampering(self) -> None:
        self.write()
        library = (
            shared_python_path(Path(__file__).resolve().parents[1]).parent / "software_catalog.sh"
        )
        command = [
            "bash",
            "-c",
            'source "$1"; software_catalog_load "$2"; software_catalog_get fixture version linux-amd64',
            "bash",
            str(library),
            str(self.directory),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("1.2.3", result.stdout.strip())
        table = self.directory / "software.tsv"
        table.write_text(table.read_text() + "fixture\tversion\tlinux-amd64\t9.9.9\n")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)

    def test_production_freeze_uses_approved_git_objects_not_working_files(self) -> None:
        artifact = self.data["components"]["fixture"]["targets"]["linux-amd64"]
        artifact.pop("url")
        artifact["lock"] = "locks/npm/package-lock.json"
        lock = self.directory / artifact["lock"]
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b'{"lockfileVersion":3}\n')
        artifact["digest"] = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
        self.write()
        repository = self.directory / "shared"
        subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", str(repository)], check=True)
        catalog = repository / "catalog"
        catalog.mkdir()
        for name in (
            "software.json",
            "software.tsv",
            "approval",
            "approval.json",
            artifact["lock"],
        ):
            (catalog / name).parent.mkdir(parents=True, exist_ok=True)
            (catalog / name).write_bytes((self.directory / name).read_bytes())
        for arguments in (
            ["config", "user.name", "Fixture"],
            ["config", "user.email", "fixture@example.invalid"],
            ["add", "."],
            ["commit", "-qm", "approved fixture"],
            ["update-ref", "refs/remotes/origin/main", "HEAD"],
            ["config", "remote.origin.url", "git@github.com:PamuduW/dotfiles-shared.git"],
        ):
            subprocess.run(["/usr/bin/git", "-C", str(repository), *arguments], check=True)
        (catalog / "software.json").write_text(
            (catalog / "software.json").read_text().replace("1.2.3", "9.9.9")
        )
        cache = self.directory / "cache"
        frozen = freeze_approved_snapshot(repository, cache)
        self.assertEqual("1.2.3", resolve_entry(frozen, "fixture", "linux-amd64")["version"])
        self.assertEqual(lock.read_bytes(), (frozen.directory / artifact["lock"]).read_bytes())
        import errno
        import shutil
        from unittest.mock import patch

        def another_process_publishes(staging, destination):
            shutil.copytree(staging, destination)
            raise OSError(errno.ENOTEMPTY, "another process published the snapshot")

        with patch.object(Path, "rename", another_process_publishes):
            raced = freeze_approved_snapshot(repository, self.directory / "racing-cache")
        self.assertEqual(frozen.digest, raced.digest)
        library = (
            shared_python_path(Path(__file__).resolve().parents[1]).parent / "software_catalog.sh"
        )
        command = [
            "bash",
            "-c",
            'source "$1"; software_catalog_begin "$2" "$3"; software_catalog_get fixture version linux-amd64',
            "bash",
            str(library),
            str(repository),
            str(cache),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("1.2.3", result.stdout.strip())
        racing_command = command.copy()
        racing_command[2] = (
            'source "$1"; mv() { command cp -a -- "$3" "$4"; return 1; }; '
            'software_catalog_begin "$2" "$3" && software_catalog_get fixture version linux-amd64'
        )
        racing_command[-1] = str(self.directory / "bash-racing-cache")
        raced_result = subprocess.run(racing_command, capture_output=True, text=True)
        self.assertEqual(0, raced_result.returncode, raced_result.stderr)
        self.assertEqual("1.2.3", raced_result.stdout.strip())

    def test_dependency_lock_requires_matching_bytes_and_safe_path(self) -> None:
        artifact = self.data["components"]["fixture"]["targets"]["linux-amd64"]
        artifact.pop("url")
        artifact["lock"] = "locks/npm/package-lock.json"
        lock = self.directory / artifact["lock"]
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b'{"lockfileVersion":3}\n')
        artifact["digest"] = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
        self.write()
        load_snapshot(self.directory)
        lock.write_bytes(b"modified")
        with self.assertRaisesRegex(CatalogError, "lock"):
            load_snapshot(self.directory)
        artifact["lock"] = "software.json"
        with self.assertRaisesRegex(CatalogError, "lock"):
            self.write()

    def test_symlinked_catalog_files_are_rejected(self) -> None:
        self.write()
        table = self.directory / "software.tsv"
        other = self.directory / "elsewhere"
        table.rename(other)
        table.symlink_to(other)
        with self.assertRaises(CatalogError):
            load_snapshot(self.directory)
