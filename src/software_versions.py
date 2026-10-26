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
    cache = snapshot.directory.parent / "npm"
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.is_symlink() or cache.stat().st_uid != os.geteuid() or cache.stat().st_mode & 0o077:
        raise CatalogError("npm catalog cache must be private and owned")
    destination = cache / f"{identity}-{snapshot.digest}"
    executable = destination / "node_modules/.bin" / binary
    if not install:
        return executable
    if destination.exists() or destination.is_symlink():
        _verify_npm_install(destination, package, version, binary, lock)
        return executable
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=cache))
    try:
        (staging / "package-lock.json").write_bytes(lock.read_bytes())
        write_text_atomic(staging / "package.json", json.dumps(manifest) + "\n")
        result = runner.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=staging,
            timeout_seconds=300,
        )
        if not result.ok:
            raise CatalogError("cannot install approved npm dependency closure")
        _verify_npm_install(staging, package, version, binary, lock)
        try:
            staging.rename(destination)
        except FileExistsError:
            _verify_npm_install(destination, package, version, binary, lock)
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
