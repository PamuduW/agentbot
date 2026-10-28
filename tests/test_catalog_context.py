from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_context import qualification_snapshot
from software_catalog import CatalogError, load_snapshot

from . import test_software_catalog as fixtures


class QualificationContextTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write

    def sources(self):
        roots = {}
        for name in self.data["sources"]:
            root = self.directory / name
            root.mkdir()
            (root / "source.txt").write_text(name)
            for args in (
                ["init", "-q", "-b", "main"],
                ["config", "user.name", "Fixture"],
                ["config", "user.email", "fixture@example.invalid"],
                ["add", "."],
                ["commit", "-qm", "frozen source fixture"],
                ["config", "remote.origin.url", f"https://github.com/PamuduW/{name}.git"],
            ):
                subprocess.run(["/usr/bin/git", "-C", str(root), *args], check=True)
            self.data["sources"][name] = subprocess.check_output(
                ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            roots[name] = root
        return roots

    def test_qualification_rejects_tracked_edits_hidden_by_the_index(self):
        roots = self.sources()
        root = roots["agentbot"]
        subprocess.run(
            ["/usr/bin/git", "-C", str(root), "update-index", "--assume-unchanged", "source.txt"],
            check=True,
        )
        (root / "source.txt").write_text("unqualified source")
        self.write(approve=False)
        digest = load_snapshot(self.directory, require_approved=False).digest
        with self.assertRaises(CatalogError):
            qualification_snapshot(self.directory, digest, roots)

    def test_candidate_requires_exact_digest_and_clean_frozen_sources_without_approval(
        self,
    ) -> None:
        roots = self.sources()
        self.write(approve=False)
        digest = load_snapshot(self.directory, require_approved=False).digest
        candidate = qualification_snapshot(self.directory, digest, roots)
        self.assertFalse(candidate.approved)
        self.assertFalse((self.directory / "approval").exists())
        from src.software_versions import operation_snapshot, qualification_operation, resolve_entry

        with qualification_operation(self.directory, digest, roots) as operation:
            self.assertIs(operation, operation_snapshot(roots["agentbot"]))
            self.assertFalse(operation.approved)
            self.assertEqual("1.2.3", resolve_entry(operation, "fixture", "linux-amd64")["version"])
            from src.skill_catalog import discover_remote_catalogs
            from src.skills_sources import SkillSourceEntry, SkillsSourcesConfig

            config = SkillsSourcesConfig(
                1,
                ["codex"],
                "global",
                [
                    SkillSourceEntry("first", "owner/first", ["*"]),
                    SkillSourceEntry("second", "owner/second", ["*"]),
                ],
            )

            def inspect_in_worker(_repo, checkout):
                self.assertIs(operation, operation_snapshot(roots["agentbot"]))
                skill = checkout / "fixture/SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text("# fixture\n")

            catalogs = discover_remote_catalogs(
                config, clone_source=inspect_in_worker, revision_reader=lambda _: "a" * 40
            )
            self.assertEqual(2, len(catalogs))
            with self.assertRaises(CatalogError):
                operation_snapshot(roots["dotfiles"])
        with self.assertRaises(CatalogError):
            resolve_entry(operation, "fixture", "linux-amd64")
        with self.assertRaises(CatalogError):
            qualification_snapshot(self.directory, "0" * 64, roots)
        with self.assertRaises(CatalogError):
            qualification_snapshot(self.directory, digest, {})
        (roots["agentbot"] / "source.txt").write_text("changed after freeze")
        with self.assertRaisesRegex(CatalogError, "source"):
            qualification_snapshot(self.directory, digest, roots)

    def test_aggregate_cli_emits_only_complete_bound_evidence_and_never_approval(self):
        roots = self.sources()
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        receipt = {
            "catalog_digest": snapshot.digest,
            "sources": dict(snapshot.data["sources"]),
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
        path = self.directory / "result.json"
        path.write_text(json.dumps(receipt))
        command = [
            sys.executable,
            str(
                shared_python_path(Path(__file__).resolve().parents[1]).parents[2]
                / "catalog/qualify.py"
            ),
            str(self.directory),
            "--digest",
            snapshot.digest,
            "--result",
            str(path),
            "--run-id",
            "123",
        ]
        for name, root in roots.items():
            command.extend(["--source", f"{name}={root}"])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(snapshot.digest, json.loads(completed.stdout)["catalog_digest"])
        self.assertFalse((self.directory / "approval").exists())
        receipt["scenarios"]["upgrade"] = "skipped"
        path.write_text(json.dumps(receipt))
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stdout)
        self.assertFalse((self.directory / "approval").exists())

    def test_explicit_bash_candidate_context_keeps_normal_operations_approved_only(self):
        roots = self.sources()
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "qualification.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        shared = shared_python_path(Path(__file__).resolve().parents[1]).parent
        script = """
set -euo pipefail
source "$1/software_catalog.sh"
if software_catalog_begin "$3" "$4" >/dev/null 2>&1; then exit 95; fi
if ! declare -F software_catalog_qualification_begin >/dev/null; then
    echo 'missing explicit Bash qualification context' >&2; exit 94
fi
software_catalog_qualification_begin "$2"
software_catalog_begin "$3" "$4"
software_catalog_get fixture version linux-amd64
[[ ! -e "$5/approval" && ! -e "$5/approval.json" ]]
"""
        completed = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "fixture",
                str(shared),
                str(manifest),
                str(roots["dotfiles-shared"]),
                str(self.directory / "cache"),
                str(self.directory),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("1.2.3\n", completed.stdout)

    def test_dotfiles_candidate_operation_reloads_explicit_context_without_fake_approval(self):
        roots = self.sources()
        self.data["components"]["fixture"]["targets"]["any"] = self.data["components"]["fixture"][
            "targets"
        ].pop("linux-amd64")
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "qualification.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        core = Path(__file__).resolve().parents[2] / "dotfiles/scripts/lib/software_catalog.sh"
        script = """
set -euo pipefail
source "$1"
DOTFILES_SHARED_ROOT="$3"
software_catalog_qualification_begin "$2"
dotfiles_catalog_begin
dotfiles_catalog_begin
dotfiles_catalog_get fixture version
[[ ! -e "$4/approval" ]]
printf ' ' >>"$2"
if dotfiles_catalog_begin; then echo 'changed manifest accepted'; exit 97; fi
if software_catalog_get fixture version any; then echo 'rejected data remained accessible'; exit 98; fi
"""
        completed = subprocess.run(
            [
                "bash",
                "-c",
                script,
                "fixture",
                str(core),
                str(manifest),
                str(roots["dotfiles-shared"]),
                str(self.directory),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("1.2.3\n", completed.stdout)

    def test_python_cli_qualification_is_explicit_and_clears_context_after_command(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from src import cli
        from src.software_versions import operation_snapshot, resolve_entry

        roots = self.sources()
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "qualification.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        observed = []

        def handler(context):
            active = operation_snapshot(Path(context.args.root))
            observed.append(active)
            self.assertEqual("1.2.3", resolve_entry(active, "fixture", "linux-amd64")["version"])
            return 0

        with (
            patch.object(
                sys,
                "argv",
                [
                    "agentbot",
                    "--root",
                    str(roots["agentbot"]),
                    "--qualification-context",
                    str(manifest),
                    "status",
                ],
            ),
            patch.object(
                cli, "default_paths", return_value=SimpleNamespace(root=roots["agentbot"])
            ),
            patch.object(cli, "Diagnostics"),
            patch.object(cli, "Lifecycle"),
            patch.dict(cli.COMMAND_HANDLERS, {"status": handler}),
        ):
            self.assertEqual(0, cli._run())
        self.assertEqual(1, len(observed))
        with self.assertRaises(CatalogError):
            resolve_entry(observed[0], "fixture", "linux-amd64")

    def test_shell_entrypoints_forward_only_an_explicit_qualification_context(self):
        roots = self.sources()
        self.data["components"]["fixture"]["targets"]["any"] = self.data["components"]["fixture"][
            "targets"
        ].pop("linux-amd64")
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "qualification.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        workspace = Path(__file__).resolve().parents[2]
        launcher = """
set -euo pipefail
AGENTBOT_SOURCE_ONLY=1 source "$1/agentbot/bin/agentbot"
agentbot_run_backend() { printf '%s\n' "$@"; }
agentbot_main --qualification-context "$2" status
"""
        completed = subprocess.run(
            ["bash", "-c", launcher, "fixture", str(workspace), str(manifest)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(f"--qualification-context\n{manifest}\nstatus\n", completed.stdout)
        dotfiles = """
set -euo pipefail
DOTFILES_SOURCE_ONLY=1 source "$1/dotfiles/bin/bin/dotfiles"
source "$1/dotfiles/scripts/lib/software_catalog.sh"
DOTFILES_DIR="$3"
DOTFILES_SHARED_ROOT="$4"
dotfiles_load_command() { :; }
qualified_status() { dotfiles_catalog_get fixture version; }
DOTFILES_COMMAND_HANDLERS[status]=qualified_status
main --qualification-context "$2" status
"""
        completed = subprocess.run(
            [
                "bash",
                "-c",
                dotfiles,
                "fixture",
                str(workspace),
                str(manifest),
                str(roots["dotfiles"]),
                str(roots["dotfiles-shared"]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("1.2.3\n", completed.stdout)


class FrozenRepositoryGateTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write
    sources = QualificationContextTests.sources

    def test_qualified_repository_gate_accepts_frozen_detached_sources_and_rejects_drift(self):
        roots = self.sources()
        for root in roots.values():
            subprocess.run(
                ["/usr/bin/git", "-C", str(root), "checkout", "--detach", "-q"], check=True
            )
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "gate-context.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        shared = shared_python_path(Path(__file__).resolve().parents[1]).parent
        command = [
            "bash",
            "-c",
            'set -euo pipefail; source "$1/software_catalog.sh"; source "$1/repo_update.sh"; source "$1/repo_gate.sh"; '
            'software_catalog_qualification_begin "$2"; declare -A result=(); '
            'repo_gate_frozen "$3" dotfiles result "Dotfiles fixture"; '
            '[[ "${result[reason]}" == qualification-frozen && "${result[outcome]}" == current ]]; '
            'printf changed >>"$3/source.txt"; '
            'if repo_gate_frozen "$3" dotfiles result "Dotfiles fixture"; then exit 99; fi; '
            '[[ "${result[safe]}" == 0 ]]',
            "frozen-gate",
            str(shared),
            str(manifest),
            str(roots["dotfiles"]),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_both_product_repository_gates_use_the_verified_qualification_source(self):
        roots = self.sources()
        for root in roots.values():
            subprocess.run(
                ["/usr/bin/git", "-C", str(root), "checkout", "--detach", "-q"], check=True
            )
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        manifest = self.directory / "product-context.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog": str(self.directory),
                    "digest": snapshot.digest,
                    "sources": {name: str(root) for name, root in roots.items()},
                }
            )
        )
        shared = shared_python_path(Path(__file__).resolve().parents[1]).parent
        workspace = Path(__file__).resolve().parents[2]
        script = (
            'set -euo pipefail; DOTFILES_SHARED_LIB="$1"; DOTFILES_DIR="$7/dotfiles"; REPO_ROOT="$7/agentbot"; '
            'source "$1/software_catalog.sh"; source "$7/$6/scripts/lib/repo_update.sh"; '
            'software_catalog_qualification_begin "$2"; DOTFILES_SHARED_ROOT="$5"; DOTFILES_DIR="$4"; REPO_ROOT="$3"; '
            "reject() { return 99; }; "
            'if [[ "$6" == dotfiles ]]; then declare -A result=(); dotfiles_repo_gate reject result; '
            '[[ "${result[reason]}" == qualification-frozen ]]; '
            'else outcome_value=""; reason_value=""; agentbot_repo_gate reject outcome_value reason_value; '
            '[[ "$outcome_value" == current && "$reason_value" == qualification-frozen ]]; fi'
        )
        for owner in ("dotfiles", "agentbot"):
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    script,
                    "qualified-product",
                    str(shared),
                    str(manifest),
                    str(roots["agentbot"]),
                    str(roots["dotfiles"]),
                    str(roots["dotfiles-shared"]),
                    owner,
                    str(workspace),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(0, result.returncode, owner + ": " + result.stderr)
