"""Autonomous project-memory writes (ADR-0009, ticket M5).

Agents may create, edit, supersede, move, and delete records in the current
project, set its active context, and forget the whole project, without
approval. Every operation:

- works only on a schema 3 vault, and only when project writes are enabled
  (``AGENTBOT_MEMORY_PROJECT_WRITES=off`` disables them; reads keep working);
- resolves the project from the working directory (M3), and refuses on an
  unregistered repository or an identity collision;
- takes paths relative to that project and never follows them outside it;
- generates or preserves valid v3 front matter, validates the record for its
  exact destination, and secret-scans it before anything is written;
- is one structured sync operation, committed locally and queued for sync,
  carrying the version it expects so a concurrent change is a conflict, not
  an overwrite.

Core memory is never touched here; it changes only through proposals and the
user's approval.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import memory_projects, memory_sync
from .memory import (
    MAX_FILE_BYTES,
    Finding,
    MemoryVaultError,
    _destination_v3,
    _front_matter,
    _RecordCheck,
    read_bounded,
    read_marker,
    scan_secrets,
    validate,
)

KIND_DIRS = {"decision": "decisions", "lesson": "lessons", "note": "notes"}
KIND_TYPES = {"decision": "decision", "lesson": "lesson", "note": "project"}
FLAG = "AGENTBOT_MEMORY_PROJECT_WRITES"


def _enabled() -> bool:
    return os.environ.get(FLAG, "on").strip().lower() not in {"off", "0", "false", "no"}


def _project(vault: Path, cwd: Path, config_home: Path) -> str:
    if read_marker(vault) != 3:
        raise MemoryVaultError("project writes need a schema 3 vault; migrate it first")
    if not _enabled():
        raise MemoryVaultError(f"project writes are turned off ({FLAG}); reads still work")
    return str(memory_projects.require_project(vault, cwd, config_home)["folder"])


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80].strip("-") or "record"


def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


def render(
    *,
    record_id: str,
    kind: str,
    title: str,
    folder: str,
    body: str,
    tags: list[str],
    supersedes: list[str] | None = None,
    created: str | None = None,
) -> bytes:
    lines = [
        "---",
        "schema: 3",
        f"id: {record_id}",
        f"type: {kind}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"date: {created or datetime.now(timezone.utc).date().isoformat()}",
        "status: accepted",
        "scope: project",
        f"projects: [{folder}]",
        f"tags: {_yaml_list(tags)}",
    ]
    if supersedes:
        lines.append(f"supersedes: {_yaml_list(supersedes)}")
    lines += ["---", ""]
    return ("\n".join(lines) + (body if body.endswith("\n") else body + "\n")).encode("utf-8")


def _check(relative: str, data: bytes) -> list[Finding]:
    """The record is valid for exactly this destination, and holds no secret."""
    destination = _destination_v3(relative)
    if destination is None or destination.tier != "project":
        return [Finding("error", "MEMORY_LOCATION", relative, "not a project record location")]
    text = data.decode("utf-8")
    findings = [item for item in scan_secrets(relative, text) if item.severity == "error"]
    check = _RecordCheck(3, relative, destination)
    check.run(_front_matter(text))
    return [*findings, *check.findings]


def _require_valid(relative: str, data: bytes) -> None:
    findings = _check(relative, data)
    if findings:
        raise MemoryVaultError(
            f"{relative} was not written: " + "; ".join(f"{f.rule}: {f.message}" for f in findings)
        )


def _inside(folder: str, path: str) -> str:
    """A vault path inside the project, from a vault- or project-relative path."""
    relative = path if path.startswith("projects/") else f"projects/{folder}/{path}"
    if not relative.startswith(f"projects/{folder}/") or ".." in relative.split("/"):
        raise MemoryVaultError(f"{path} is not inside the current project ({folder})")
    return relative


def _result(op: memory_sync.Op, **extra: Any) -> dict[str, Any]:
    return {"op": op.id, "commit": op.commit, "state": "committed-locally", **extra}


def add(
    vault: Path,
    cwd: Path,
    config_home: Path,
    *,
    kind: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    supersedes: str | None = None,
    client: str = "agent",
) -> dict[str, Any]:
    folder = _project(vault, cwd, config_home)
    if kind not in KIND_DIRS:
        raise MemoryVaultError(f"kind must be one of {', '.join(sorted(KIND_DIRS))}")
    record_id = str(uuid.uuid4())
    today = datetime.now(timezone.utc).date().isoformat()
    relative = f"projects/{folder}/{KIND_DIRS[kind]}/{today}-{_slug(title)}.md"
    if (vault / relative).exists():
        relative = relative[:-3] + f"-{record_id[:8]}.md"
    data = render(
        record_id=record_id,
        kind=KIND_TYPES[kind],
        title=title,
        folder=folder,
        body=body,
        tags=list(tags or []),
        supersedes=[supersedes] if supersedes else None,
    )
    _require_valid(relative, data)
    changes: list[tuple[str, bytes | None]] = [(relative, data)]
    if supersedes:
        changes.append(_supersede_target(vault, folder, supersedes))
    op = memory_sync.change_project(vault, folder, changes, client=client)
    return _result(op, path=relative, id=record_id)


def _supersede_target(vault: Path, folder: str, target_id: str) -> tuple[str, bytes]:
    target = next((r for r in validate(vault).records if r.id == target_id and not r.draft), None)
    if target is None:
        raise MemoryVaultError(f"no record with id {target_id}")
    if not target.path.startswith(f"projects/{folder}/"):
        raise MemoryVaultError(
            f"{target.path} is not in this project; supersession stays in one project"
        )
    if target.status != "accepted":
        raise MemoryVaultError(f"{target.path} is {target.status}, not accepted")
    original = read_bounded(vault, target.path, MAX_FILE_BYTES).decode("utf-8")
    changed = _set_status(original, "superseded")
    _require_valid(target.path, changed)
    return target.path, changed


def _set_status(text: str, status: str) -> bytes:
    lines = text.split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---")
    for index in range(1, end):
        if lines[index].startswith("status:"):
            lines[index] = f"status: {status}"
    return "\n".join(lines).encode("utf-8")


def _split(text: str) -> tuple[str, str]:
    lines = text.split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---")
    return "\n".join(lines[: end + 1]), "\n".join(lines[end + 1 :])


def edit(
    vault: Path,
    cwd: Path,
    config_home: Path,
    path: str,
    *,
    body: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    client: str = "agent",
) -> dict[str, Any]:
    folder = _project(vault, cwd, config_home)
    relative = _inside(folder, path)
    try:
        text = read_bounded(vault, relative, MAX_FILE_BYTES).decode("utf-8")
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{relative} does not exist") from error
    head, old_body = _split(text)
    if title is not None or tags is not None:
        replacements = {}
        if title is not None:
            replacements["title"] = json.dumps(title, ensure_ascii=False)
        if tags is not None:
            replacements["tags"] = _yaml_list(tags)
        head = "\n".join(
            f"{key}: {replacements[key]}"
            if (key := line.split(":", 1)[0].strip()) in replacements
            else line
            for line in head.split("\n")
        )
    new_body = old_body if body is None else "\n" + (body if body.endswith("\n") else body + "\n")
    data = (head + "\n" + new_body).encode("utf-8")
    _require_valid(relative, data)
    op = memory_sync.change_project(vault, folder, [(relative, data)], client=client)
    return _result(op, path=relative)


def set_context(
    vault: Path, cwd: Path, config_home: Path, body: str, *, client: str = "agent"
) -> dict[str, Any]:
    """Create or replace the project's bounded active context."""
    folder = _project(vault, cwd, config_home)
    relative = f"projects/{folder}/active-context.md"
    existing = next((r for r in validate(vault).records if r.path == relative), None)
    data = render(
        record_id=existing.id if existing and existing.id else str(uuid.uuid4()),
        kind="context",
        title="Active context",
        folder=folder,
        body=body,
        tags=list(existing.tags) if existing else [],
        created=existing.date if existing else None,
    )
    _require_valid(relative, data)
    op = memory_sync.change_project(vault, folder, [(relative, data)], client=client)
    return _result(op, path=relative)


