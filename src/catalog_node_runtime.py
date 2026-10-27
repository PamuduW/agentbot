"""Private Node/npm dependency using the qualified owner's installer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from catalog_source_lease import _lease_lock, lease_catalog_sources, private_directory
from software_catalog import CatalogError, CatalogSnapshot, _read, load_snapshot

from .atomic_io import write_text_atomic
from .command_runner import CommandRunner


def node_runtime_home(snapshot: CatalogSnapshot) -> Path:
    from .software_versions import operation_target

    rows = _read(snapshot.directory / "software.tsv", 16_777_216).splitlines(keepends=True)
    identity = hashlib.sha256(
        b"".join(
            sorted(row for row in rows if row.split(b"\t", 1)[0] in {b"nodejs", b"npm", b"nvm"})
        )
    ).hexdigest()
    return snapshot.directory.parent / "node-runtime" / f"{operation_target()}-{identity}" / "home"


def _fingerprint(node: Path, npm: Path) -> str:
    prefix = node.parent.parent
    digest = hashlib.sha256()
    for path in (node, *sorted(npm.parents[1].rglob("*"))):
        if path.is_symlink() and not path.resolve().is_relative_to(prefix):
            raise CatalogError("private Node/npm payload escapes its runtime")
        if path.is_file():
            digest.update(path.relative_to(prefix).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def installer_path(home: Path) -> str:
    """Expose OS prerequisites without admitting public language runtimes."""
    tools = home.parent / "tools"
    private_directory(tools)
    for name in (
        "bash",
        "python3",
        "uname",
        "dirname",
        "readlink",
        "realpath",
        "head",
        "grep",
        "tr",
        "chmod",
        "mkdir",
        "cp",
        "rm",
        "mv",
        "ln",
        "stat",
        "sha256sum",
        "sha512sum",
        "mktemp",
        "env",
        "tar",
        "gzip",
        "cat",
        "awk",
        "sort",
        "flock",
        "xz",
        "timeout",
        "find",
        "sed",
        "basename",
        "wc",
        "id",
        "install",
        "git",
        "curl",
        "tee",
        "cut",
        "touch",
        "getconf",
    ):
        source = Path("/usr/bin") / name
        if not source.is_file() or not os.access(source, os.X_OK):
            raise CatalogError(f"private runtime installer requires OS prerequisite: {name}")
        link = tools / name
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or os.readlink(link) != str(source):
                raise CatalogError("private installer tool path is externally managed")
        else:
            link.symlink_to(source)
    return str(home / ".local/bin") + os.pathsep + str(tools)


def private_npm_runtime(
    snapshot: CatalogSnapshot,
    authority: Path,
    runner: CommandRunner,
    *,
    qualification_sources: Mapping[str, Path] | None = None,
    install: bool = True,
) -> tuple[Path, Path]:
    from .software_versions import _approved_npm_runtime, _npm_environment, _owned_npm_runtime

    current = load_snapshot(snapshot.directory, require_approved=qualification_sources is None)
    if current.digest != snapshot.digest:
        raise CatalogError("private runtime catalog snapshot changed")
    home = node_runtime_home(snapshot)
    receipt = home.parent / "runtime.json"
    if home.is_symlink() or any(parent.is_symlink() for parent in home.parents):
        raise CatalogError("private runtime cannot cross a symlink")
    if home.exists():
        if home.stat().st_uid != os.geteuid() or home.stat().st_mode & 0o077:
            raise CatalogError("private runtime home must be private and owned")
        try:
            proof = json.loads(_read(receipt, 4096))
        except (OSError, ValueError) as error:
            raise CatalogError(
                "preserving an unverified private runtime without its receipt"
            ) from error
        if receipt.stat().st_uid != os.geteuid():
            raise CatalogError("private runtime receipt is unowned")
        node, npm = _owned_npm_runtime(snapshot, home=home)
        if proof != {"schema_version": 1, "integrity": _fingerprint(node, npm)}:
            raise CatalogError("private Node/npm runtime integrity differs from its receipt")
        return _approved_npm_runtime(snapshot, runner, home=home)
    if not install:
        raise CatalogError("selected private Node/npm runtime is missing; run skills install")
    if receipt.exists() or receipt.is_symlink():
        raise CatalogError("preserving an inconsistent private runtime receipt")
    private_directory(home.parent)
    with _lease_lock(home.parent / "install.lock"):
        if home.exists():
            return private_npm_runtime(
                snapshot,
                authority,
                runner,
                qualification_sources=qualification_sources,
                install=False,
            )
        with lease_catalog_sources(
            snapshot, authority, runner, qualification_sources=qualification_sources
        ) as sources:
            context = home.parent / "qualification-context.json"
            environment = _npm_environment()
            environment.update(
                {
                    "HOME": str(home),
                    "PATH": installer_path(home),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                    "DOTFILES_DIR": str(sources["dotfiles"]),
                    "DOTFILES_SHARED_ROOT": str(sources["dotfiles-shared"]),
                    "DOTFILES_SHARED_DIR": str(sources["dotfiles-shared"]),
                    "SOFTWARE_CATALOG_REVISION": snapshot.revision
                    or snapshot.data["sources"]["dotfiles-shared"],
                    "SOFTWARE_CATALOG_DIGEST": snapshot.digest,
                }
            )
            script = 'set -euo pipefail; umask 077; source "$DOTFILES_SHARED_ROOT/scripts/lib/shared/software_catalog.sh"; source "$DOTFILES_DIR/scripts/lib/catalog_runtimes.sh"; '
            if qualification_sources is not None:
                from catalog_context import qualification_snapshot

                qualification_snapshot(snapshot.directory, snapshot.digest, qualification_sources)
                write_text_atomic(
                    context,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "catalog": str(snapshot.directory),
                            "digest": snapshot.digest,
                            "sources": {
                                key: str(path) for key, path in qualification_sources.items()
                            },
                        }
                    ),
                )
                context.chmod(0o600)
                script += 'software_catalog_qualification_begin "$1"; '
            else:
                script += 'software_catalog_begin "$DOTFILES_SHARED_ROOT"; '
            script += '[[ "$SOFTWARE_CATALOG_DIGEST" == "$2" ]]; dotfiles_catalog_node_install'
            private_directory(home)
            try:
                outcome = runner.run(
                    ["bash", "-c", script, "catalog-node", str(context), snapshot.digest],
                    timeout_seconds=900,
                    env=environment,
                )
                if not outcome.ok:
                    raise CatalogError(
                        "cannot provision the selected private Node/npm runtime: "
                        + outcome.detail()
                    )
                node, npm = _approved_npm_runtime(snapshot, runner, home=home)
                if (
                    load_snapshot(
                        snapshot.directory, require_approved=qualification_sources is None
                    ).digest
                    != snapshot.digest
                ):
                    raise CatalogError("runtime catalog snapshot changed during provisioning")
                if qualification_sources is not None:
                    qualification_snapshot(
                        snapshot.directory, snapshot.digest, qualification_sources
                    )
                proof = {"schema_version": 1, "integrity": _fingerprint(node, npm)}
            except BaseException:
                shutil.rmtree(home)
                raise
            finally:
                context.unlink(missing_ok=True)
        write_text_atomic(receipt, json.dumps(proof) + "\n")
        receipt.chmod(0o600)
        return node, npm
