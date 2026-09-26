"""Migrate a schema 2 vault to schema 3 (ADR-0009, ticket M4).

Schema 3 moves records into trust tiers: ``core/`` (approval-gated),
``projects/<folder>/`` (autonomous), and tracked ``proposals/core/``. The
migration is the same reviewed pattern as v1 to v2:

1. ``plan`` suggests a destination for every record and pending draft. It
   leaves ``null`` where a person must choose (the old global active context,
   and project-scoped drafts) and asks for each project's origin: a Git URL,
   or ``local`` for a repository with no remote.
2. ``check`` builds the complete v3 tree in isolation (moved and converted
   records, the project registry, one ``project.md`` per project, updated
   templates, and a new marker) and runs the v3 validator on it.
3. ``apply`` repeats the check, snapshots every byte it will change, blocks
   reads with the migration guard, writes under the promotion lock, and
   changes the marker last. Any failure undoes only its own writes.
4. ``rollback`` restores schema 2 from the snapshot, never over a later edit.

Record bodies, IDs, titles, dates, statuses, tags, and supersession links are
kept. Only ``schema`` changes, plus ``scope``/``projects`` when a record moves
into a project, and ``status`` when a project-scoped draft is accepted into
project memory. A retired record leaves the tree but stays in Git history.
Nothing is staged, committed, or pushed.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import write_text_atomic
from .memory import (
    MARKER,
    MAX_FILE_BYTES,
    MIGRATION_IN_PROGRESS,
    REGISTRY,
    SLUG,
    UUID4,
    MemoryVaultError,
    Record,
    _git_text,
    _listed_paths,
    _Unsafe,
    read_bounded,
    read_marker,
    validate,
)
from .memory_approve import _promotion_lock, lock_path
from .memory_migrate import MigrationReport, _clean_tree
from .memory_sync import normalize_origin

TARGET = 3
DIRS = {
    "decision": "decisions",
    "lesson": "lessons",
    "preference": "preferences",
    "profile": "profile",
}
V2_ONLY_READMES = ("decisions/README.md", "lessons/README.md", "drafts/README.md")
NEW_READMES = {
    "core/README.md": "# Core memory\n\nLong-term memory. Changes need the user's approval.\n",
    "proposals/README.md": "# Proposals\n\nPending core changes. Never retrieved as memory.\n",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _suggest(record: Record) -> tuple[str | None, bool]:
    """(destination, fixed). ``retire`` drops the record from the tree."""
    name = record.path.rsplit("/", 1)[-1]
    folder = record.projects[0] if len(record.projects) == 1 else None
    if record.draft:
        if record.scope in {"global", "shared"} and record.type in DIRS:
            return f"proposals/core/{DIRS[record.type]}/{name}", True
        return (
            f"projects/{folder}/{DIRS.get(record.type, 'notes')}/{name}" if folder else None
        ), False
    if record.type == "preference":
        return "core/user/preferences.md", True
    if record.type == "context":
        return "retire", False
    if record.type == "project" and folder:
        return f"projects/{folder}/notes/{name}", True
    if record.type in {"decision", "lesson"}:
        if record.scope == "project" and folder:
            return f"projects/{folder}/{DIRS[record.type]}/{name}", True
        return f"core/{DIRS[record.type]}/{name}", True
    return None, False


def plan(root: Path) -> dict[str, Any]:
    if read_marker(root) != 2:
        raise MemoryVaultError("only a schema 2 vault can be migrated to schema 3")
    report = validate(root)
    if report.findings:
        raise MemoryVaultError(
            f"the v2 tree has {len(report.findings)} finding(s); fix them before planning"
        )
    entries = []
    folders: set[str] = set()
    for record in sorted(report.records, key=lambda item: item.path):
        destination, fixed = _suggest(record)
        entries.append(
            {
                "path": record.path,
                "sha256": _sha(read_bounded(root, record.path, MAX_FILE_BYTES)),
                "id": record.id,
                "type": record.type,
                "scope": record.scope,
                "projects": list(record.projects),
                "draft": record.draft,
                "suggested": destination,
                "destination": destination if fixed else None,
            }
        )
        if record.scope == "project" or record.type == "project":
            folders.update(record.projects)
    for path in _listed_paths(root):
        parts = path.split("/")
        if parts[0] == "projects" and len(parts) >= 3:
            folders.add(parts[1])
    templates = [
        {"path": path, "sha256": _sha(read_bounded(root, path, MAX_FILE_BYTES))}
        for path in _listed_paths(root)
        if path.startswith("templates/") and path.endswith(".md") and not path.endswith("README.md")
    ]
    return {
        "version": 1,
        "target": TARGET,
        "vault": str(root.resolve()),
        "head": _git_text(root, "rev-parse", "HEAD"),
        "marker_sha256": _sha(read_bounded(root, MARKER, 4096)),
        "vault_id": str(uuid.uuid4()),
        "entries": entries,
        "projects": [
            {"folder": folder, "id": str(uuid.uuid4()), "origin": None}
            for folder in sorted(folders)
        ],
        "templates": templates,
    }


# --- Conversion ---------------------------------------------------------------


def _set_fields(text: str, values: dict[str, str]) -> str:
    """Replace or append front-matter lines; the body is untouched."""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None)
    if lines[0].rstrip("\r") != "---" or end is None:
        raise MemoryVaultError("front matter is not delimited")
    remaining = dict(values)
    for index in range(1, end):
        key = lines[index].split(":", 1)[0].strip()
        if key in remaining:
            ending = "\r" if lines[index].endswith("\r") else ""
            lines[index] = f"{key}: {remaining.pop(key)}".rstrip() + ending
    insert = [f"{key}: {value}".rstrip() for key, value in remaining.items()]
    return "\n".join([*lines[:end], *insert, *lines[end:]])


def _project_origin(project: dict[str, Any]) -> str:
    origin = project["origin"]
    if origin == "local":
        return f"local/{project['id'][:8]}-{project['folder']}"
    return normalize_origin(origin)


def _project_md(project: dict[str, Any], origin: str, today: str) -> bytes:
    return (
        "---\n"
        "schema: 3\n"
        f"id: {project['id']}\n"
        "type: project\n"
        f"title: {project['folder']}\n"
        f"date: {today}\n"
        "status: accepted\n"
        "scope: project\n"
        f"projects: [{project['folder']}]\n"
        "tags: []\n"
        "---\n\n"
        f"Origin: {origin}\n"
    ).encode()


def _changes(
    root: Path, mapping: dict[str, Any], report: MigrationReport
) -> tuple[dict[str, bytes], set[str], bytes]:
    """The full v3 change set: writes, removals, and the new marker."""
    if Path(mapping.get("vault", "")).resolve() != root.resolve():
        raise MemoryVaultError("this mapping was made for another vault")
    if read_marker(root) != 2:
        raise MemoryVaultError("the vault is not schema 2")
    listed = set(_listed_paths(root))
    current = {record.path for record in validate(root).records}
    mapped = {entry["path"] for entry in mapping["entries"]}
    report.problems += [f"{p} is not in the mapping; plan again" for p in sorted(current - mapped)]
    report.problems += [
        f"{p} is in the mapping but not a record now" for p in sorted(mapped - current)
    ]
    projects = {project["folder"]: project for project in mapping["projects"]}
    for project in mapping["projects"]:
        if not SLUG.match(project["folder"]) or not UUID4.match(project["id"]):
            report.problems.append(f"project {project['folder']}: invalid folder or id")
        if not project.get("origin"):
            report.unresolved.append(f"project {project['folder']}: origin (a Git URL or 'local')")
    writes: dict[str, bytes] = {}
    removals: set[str] = set()
    for entry in mapping["entries"]:
        destination = entry.get("destination")
        if destination is None:
            report.unresolved.append(entry["path"])
            continue
        original = _read_checked(root, entry["path"], entry["sha256"], report)
        if original is None:
            continue
        removals.add(entry["path"])
        if destination == "retire":
            continue
        if destination in writes:
            report.problems.append(f"{destination} is the destination of two records")
            continue
        writes[destination] = _convert(original, entry, destination, projects, report)
    today = datetime.now(timezone.utc).date().isoformat()
    registered = []
    for project in mapping["projects"]:
        if not project.get("origin"):
            continue
        try:
            origin = _project_origin(project)
        except MemoryVaultError as error:
            report.problems.append(f"project {project['folder']}: {error}")
            continue
        writes[f"projects/{project['folder']}/project.md"] = _project_md(project, origin, today)
        registered.append({"id": project["id"], "origin": origin, "folder": project["folder"]})
    registry = {"projects": registered}
    writes[REGISTRY] = (json.dumps(registry, indent=2, sort_keys=True) + "\n").encode()
    for template in mapping.get("templates", []):
        original = _read_checked(root, template["path"], template["sha256"], report)
        if original is not None:
            writes[template["path"]] = original.replace(b"\nschema: 2\n", b"\nschema: 3\n")
    for readme in V2_ONLY_READMES:
        if readme in listed:
            removals.add(readme)
    for path, text in NEW_READMES.items():
        if path not in listed:
            writes[path] = text.encode()
    replaceable = removals | {t["path"] for t in mapping.get("templates", [])}
    for path in writes:
        if path in listed and path not in replaceable:
            report.problems.append(f"{path} already exists and would be overwritten")
    marker = (
        json.dumps({"agentbot_memory_schema": TARGET, "vault_id": mapping["vault_id"]}) + "\n"
    ).encode()
    return writes, removals, marker


def _read_checked(root: Path, path: str, digest: str, report: MigrationReport) -> bytes | None:
    try:
        data = read_bounded(root, path, MAX_FILE_BYTES)
    except (_Unsafe, OSError):
        report.problems.append(f"{path} cannot be read")
        return None
    if _sha(data) != digest:
        report.problems.append(f"{path} changed since the mapping was made")
        return None
    return data


def _convert(
    original: bytes,
    entry: dict[str, Any],
    destination: str,
    projects: dict[str, dict[str, Any]],
    report: MigrationReport,
) -> bytes:
    values = {"schema": "3"}
    parts = destination.split("/")
    if parts[0] == "projects" and len(parts) >= 3:
        folder = parts[1]
        if folder not in projects:
            report.problems.append(f"{destination}: project {folder} is not in the mapping")
        values.update({"scope": "project", "projects": f"[{folder}]"})
        if entry["draft"]:
            values["status"] = "accepted"
    try:
        return _set_fields(original.decode("utf-8"), values).encode("utf-8")
    except MemoryVaultError as error:
        report.problems.append(f"{entry['path']}: {error}")
        return original


# --- Check ----------------------------------------------------------------------


def _materialize(
    root: Path, target: Path, writes: dict[str, bytes], removals: set[str], marker: bytes
) -> None:
    for path in _listed_paths(root):
        if path.startswith(".obsidian/") or path in removals:
            continue
        source = root / path
        if source.is_symlink() or not source.is_file():
            continue
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    for path, data in writes.items():
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    (target / MARKER).write_bytes(marker)


def check(
    root: Path, mapping: dict[str, Any]
) -> tuple[MigrationReport, tuple[dict[str, bytes], set[str], bytes]]:
    report = MigrationReport(state="ready")
    changes = _changes(root, mapping, report)
    if report.problems:
        report.state = "stale"
        return report, changes
    if report.unresolved:
        report.state = "unresolved"
        return report, changes
    scratch = Path(tempfile.mkdtemp(prefix="agentbot-migrate3-"))
    os.chmod(scratch, 0o700)
    candidate = scratch / "vault"
    try:
        subprocess.run(["git", "init", "-q", str(candidate)], check=True, capture_output=True)
        _materialize(root, candidate, *changes)
        findings = validate(candidate).findings
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    report.problems += [f"{item.path}: {item.rule}: {item.message}" for item in findings]
    if findings:
        report.state = "invalid"
    writes, removals, _ = changes
    report.changed = sorted(set(writes) | removals | {MARKER})
    return report, changes


# --- Apply and rollback ---------------------------------------------------------


def _snapshot(
    root: Path, destination: Path, writes: dict[str, bytes], removals: set[str], marker: bytes
) -> None:
    if destination.exists() or destination.is_symlink():
        raise MemoryVaultError(f"{destination} exists; the snapshot needs a new directory")
    resolved = destination.resolve()
    if resolved == root.resolve() or root.resolve() in resolved.parents:
        raise MemoryVaultError("the snapshot must be outside the vault")
    destination.mkdir(mode=0o700, parents=True)
    files: dict[str, dict[str, Any]] = {}
    touched = {**dict.fromkeys(removals), **writes, MARKER: marker}
    for index, path in enumerate(sorted(touched)):
        new = touched[path]
        original = read_bounded(root, path, MAX_FILE_BYTES) if (root / path).is_file() else None
        name = f"{index:05}"
        if original is not None:
            (destination / f"{name}.orig").write_bytes(original)
            os.chmod(destination / f"{name}.orig", 0o600)
        files[path] = {
            "file": name,
            "original": None if original is None else _sha(original),
            "converted": None if new is None else _sha(new),
        }
    write_text_atomic(
        destination / "manifest.json",
        json.dumps({"vault": str(root.resolve()), "target": TARGET, "files": files}, indent=2)
        + "\n",
    )
    os.chmod(destination / "manifest.json", 0o600)


def _write_new(root: Path, path: str, data: bytes, expected_original: str | None) -> None:
    target = root / path
    current = target.read_bytes() if target.is_file() else None
    if (None if current is None else _sha(current)) != expected_original:
        raise MemoryVaultError(f"{path} changed during migration")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.agentbot-migrate.tmp")
    with open(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644), "wb"
    ) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if expected_original is None:
        try:
            os.link(temporary, target)
        finally:
            temporary.unlink()
    else:
        os.replace(temporary, target)


def _remove(root: Path, path: str, expected_original: str) -> None:
    target = root / path
    if not target.is_file() or _sha(target.read_bytes()) != expected_original:
        raise MemoryVaultError(f"{path} changed during migration")
    target.unlink()
    parent = target.parent
    while parent != root and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def apply(
    root: Path, mapping: dict[str, Any], *, snapshot: Path, config_home: Path, confirm: bool = False
) -> MigrationReport:
    _clean_tree(root)
    report, changes = check(root, mapping)
    if report.state != "ready" or not confirm:
        return report
    writes, removals, marker = changes
    report.snapshot = snapshot
    _snapshot(root, snapshot, writes, removals, marker)
    files = json.loads((snapshot / "manifest.json").read_text())["files"]
    guard = root / MIGRATION_IN_PROGRESS
    done: list[str] = []
    notes: list[str] = []
    with _promotion_lock(lock_path(config_home, root), 5.0, notes):
        os.close(os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        try:
            for path in sorted(writes):
                _write_new(root, path, writes[path], files[path]["original"])
                done.append(path)
            for path in sorted(removals - set(writes)):
                _remove(root, path, files[path]["original"])
                done.append(path)
            _write_new(root, MARKER, marker, files[MARKER]["original"])
            done.append(MARKER)
            guard.unlink()
            findings = validate(root).findings
            if findings:
                guard.touch()
                raise MemoryVaultError(f"the migrated vault has {len(findings)} finding(s)")
        except BaseException as error:
            report.problems.extend(_undo(root, snapshot, done))
            with contextlib.suppress(FileNotFoundError):
                guard.unlink()
            if isinstance(error, MemoryVaultError):
                report.state = "rolled-back"
                report.problems.insert(0, str(error))
                return report
            raise
    report.state = "applied"
    report.changed = done
    return report


def _current_sha(root: Path, path: str) -> str | None:
    target = root / path
    return _sha(target.read_bytes()) if target.is_file() else None


def _undo(root: Path, snapshot: Path, paths: list[str]) -> list[str]:
    """Restore originals, and remove created files, only where the bytes are still ours."""
    files = json.loads((snapshot / "manifest.json").read_text())["files"]
    problems = []
    for path in reversed(paths):
        entry = files[path]
        current = _current_sha(root, path)
        if current != entry["converted"]:
            if current != entry["original"]:
                problems.append(
                    f"{path} was edited after migration wrote it; restore it from {snapshot} by hand"
                )
            continue
        target = root / path
        if entry["original"] is None:
            target.unlink()
            continue
        data = (snapshot / f"{entry['file']}.orig").read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.agentbot-rollback.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return problems


def rollback(
    root: Path, snapshot: Path, *, config_home: Path, confirm: bool = False
) -> MigrationReport:
    data = json.loads((snapshot / "manifest.json").read_text())
    if Path(data.get("vault", "")).resolve() != root.resolve():
        raise MemoryVaultError("this snapshot was taken from another vault")
    report = MigrationReport(state="ready", snapshot=snapshot)
    for path, entry in sorted(data["files"].items()):
        current = _current_sha(root, path)
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


def plan_summary(mapping: dict[str, Any]) -> dict[str, Any]:
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
            if e["destination"] is None
        ],
        "fixed": [
            {"path": e["path"], "scope": e["destination"]}
            for e in mapping["entries"]
            if e["destination"] is not None
        ],
        "projects_needing_origin": [
            p["folder"] for p in mapping["projects"] if not p.get("origin")
        ],
    }
