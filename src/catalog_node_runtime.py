"""Private Node/npm dependency using the qualified owner's installer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

from software_catalog import CatalogError, CatalogSnapshot, _read, load_snapshot

from .atomic_io import write_text_atomic
from .command_runner import CommandRunner


def private_directory(path: Path) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise CatalogError("private catalog cache cannot cross a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise CatalogError("catalog dependency cache must be private and owned")


@contextmanager
def _lease_lock(path: Path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise CatalogError("unsafe catalog dependency lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _git(root: Path, runner: CommandRunner, *arguments: str) -> str:
    result = runner.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-C",
            str(root),
            *arguments,
        ],
        timeout_seconds=180,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    if not result.ok:
        raise CatalogError("cannot lease the qualified catalog source: " + result.detail())
    return result.stdout.strip()


def _verify_source(name: str, root: Path, revision: str, runner: CommandRunner) -> None:
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise CatalogError("qualified source cannot cross a symlink")
    origin = _git(root, runner, "remote", "get-url", "origin").removesuffix(".git")
    if origin not in {
        f"https://github.com/PamuduW/{name}",
        f"git@github.com:PamuduW/{name}",
        f"ssh://git@github.com/PamuduW/{name}",
    }:
        raise CatalogError("qualified source origin differs from its public authority")
    if _git(root, runner, "rev-parse", "HEAD") != revision:
        raise CatalogError("qualified source revision changed")
    if _git(root, runner, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CatalogError("qualified source is not clean")


@contextmanager
def lease_catalog_sources(
    snapshot: CatalogSnapshot,
    authority: Path,
    runner: CommandRunner,
    *,
    qualification_sources: Mapping[str, Path] | None = None,
) -> Iterator[dict[str, Path]]:
    """Lease exact public source SHAs without modifying an operator checkout."""
    expected = snapshot.data["sources"]
    names = ("dotfiles", "dotfiles-shared")
    for name in names:
        if re.fullmatch(r"[0-9a-f]{40}", expected[name]) is None:
            raise CatalogError("qualified source needs an exact Git revision")
    if qualification_sources is not None:
        qualified_roots = {name: qualification_sources[name].resolve() for name in names}
        for name, root in qualified_roots.items():
            _verify_source(name, root, expected[name], runner)
        yield qualified_roots
        for name, root in qualified_roots.items():
            _verify_source(name, root, expected[name], runner)
        return
    if (
        not snapshot.approved
        or not snapshot.revision
        or not re.fullmatch(r"[0-9a-f]{40}", snapshot.revision)
    ):
        raise CatalogError("normal runtime provisioning requires an approved catalog revision")
    origin = _git(authority, runner, "remote", "get-url", "origin").removesuffix(".git")
    if origin not in {
        "https://github.com/PamuduW/dotfiles-shared",
        "git@github.com:PamuduW/dotfiles-shared",
        "ssh://git@github.com/PamuduW/dotfiles-shared",
    }:
        raise CatalogError("catalog source authority has an untrusted origin")
    main = _git(authority, runner, "rev-parse", "refs/remotes/origin/main")
    if re.fullmatch(r"[0-9a-f]{40}", main) is None:
        raise CatalogError("catalog authority lacks an exact approved main revision")
    _git(authority, runner, "merge-base", "--is-ancestor", snapshot.revision, main)
    roots: dict[str, Path] = {}
    with ExitStack() as leases:
        for name in names:
            candidate = authority if name == "dotfiles-shared" else authority.parent / name
            try:
                _verify_source(name, candidate, expected[name], runner)
            except CatalogError:
                cache = snapshot.directory.parent / "sources"
                private_directory(cache)
                leases.enter_context(_lease_lock(cache / f"{name}-{expected[name]}.lock"))
                candidate = cache / f"{name}-{expected[name]}"
                if not candidate.exists() and not candidate.is_symlink():
                    staging = Path(tempfile.mkdtemp(prefix=".source-", dir=cache))
                    try:
                        _git(staging, runner, "init", "--quiet")
                        _git(
                            staging,
                            runner,
                            "remote",
                            "add",
                            "origin",
                            f"https://github.com/PamuduW/{name}.git",
                        )
                        _git(staging, runner, "fetch", "--no-tags", "origin", expected[name])
                        _git(staging, runner, "checkout", "--quiet", "--detach", expected[name])
                        _verify_source(name, staging, expected[name], runner)
                        staging.rename(candidate)
                    finally:
                        if staging.exists():
                            shutil.rmtree(staging)
                _verify_source(name, candidate, expected[name], runner)
                if name == "dotfiles-shared":
                    # Preserve the verified authority's exact ref, not a floating lookup.
                    _git(candidate, runner, "fetch", "--no-tags", "origin", main)
                    _git(candidate, runner, "update-ref", "refs/remotes/origin/main", main)
                    _git(candidate, runner, "merge-base", "--is-ancestor", snapshot.revision, main)
            roots[name] = candidate
        yield roots
        for name, root in roots.items():
            _verify_source(name, root, expected[name], runner)


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
