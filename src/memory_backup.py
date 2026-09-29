"""Back the vault up to a local mirror, and restore it into a fresh directory.

A backup destination holds generations, each a ``gen-*`` folder with
``local.git`` (a bare mirror of the vault's committed refs) and its
``manifest.json``, and a ``current`` symlink naming the live one. Each backup
builds a whole new generation, verifies it (object integrity, refs against the
source, and a trial clone that passes the vault validator), writes its
manifest, and only then switches ``current`` in one atomic rename. A backup
that fails or is interrupted at any point before that leaves the previous
generation as it was; older generations are removed after the switch. A
destination from before generations (``local.git`` and ``manifest.json`` at
its top) is read as is and converted by its next backup.

A successful backup records its destination in the machine's memory
configuration, and every ``agentbot update`` refreshes that backup
(``refresh``), so it stays at most one update behind without a scheduler.

Git carries committed history only: uncommitted edits and ignored exports are
never in a snapshot, and every report says so. Restore
clones into a new or empty private directory, removes the backup ``origin``,
validates the result, and touches nothing else: not the active checkout, not
Agentbot's configuration, not a remote. There is no derived index to rebuild:
retrieval reads the restored files directly.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import SCANNER, MemoryVaultError, read_marker, validate, vault_id

MANIFEST_VERSION = 1
MIRROR = "local.git"
MANIFEST = "manifest.json"
CURRENT = "current"
GENERATION = re.compile(r"gen-[0-9a-f]{32}")
ASSURANCES = ("unknown", "user-attested")
LIMITATIONS = (
    "uncommitted edits, ignored exports, and per-device Obsidian files are not in a Git snapshot",
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
    return {
        "path": str(vault.resolve()),
        "root_commits": sorted(roots),
        "vault_id": vault_id(vault),
    }


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


def current_generation(backup: Path) -> Path:
    """The folder holding the live mirror and manifest."""
    link = backup / CURRENT
    if not link.is_symlink():
        return backup  # the layout from before generations
    name = os.readlink(link)
    generation = backup / name
    if not GENERATION.fullmatch(name) or generation.is_symlink() or not generation.is_dir():
        raise MemoryVaultError(f"{backup} is not an Agentbot memory backup")
    return generation


def read_manifest(backup: Path) -> dict[str, Any]:
    generation = current_generation(backup)
    path = generation / MANIFEST
    if path.is_symlink() or not path.is_file() or not (generation / MIRROR).is_dir():
        raise MemoryVaultError(f"{backup} is not an Agentbot memory backup")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryVaultError(f"{path} is unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise MemoryVaultError(f"{path} has an unsupported version")
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
    # A first backup interrupted before its switch leaves only its own
    # generation behind: that is still an empty destination.
    if destination.exists() and any(
        not GENERATION.fullmatch(child.name) and child.name != f".{CURRENT}.new"
        for child in destination.iterdir()
    ):
        previous = read_manifest(destination).get("source")
        # The vault is its history and its ID, not its folder: a clone moved to
        # a new path (the workspace copy to ~/agent-memory) keeps refreshing the
        # same backup. A backup from before the ID was recorded has only history.
        if (
            not isinstance(previous, dict)
            or previous.get("root_commits") != identity["root_commits"]
            or previous.get("vault_id", identity["vault_id"]) != identity["vault_id"]
        ):
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
    generation = destination / f"gen-{uuid.uuid4().hex}"
    _private_dir(generation)
    published = False
    try:
        mirror = generation / MIRROR
        clone = _run("clone", "--quiet", "--mirror", "--no-hardlinks", str(vault), str(mirror))
        if clone.returncode != 0:
            raise MemoryVaultError("git clone --mirror failed")
        result.findings = _verify(mirror, result.refs)
        _write_json(generation / MANIFEST, _manifest(result, identity, assurance))
        _publish(destination, generation)
        published = True
    finally:
        if not published:
            shutil.rmtree(generation, ignore_errors=True)
    _retire(destination, keep=generation)
    result.state = (
        "completed-with-warnings" if result.changed_paths or result.findings else "completed"
    )
    return result


def _manifest(result: BackupResult, identity: dict[str, Any], assurance: str) -> dict[str, Any]:
    return {
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
    }


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


def _publish(destination: Path, generation: Path) -> None:
    """Point ``current`` at a complete generation: one rename, never half done."""
    link = destination / f".{CURRENT}.new"
    with contextlib.suppress(FileNotFoundError):
        link.unlink()
    os.symlink(generation.name, link)
    os.replace(link, destination / CURRENT)


def _retire(destination: Path, *, keep: Path) -> None:
    """Remove every generation but the live one, and the old flat layout."""
    for child in destination.iterdir():
        stale = (GENERATION.fullmatch(child.name) and child != keep) or child.name in {
            MIRROR,
            MANIFEST,
            f".{MIRROR}.new",
            f".{MIRROR}.old",
        }
        if not stale:
            continue
        with contextlib.suppress(OSError):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()


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
    mirror = current_generation(source) / MIRROR
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


def refresh(agentbot_root: Path, config_home: Path) -> tuple[str, str]:
    """Refresh the recorded backup for the converge pass: (detail, result).

    Never raises: a backup that cannot be refreshed is reported for the user
    and the previous snapshot stays as it was.
    """
    from . import memory, memory_setup

    try:
        recorded = memory_setup.recorded_backup(config_home)
        if recorded is None:
            return "no backup recorded (agentbot memory backup --destination PATH --yes)", "skipped"
        root, _ = memory.find_vault(agentbot_root, config_home=config_home)
        if root is None:
            return "no memory vault configured", "skipped"
        destination = Path(recorded["destination"])
        result = backup(root, destination, apply=True, assurance=recorded["assurance"])
    except (MemoryVaultError, OSError) as error:
        return f"not refreshed: {error}", "check"
    if result.state == "completed":
        return f"refreshed {destination}", "ok"
    return f"refreshed {destination} with warnings; run agentbot memory backup", "check"
