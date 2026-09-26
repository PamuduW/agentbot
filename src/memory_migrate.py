"""Migrate a schema 1 vault to schema 2 through a reviewed mapping.

``plan`` inspects a v1 vault that validates clean and proposes one stable
UUID per record and pending draft. ``preferences.md`` is global and
``projects/<slug>/**`` is project-scoped by rule; every other record's scope
is left for a human to choose, with a suggestion beside it. The mapping holds
paths, hashes, IDs, and scopes, never note bodies, and is written only to a
private (``0600``) file outside the vault.

``check`` converts an isolated copy of the tree and runs complete-tree v2
validation on it. ``apply`` repeats that, takes a verified byte snapshot of
every file it will touch, and then, under the promotion lock, rewrites each
file only while its bytes still match the mapping, changing the marker last.
While it runs, ``.meta/migration-in-progress`` makes every validation (and so
every read) fail closed. Any failure restores the original bytes wherever the
live bytes are still the ones this migration wrote; a concurrent human edit is
never overwritten. Nothing is staged, committed, or pushed.

Only front matter changes: ``schema: 1`` becomes ``schema: 2`` followed by the
``id`` line, and a ``scope`` line follows ``status``. Paths, titles, bodies,
statuses, projects, tags, and links are unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic_io import write_text_atomic
from .memory import (
    MARKER,
    MAX_FILE_BYTES,
    MIGRATION_IN_PROGRESS,
    SCOPES,
    UUID4,
    MemoryVaultError,
    _git_text,
    _listed_paths,
    _Unsafe,
    read_bounded,
    read_marker,
    validate,
)
from .memory_approve import _promotion_lock, lock_path

MAPPING_VERSION = 1
V2_MARKER = b'{ "agentbot_memory_schema": 2 }\n'
TEMPLATE_PREFIX = "templates/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _suggest(record_type: str, projects: tuple[str, ...]) -> str:
    if record_type == "preference":
        return "global"
    if record_type == "project":
        return "project"
    if len(projects) == 1:
        return "project"
    return "shared" if projects else "global"


def plan(root: Path) -> dict[str, Any]:
    """A mapping to review. Fixed scopes are set; the rest are null until chosen."""
    if read_marker(root) != 1:
        raise MemoryVaultError("only a schema 1 vault can be migrated")
    report = validate(root)
    if report.findings:
        raise MemoryVaultError(
            f"the v1 tree has {len(report.findings)} finding(s); run agentbot memory validate "
            "and fix them before planning a migration"
        )
    entries = []
    for record in sorted(report.records, key=lambda item: item.path):
        fixed = record.type in {"preference", "project"}
        entries.append(
            {
                "path": record.path,
                "sha256": _sha(read_bounded(root, record.path, MAX_FILE_BYTES)),
                "id": str(uuid.uuid4()),
                "type": record.type,
                "draft": record.draft,
                "projects": list(record.projects),
                "suggested": _suggest(record.type, record.projects),
                "scope": _suggest(record.type, record.projects) if fixed else None,
            }
        )
    templates = [
        {"path": path, "sha256": _sha(read_bounded(root, path, MAX_FILE_BYTES))}
        for path in _listed_paths(root)
        if path.startswith(TEMPLATE_PREFIX)
        and path.endswith(".md")
        and not path.endswith("README.md")
    ]
    return {
        "version": MAPPING_VERSION,
        "vault": str(root.resolve()),
        "head": _git_text(root, "rev-parse", "HEAD"),
        "marker_sha256": _sha(read_bounded(root, MARKER, 4096)),
        "entries": entries,
        "templates": templates,
    }


def write_mapping(mapping: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise MemoryVaultError(f"{path} exists; choose a new mapping file")
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(mapping, stream, indent=2)
        stream.write("\n")


def read_mapping(path: Path) -> dict[str, Any]:
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryVaultError(f"{path} is not a readable mapping") from error
    if not isinstance(mapping, dict) or mapping.get("version") != MAPPING_VERSION:
        raise MemoryVaultError(f"{path} has an unsupported mapping version")
    return mapping


# --- Checking a mapping --------------------------------------------------------


@dataclass
class MigrationReport:
    state: str  # unresolved, stale, invalid, ready, applied, rolled-back
    problems: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    snapshot: Path | None = None


def _convert(text: str, record_id: str | None, scope: str | None) -> str:
    """Rewrite the v1 front matter lines that v2 changes; leave all else as is."""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None)
    if lines[0].rstrip("\r") != "---" or end is None:
        raise MemoryVaultError("front matter is not delimited")
    out = [lines[0]]
    schema_seen = status_seen = False
    for line in lines[1:end]:
        bare = line.rstrip("\r")
        ending = line[len(bare) :]
        if bare.strip() == "schema: 1":
            out.append("schema: 2" + ending)
            if record_id is not None:
                out.append(f"id: {record_id}".rstrip() + ending)
            schema_seen = True
            continue
        out.append(line)
        if bare.startswith("status:") and scope is not None:
            out.append(f"scope: {scope}".rstrip() + ending)
            status_seen = True
    if not schema_seen or (scope is not None and not status_seen):
        raise MemoryVaultError("front matter lacks a 'schema: 1' or 'status:' line")
    return "\n".join([*out, *lines[end:]])


def _targets(
    root: Path, mapping: dict[str, Any], report: MigrationReport
) -> dict[str, tuple[bytes, bytes]]:
    """path -> (original bytes, converted bytes), checked against the mapping."""
    if read_marker(root) != 1:
        raise MemoryVaultError("the vault is not schema 1; nothing to migrate")
    if Path(mapping.get("vault", "")).resolve() != root.resolve():
        raise MemoryVaultError("this mapping was made for another vault")
    current = {record.path for record in validate(root).records}
    mapped = {entry["path"] for entry in mapping["entries"]}
    for path in sorted(current - mapped):
        report.problems.append(f"{path} is not in the mapping; plan again")
    for path in sorted(mapped - current):
        report.problems.append(f"{path} is in the mapping but not a valid record now")
    ids = [entry.get("id") for entry in mapping["entries"]]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and UUID4.match(i) for i in ids):
        report.problems.append("the mapping's ids must be unique lowercase UUID version 4")
    converted: dict[str, tuple[bytes, bytes]] = {}
    for entry in mapping["entries"]:
        if entry.get("scope") not in SCOPES:
            report.unresolved.append(entry["path"])
            continue
        _add(root, entry["path"], entry["sha256"], entry["id"], entry["scope"], converted, report)
    # Templates get blank id and scope lines: a record made from one fails
    # validation until a person fills them, rather than guessing either.
    for entry in mapping.get("templates", []):
        _add(root, entry["path"], entry["sha256"], "", "", converted, report)
    return converted


def _add(
    root: Path,
    path: str,
    digest: str,
    record_id: str | None,
    scope: str | None,
    converted: dict[str, tuple[bytes, bytes]],
    report: MigrationReport,
) -> None:
    try:
        original = read_bounded(root, path, MAX_FILE_BYTES)
    except (_Unsafe, OSError) as error:
        report.problems.append(f"{path} cannot be read: {type(error).__name__}")
        return
    if _sha(original) != digest:
        report.problems.append(f"{path} changed since the mapping was made")
        return
    if path.startswith(TEMPLATE_PREFIX) and b"\nschema: 1" not in original:
        return  # a template without v1 front matter has nothing to convert
    try:
        text = _convert(original.decode("utf-8"), record_id, scope)
    except MemoryVaultError as error:
        report.problems.append(f"{path}: {error}")
        return
    converted[path] = (original, text.encode("utf-8"))


def _candidate(root: Path, converted: dict[str, tuple[bytes, bytes]]) -> Path:
    """An isolated copy of every listed and draft file, converted, with a v2 marker."""
    scratch = Path(tempfile.mkdtemp(prefix="agentbot-migrate-"))
    os.chmod(scratch, 0o700)
    candidate = scratch / "vault"
    subprocess.run(["git", "init", "-q", str(candidate)], check=True, capture_output=True)
    for path in _listed_paths(root):
        if path.startswith(".obsidian/"):
            continue
        source = root / path
        if source.is_symlink() or not source.is_file():
            continue
        target = candidate / path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = converted[path][1] if path in converted else source.read_bytes()
        target.write_bytes(V2_MARKER if path == MARKER else data)
    return candidate


def check(
    root: Path, mapping: dict[str, Any]
) -> tuple[MigrationReport, dict[str, tuple[bytes, bytes]]]:
    report = MigrationReport(state="ready")
    converted = _targets(root, mapping, report)
    if report.problems:
        report.state = "stale"
        return report, converted
    if report.unresolved:
        report.state = "unresolved"
        return report, converted
    candidate = _candidate(root, converted)
    try:
        findings = validate(candidate).findings
    finally:
        shutil.rmtree(candidate.parent, ignore_errors=True)
    for item in findings:
        report.problems.append(f"{item.path}: {item.rule}: {item.message}")
    if findings:
        report.state = "invalid"
    report.changed = sorted(converted)
    return report, converted


# --- Apply and rollback ------------------------------------------------------


def _snapshot(root: Path, destination: Path, converted: dict[str, tuple[bytes, bytes]]) -> None:
    """Original and converted bytes of every touched file, verified after writing."""
    if destination.exists() or destination.is_symlink():
        raise MemoryVaultError(f"{destination} exists; the snapshot needs a new directory")
    resolved = destination.resolve()
    if resolved == root.resolve() or root.resolve() in resolved.parents:
        raise MemoryVaultError("the snapshot must be outside the vault")
    destination.mkdir(mode=0o700, parents=True)
    marker = read_bounded(root, MARKER, 4096)
    files = {**converted, MARKER: (marker, V2_MARKER)}
    manifest = {}
    for index, (path, (original, new)) in enumerate(sorted(files.items())):
        name = f"{index:05}"
        (destination / f"{name}.orig").write_bytes(original)
        (destination / f"{name}.new").write_bytes(new)
        manifest[path] = {"file": name, "original": _sha(original), "converted": _sha(new)}
    write_text_atomic(
        destination / "manifest.json",
        json.dumps({"vault": str(root.resolve()), "files": manifest}, indent=2) + "\n",
    )
    for child in destination.iterdir():
        os.chmod(child, 0o600)
    for path, entry in read_snapshot(destination)["files"].items():
        if _sha((destination / f"{entry['file']}.orig").read_bytes()) != entry["original"]:
            raise MemoryVaultError(f"the snapshot of {path} did not verify")


def read_snapshot(snapshot: Path) -> dict[str, Any]:
    try:
        return json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryVaultError(f"{snapshot} is not a migration snapshot") from error


def _replace(root: Path, path: str, expected: str, data: bytes) -> bool:
    """Write data over path only while its bytes hash to expected."""
    try:
        current = read_bounded(root, path, MAX_FILE_BYTES)
    except (_Unsafe, OSError):
        return False
    if _sha(current) != expected:
        return False
    temporary = root / f"{path}.agentbot-migrate.tmp"
    with open(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644), "wb"
    ) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / path)
    return True


def _clean_tree(root: Path) -> None:
    if _git_text(root, "symbolic-ref", "-q", "HEAD") is None:
        raise MemoryVaultError("HEAD is detached; check out the vault's branch first")
    if _git_text(root, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise MemoryVaultError("the vault has uncommitted changes; commit or stash them first")


def apply(
    root: Path,
    mapping: dict[str, Any],
    *,
    snapshot: Path,
    config_home: Path,
    confirm: bool = False,
    after_file: Any = None,
) -> MigrationReport:
    _clean_tree(root)
    report, converted = check(root, mapping)
    if report.state != "ready" or not confirm:
        return report
    report.snapshot = snapshot
    _snapshot(root, snapshot, converted)
    notes: list[str] = []
    guard = root / MIGRATION_IN_PROGRESS
    written: list[str] = []
    with _promotion_lock(lock_path(config_home, root), 5.0, notes):
        handle = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(handle)
        try:
            for path, (original, new) in sorted(converted.items()):
                if not _replace(root, path, _sha(original), new):
                    raise MemoryVaultError(f"{path} changed during migration; nothing was kept")
                written.append(path)
                if after_file is not None:
                    after_file(path)
            marker = read_bounded(root, MARKER, 4096)
            if _sha(marker) != mapping["marker_sha256"] or not _replace(
                root, MARKER, _sha(marker), V2_MARKER
            ):
                raise MemoryVaultError("the vault marker changed during migration")
            written.append(MARKER)
            guard.unlink()
            findings = validate(root).findings
            if findings:
                guard.touch()
                raise MemoryVaultError(f"the migrated vault has {len(findings)} finding(s)")
        except BaseException as error:
            report.problems.extend(_undo(root, snapshot, written))
            with contextlib.suppress(FileNotFoundError):
                guard.unlink()
            if isinstance(error, MemoryVaultError):
                report.state = "rolled-back"
                report.problems.insert(0, str(error))
                return report
            raise
    report.state = "applied"
    report.changed = written
    return report


def _undo(root: Path, snapshot: Path, paths: list[str]) -> list[str]:
    """Restore original bytes where the live bytes are still what this migration wrote."""
    files = read_snapshot(snapshot)["files"]
    problems = []
    for path in reversed(paths):
        entry = files[path]
        original = (snapshot / f"{entry['file']}.orig").read_bytes()
        if not _replace(root, path, entry["converted"], original):
            problems.append(
                f"{path} was edited after migration wrote it; restore it from {snapshot} by hand"
            )
    return problems


def rollback(
    root: Path, snapshot: Path, *, config_home: Path, confirm: bool = False
) -> MigrationReport:
    """Return a migrated vault to v1 from its snapshot, never over another writer's bytes."""
    data = read_snapshot(snapshot)
    if Path(data.get("vault", "")).resolve() != root.resolve():
        raise MemoryVaultError("this snapshot was taken from another vault")
    report = MigrationReport(state="ready", snapshot=snapshot)
    for path, entry in sorted(data["files"].items()):
        try:
            current = _sha(read_bounded(root, path, MAX_FILE_BYTES))
        except (_Unsafe, OSError):
            current = ""
        if current == entry["original"]:
            continue
        if current != entry["converted"]:
            report.problems.append(f"{path} was edited after migration; it will be left as is")
        report.changed.append(path)
    if not confirm:
        return report
    notes: list[str] = []
    with _promotion_lock(lock_path(config_home, root), 5.0, notes):
        report.problems = _undo(root, snapshot, list(report.changed))
    with contextlib.suppress(FileNotFoundError):
        (root / MIGRATION_IN_PROGRESS).unlink()
    report.state = "rolled-back"
    return report


def report_json(report: MigrationReport) -> dict[str, Any]:
    return {
        "state": report.state,
        "problems": report.problems,
        "unresolved": report.unresolved,
        "changed": report.changed,
        "snapshot": str(report.snapshot) if report.snapshot else None,
    }


def plan_summary(mapping: dict[str, Any]) -> dict[str, Any]:
    """What a reviewer needs to choose, without IDs or hashes."""
    return {
        "records": len(mapping["entries"]),
        "drafts": sum(1 for entry in mapping["entries"] if entry["draft"]),
        "templates": len(mapping["templates"]),
        "to_choose": [
            {
                "path": e["path"],
                "type": e["type"],
                "projects": e["projects"],
                "suggested": e["suggested"],
            }
            for e in mapping["entries"]
            if e["scope"] is None
        ],
        "fixed": [
            {"path": e["path"], "scope": e["scope"]}
            for e in mapping["entries"]
            if e["scope"] is not None
        ],
    }
