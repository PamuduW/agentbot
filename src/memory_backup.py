"""Back the vault up to a local mirror, and restore it into a fresh directory.

A backup destination holds ``local.git`` (a bare mirror of the vault's
committed refs) and ``manifest.json``. Each backup builds a new mirror beside
the old one, verifies it (object integrity, refs against the source, and a
trial clone that passes the vault validator), and only then replaces the
previous snapshot and writes the manifest atomically. A failed backup leaves
the previous snapshot as it was.

Git carries committed history only: uncommitted edits, ignored drafts, and
ignored exports are never in a snapshot, and every report says so. Restore
clones into a new or empty private directory, removes the backup ``origin``,
validates the result, and touches nothing else: not the active checkout, not
Agentbot's configuration, not a remote. There is no derived index to rebuild:
retrieval reads the restored files directly.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import SCANNER, MemoryVaultError, read_marker, validate

MANIFEST_VERSION = 1
MIRROR = "local.git"
MANIFEST = "manifest.json"
ASSURANCES = ("unknown", "user-attested")
LIMITATIONS = (
    "uncommitted edits, ignored drafts, and ignored exports are not in a Git snapshot",
    "remote snapshots are not implemented; only the local checkout's refs are captured",
)


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MemoryVaultError("git is unavailable or did not respond") from error


def _git(repo: Path, *args: str) -> str:
    result = _run("-C", str(repo), *args)
    if result.returncode != 0:
        raise MemoryVaultError(f"git {args[0]} failed in {repo.name}")
    return result.stdout.strip()


def _refs(repo: Path) -> dict[str, str]:
    """Branches and tags only: what a restore can check out."""
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")
    refs: dict[str, str] = {}
    for line in out.splitlines():
        name, _, sha = line.partition(" ")
        if name:
            refs[name] = sha
    return refs


def source_identity(vault: Path) -> dict[str, Any]:
    roots = _git(vault, "rev-list", "--max-parents=0", "HEAD").split()
    return {"path": str(vault.resolve()), "root_commits": sorted(roots)}


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _check_destination(vault: Path, destination: Path) -> None:
    """Refuse a destination that could alias, contain, or sit inside the vault."""
    if destination.is_symlink():
        raise MemoryVaultError("the destination is a symlink")
    resolved, source = destination.resolve(), vault.resolve()
    if _inside(resolved, source) or _inside(source, resolved):
        raise MemoryVaultError("the destination must be outside the vault and must not contain it")
    if resolved in {Path("/"), Path.home().resolve()}:
        raise MemoryVaultError(
            "the destination cannot be the filesystem root or the home directory"
        )


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with open(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600),
        "w",
        encoding="utf-8",
    ) as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_manifest(backup: Path) -> dict[str, Any]:
    path = backup / MANIFEST
    if path.is_symlink() or not path.is_file() or not (backup / MIRROR).is_dir():
        raise MemoryVaultError(f"{backup} is not an Agentbot memory backup")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryVaultError(f"{backup / MANIFEST} is unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise MemoryVaultError(f"{backup / MANIFEST} has an unsupported version")
    return manifest


# --- Backup ------------------------------------------------------------------


@dataclass
class BackupResult:
    state: str  # preview, completed, completed-with-warnings
    destination: Path
    refs: dict[str, str] = field(default_factory=dict)
    head: str | None = None
    changed_paths: int = 0
    findings: int = 0
    notes: list[str] = field(default_factory=list)


def _dirty_count(vault: Path) -> int:
    return len(_git(vault, "status", "--porcelain=v1", "--untracked-files=normal").splitlines())


def backup(
    vault: Path, destination: Path, *, apply: bool = False, assurance: str = "unknown"
) -> BackupResult:
    if assurance not in ASSURANCES:
        raise MemoryVaultError(
            "encryption assurance must be unknown or user-attested; no verification backend exists"
        )
    read_marker(vault)
    _check_destination(vault, destination)
    identity = source_identity(vault)
    if destination.exists() and any(destination.iterdir()):
        previous = read_manifest(destination)
        if previous.get("source") != identity:
            raise MemoryVaultError(
                "this backup belongs to a different source vault; choose a new destination"
            )
    result = BackupResult(state="preview", destination=destination)
    result.refs = _refs(vault)
    result.head = _git(vault, "rev-parse", "HEAD")
    result.changed_paths = _dirty_count(vault)
    result.notes.extend(LIMITATIONS)
    if result.changed_paths:
        result.notes.append(f"{result.changed_paths} uncommitted path(s) are not included")
    if not apply:
        return result

    _private_dir(destination)
    staging = destination / f".{MIRROR}.new"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        clone = _run("clone", "--quiet", "--mirror", "--no-hardlinks", str(vault), str(staging))
        if clone.returncode != 0:
            raise MemoryVaultError("git clone --mirror failed")
        result.findings = _verify(staging, result.refs)
        _swap(destination, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _write_json(
        destination / MANIFEST,
        {
            "version": MANIFEST_VERSION,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "encryption_assurance": assurance,
            "source": identity,
            "head": result.head,
            "refs": result.refs,
            "dirty": result.changed_paths > 0,
            "excluded_uncommitted_paths": result.changed_paths,
            "verification": "fsck, refs, trial clone",
            "validation_findings": result.findings,
            "scanner": SCANNER,
            "remote": "not implemented",
        },
    )
    result.state = (
        "completed-with-warnings" if result.changed_paths or result.findings else "completed"
    )
    return result


def _verify(mirror: Path, expected: dict[str, str]) -> int:
    """Integrity, refs, and a trial restore. Returns the restored tree's finding count."""
    if _run("-C", str(mirror), "fsck", "--full", "--no-dangling").returncode != 0:
        raise MemoryVaultError("the new mirror failed git fsck; the previous snapshot is unchanged")
    if _refs(mirror) != expected:
        raise MemoryVaultError(
            "the new mirror's refs differ from the source; the previous snapshot is unchanged"
        )
    trial = mirror.parent / ".trial-restore"
    shutil.rmtree(trial, ignore_errors=True)
    try:
        if _run("clone", "--quiet", "--no-hardlinks", str(mirror), str(trial)).returncode != 0:
            raise MemoryVaultError("a trial clone of the new mirror failed")
        return len(validate(trial).findings)
    finally:
        shutil.rmtree(trial, ignore_errors=True)