def move(
    vault: Path, cwd: Path, config_home: Path, path: str, new_path: str, *, client: str = "agent"
) -> dict[str, Any]:
    folder = _project(vault, cwd, config_home)
    source = _inside(folder, path)
    target = _inside(folder, new_path)
    if source.endswith("/project.md"):
        raise MemoryVaultError("project.md is the project's identity and does not move")
    if (vault / target).exists():
        raise MemoryVaultError(f"{target} already exists")
    try:
        data = read_bounded(vault, source, MAX_FILE_BYTES)
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{source} does not exist") from error
    _require_valid(target, data)
    op = memory_sync.change_project(vault, folder, [(target, data), (source, None)], client=client)
    return _result(op, path=target, moved_from=source)


def delete(
    vault: Path, cwd: Path, config_home: Path, path: str, *, client: str = "agent"
) -> dict[str, Any]:
    folder = _project(vault, cwd, config_home)
    relative = _inside(folder, path)
    if relative.endswith("/project.md"):
        raise MemoryVaultError("project.md is the project's identity; forget the project instead")
    op = memory_sync.change_project(vault, folder, [(relative, None)], client=client)
    return _result(op, path=relative)


def forget(vault: Path, cwd: Path, config_home: Path, *, client: str = "agent") -> dict[str, Any]:
    """Remove the current project's memory folder. Its registry identity stays."""
    folder = _project(vault, cwd, config_home)
    op = memory_sync.forget_project(vault, folder, client=client)
    return _result(op, path=f"projects/{folder}", recover=f"git revert {op.commit}")
