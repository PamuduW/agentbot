"""Resolve one approved software generation for an Agentbot operation."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import write_text_atomic
from .shared_paths import shared_python_path, shared_root

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import CatalogError, CatalogSnapshot, freeze_approved_snapshot
from software_catalog import resolve_entry as _resolve_entry


@dataclass(frozen=True)
class _QualificationOperation:
    snapshot: CatalogSnapshot
    agentbot_root: Path


_qualification: ContextVar[_QualificationOperation | None] = ContextVar(
    "catalog_qualification", default=None
)


def resolve_entry(snapshot: CatalogSnapshot, identity: str, target: str) -> Mapping[str, str]:
    context = _qualification.get()
    explicit_candidate = context is not None and context.snapshot is snapshot
    return _resolve_entry(snapshot, identity, target, require_approved=not explicit_candidate)


@contextmanager
def qualification_operation(directory: Path, digest: str, sources: Mapping[str, Path]):
    """Dedicated qualification drivers opt in explicitly; normal operations cannot inherit it."""
    from catalog_context import qualification_snapshot

    if _qualification.get() is not None:
        raise CatalogError("a qualification operation is already active")
    snapshot = qualification_snapshot(directory, digest, sources)
    context = _QualificationOperation(snapshot, sources["agentbot"].resolve())
    token = _qualification.set(context)
    try:
        yield snapshot
        qualification_snapshot(directory, digest, sources)
    finally:
        _qualification.reset(token)


@contextmanager
def qualification_manifest_operation(manifest: Path, root: Path):
    from catalog_context import load_qualification_context

    snapshot, sources, _digest = load_qualification_context(manifest)
    if root.resolve() != sources["agentbot"]:
        raise CatalogError("qualification CLI requires its frozen Agentbot checkout")
    with qualification_operation(snapshot.directory, snapshot.digest, sources) as operation:
        yield operation


def operation_target(release: str | None = None) -> str:
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    if architecture is None:
        raise CatalogError("unsupported software catalog architecture")
    kernel = platform.release() if release is None else release
    system = "wsl2" if "microsoft" in kernel.lower() else "linux"
    return f"{system}-{architecture}"


def operation_snapshot(root: Path) -> CatalogSnapshot:
    qualification = _qualification.get()
    if qualification is not None:
        if root.resolve() != qualification.agentbot_root:
            raise CatalogError("qualification operation requires its frozen Agentbot source")
        return qualification.snapshot
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "software-catalog"
    snapshot = freeze_approved_snapshot(
        shared_root(root), cache, revision=os.environ.get("SOFTWARE_CATALOG_REVISION") or None
    )
    os.environ["SOFTWARE_CATALOG_REVISION"] = snapshot.revision or ""
    return snapshot


def approved_artifact(root: Path, identity: str, *, target: str | None = None) -> Mapping[str, str]:
    return resolve_entry(operation_snapshot(root), identity, target or operation_target())


def _npm_manifest(lock: dict, package: str, version: str) -> dict:
    import json

    from catalog_dependencies import validate_npm_lock

    return validate_npm_lock(json.dumps(lock).encode(), package, version)


def _approved_npm_runtime(snapshot: CatalogSnapshot, runner) -> tuple[Path, Path]:
    import hashlib
    import re

    from software_catalog import _read

    target = operation_target()
    versions = {key: resolve_entry(snapshot, key, target)["version"] for key in ("nodejs", "npm")}
    rows = _read(snapshot.directory / "software.tsv", 16_777_216).splitlines(keepends=True)
    identity = hashlib.sha256(
        b"".join(
            sorted(row for row in rows if row.split(b"\t", 1)[0] in {b"nodejs", b"npm", b"nvm"})
        )
    ).hexdigest()
    prefix = (
        Path(os.environ.get("NVM_DIR", str(Path.home() / ".nvm")))
        / "versions/node"
        / ("v" + versions["nodejs"])
    )
    node, npm = prefix / "bin/node", prefix / "lib/node_modules/npm/bin/npm-cli.js"
    try:
        for key, binary in (("nodejs", "node"), ("npm", "npm")):
            receipt = Path.home() / ".local/share/dotfiles-catalog" / (key + ".native")
            if not receipt.exists():
                raise OSError("missing runtime receipt")
            proof = _read(receipt, 4096).decode().splitlines()
            link = Path.home() / ".local/bin" / binary
            if (
                receipt.is_symlink()
                or receipt.stat().st_uid != os.geteuid()
                or len(proof) != 7
                or proof[:3] != ["native-runtime-v1", "nvm", versions[key]]
                or re.fullmatch(r"[0-9a-f]{64}", proof[3]) is None
                or re.fullmatch(r"[0-9a-f]{40}", proof[4]) is None
                or proof[5:] != [str(prefix), identity]
                or prefix.resolve() != prefix
                or not link.is_symlink()
                or os.readlink(link) != str(prefix / "bin" / binary)
                or link.resolve() != (node if key == "nodejs" else npm)
            ):
                raise CatalogError("Node/npm ownership differs from the selected catalog")
        if node.is_symlink() or not os.access(node, os.X_OK) or not npm.is_file():
            raise CatalogError("selected Node/npm runtime is missing")
        environment = _npm_environment()
        for argv, expected in (
            ([str(node), "--version"], "v" + versions["nodejs"]),
            ([str(node), str(npm), "--version"], versions["npm"]),
        ):
            result = runner.run(argv, timeout_seconds=30, env=environment)
            if not result.ok or result.stdout.strip() != expected:
                raise CatalogError("physical Node/npm versions differ from the selected catalog")
    except (OSError, UnicodeError) as error:
        raise CatalogError("Install the selected Node/npm runtime with Dotfiles first") from error
    return node, npm


def _npm_environment() -> dict[str, str]:
    environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


def _npm_launcher(node: Path, executable: Path) -> str:
    import shlex

    return (
        "#!/bin/sh\nunset NODE_OPTIONS NODE_PATH\nexec "
        + shlex.quote(str(node))
        + " "
        + shlex.quote(str(executable))
        + ' "$@"\n'
    )


def approved_npm_executable(root: Path, identity: str, runner, *, install: bool = True) -> Path:
    """Install a hash-bound closure in a private cache; never ask npm for latest."""
    import json
    import shutil
    import tempfile

    snapshot = operation_snapshot(root)
    artifact = resolve_entry(snapshot, identity, operation_target())
    package, version, binary = (
        artifact.get(field, "") for field in ("package", "version", "binary")
    )
    if (
        not package
        or not binary
        or "/" in binary
        or binary in (".", "..")
        or "lock" not in artifact
    ):
        raise CatalogError("approved npm CLI needs package, binary and frozen lock")
    lock = snapshot.directory / artifact["lock"]
    manifest = _npm_manifest(json.loads(lock.read_text()), package, version)
    node, npm = _approved_npm_runtime(snapshot, runner)
    cache = snapshot.directory.parent / "npm"
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.is_symlink() or cache.stat().st_uid != os.geteuid() or cache.stat().st_mode & 0o077:
        raise CatalogError("npm catalog cache must be private and owned")
    destination = cache / f"{identity}-{snapshot.digest}"
    executable = destination / "bin" / binary
    launcher = _npm_launcher(node, destination / "node_modules/.bin" / binary)
    if not install:
        return executable
    if destination.exists() or destination.is_symlink():
        _verify_npm_install(destination, package, version, binary, lock)
        if (
            executable.is_symlink()
            or executable.read_text() != launcher
            or not os.access(executable, os.X_OK)
        ):
            raise CatalogError("npm launcher differs from the selected runtime")
        return executable
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=cache))
    try:
        (staging / "package-lock.json").write_bytes(lock.read_bytes())
        write_text_atomic(staging / "package.json", json.dumps(manifest) + "\n")
        global_config = staging / "global.npmrc"
        write_text_atomic(global_config, "")
        environment = _npm_environment()
        environment.update(
            {
                "NPM_CONFIG_USERCONFIG": "/dev/null",
                "NPM_CONFIG_GLOBALCONFIG": str(global_config),
            }
        )
        result = runner.run(
            [str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=staging,
            timeout_seconds=300,
            env=environment,
        )
        if not result.ok:
            raise CatalogError("cannot install approved npm dependency closure")
        _verify_npm_install(staging, package, version, binary, lock)
        (staging / "bin").mkdir()
        write_text_atomic(staging / "bin" / binary, launcher)
        (staging / "bin" / binary).chmod(0o700)
        try:
            staging.rename(destination)
        except FileExistsError:
            _verify_npm_install(destination, package, version, binary, lock)
            if executable.is_symlink() or executable.read_text() != launcher:
                raise CatalogError("npm launcher differs from the selected runtime") from None
        return executable
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _verify_npm_install(
    directory: Path, package: str, version: str, binary: str, lock: Path
) -> None:
    import json

    if directory.is_symlink() or not directory.is_dir():
        raise CatalogError("unsafe npm catalog cache")
    executable = directory / "node_modules/.bin" / binary
    if (
        not executable.is_file()
        or not os.access(executable, os.X_OK)
        or directory.resolve() not in executable.resolve().parents
        or (directory / "package-lock.json").read_bytes() != lock.read_bytes()
        or json.loads((directory / "node_modules" / package / "package.json").read_text()).get(
            "version"
        )
        != version
    ):
        raise CatalogError("npm installation differs from the approved lock")


def approved_source_revision(root: Path, repo: str) -> str:
    snapshot = operation_snapshot(root)
    matches = [
        key
        for key, entry in snapshot.data["components"].items()
        if entry["owner"] == "agentbot"
        and entry["kind"] == "git-source"
        and entry["source"] == repo
    ]
    if len(matches) != 1:
        raise CatalogError("skill source does not have one approved catalog identity")
    return resolve_entry(snapshot, matches[0], operation_target())["revision"]


def preflight_skill_sources(root: Path, config) -> None:
    snapshot = operation_snapshot(root)
    cli = resolve_entry(snapshot, "skills_cli", operation_target())
    if "lock" not in cli or "package" not in cli:
        raise CatalogError("Skills CLI requires a frozen npm dependency closure")
    import json

    _npm_manifest(
        json.loads((snapshot.directory / cli["lock"]).read_text()), cli["package"], cli["version"]
    )
    for source in config.active_sources():
        identity = "skill-source." + source.id
        metadata = snapshot.data["components"].get(identity)
        if metadata is None or metadata["source"] != source.repo:
            raise CatalogError(f"skill manifest source is not approved: {source.id}")
        resolve_entry(snapshot, identity, operation_target())