def _swap(destination: Path, staging: Path) -> None:
    current, retired = destination / MIRROR, destination / f".{MIRROR}.old"
    shutil.rmtree(retired, ignore_errors=True)
    if current.exists():
        os.replace(current, retired)
    os.replace(staging, current)
    shutil.rmtree(retired, ignore_errors=True)


# --- Restore -----------------------------------------------------------------


@dataclass
class RestoreResult:
    state: str  # preview, restored, restored-with-findings
    destination: Path
    head: str | None
    branch: str | None = None
    findings: int = 0
    notes: list[str] = field(default_factory=list)


def restore(
    source: Path, destination: Path, *, active: Path | None = None, apply: bool = False
) -> RestoreResult:
    manifest = read_manifest(source)
    if destination.is_symlink():
        raise MemoryVaultError("the destination is a symlink")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise MemoryVaultError("restore needs a new or empty destination; it never overwrites")
    resolved = destination.resolve()
    if _inside(resolved, source.resolve()):
        raise MemoryVaultError("the destination must be outside the backup")
    if active is not None and (
        _inside(resolved, active.resolve()) or _inside(active.resolve(), resolved)
    ):
        raise MemoryVaultError("the destination must be outside the active vault")
    mirror = source / MIRROR
    head_ref = _run("-C", str(mirror), "symbolic-ref", "--short", "HEAD").stdout.strip() or None
    result = RestoreResult(
        state="preview", destination=destination, head=manifest.get("head"), branch=head_ref
    )
    result.notes.append("the restored copy has no remote; the production remote stays unset")
    result.notes.append("hooks are not installed; run agentbot memory hook install after review")
    result.notes.extend(LIMITATIONS[:1])
    if not apply:
        return result
    created = not destination.exists()
    try:
        clone = _run("clone", "--quiet", "--no-hardlinks", str(mirror), str(destination))
        if clone.returncode != 0:
            raise MemoryVaultError("git clone from the backup failed")
        os.chmod(destination, 0o700)
        _git(destination, "remote", "remove", "origin")
        if _git(destination, "rev-parse", "HEAD") != manifest.get("head"):
            result.notes.append("the restored HEAD differs from the manifest's recorded HEAD")
        read_marker(destination)
        result.findings = len(validate(destination).findings)
    except BaseException:
        # Remove only what this restore created.
        _discard(destination, keep_directory=not created)
        raise
    result.state = "restored-with-findings" if result.findings else "restored"
    return result


def _discard(destination: Path, *, keep_directory: bool) -> None:
    """Remove a failed restore's output: only what the restore created."""
    if not keep_directory:
        shutil.rmtree(destination, ignore_errors=True)
        return
    for child in list(destination.iterdir()):
        with contextlib.suppress(OSError):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()


def backup_json(result: BackupResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "destination": str(result.destination),
        "head": result.head,
        "refs": len(result.refs),
        "changed_paths": result.changed_paths,
        "validation_findings": result.findings,
        "notes": result.notes,
    }


def restore_json(result: RestoreResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "destination": str(result.destination),
        "head": result.head,
        "branch": result.branch,
        "validation_findings": result.findings,
        "notes": result.notes,
    }
