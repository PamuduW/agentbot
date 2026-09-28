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
    project_hub_link,
    read_bounded,
    read_marker,
    record_link,
    scan_secrets,
    validate,
)

KIND_DIRS = {"decision": "decisions", "lesson": "lessons", "note": "notes"}
KIND_TYPES = {"decision": "decision", "lesson": "lesson", "note": "project"}
FLAG = "AGENTBOT_MEMORY_PROJECT_WRITES"

# Maintenance pressure (ADR-0009 / R7 section 5.7.4): warn, then require tidying.
CONTEXT_SOFT_TOKENS = 800
CONTEXT_HARD_TOKENS = 1_200
# One idea per record keeps a whole-record read cheap once the header says it
# is the one: past this, the write succeeds with advice to split.
RECORD_SOFT_TOKENS = 400
POOL_SOFT = 48
POOL_HARD = 64


def _enabled() -> bool:
    return os.environ.get(FLAG, "on").strip().lower() not in {"off", "0", "false", "no"}


def _project(vault: Path, cwd: Path, config_home: Path) -> str:
    read_marker(vault)
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
    replaces: list[str] | None = None,
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
    lines.append(f"up: {json.dumps(project_hub_link(folder))}")
    if replaces:
        lines.append(f"replaces: {_yaml_list([json.dumps(link) for link in replaces])}")
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


def _body_of(data: bytes) -> str:
    return _split(data.decode("utf-8"))[1].strip()


def _live(vault: Path, folder: str) -> list[Any]:
    """The project's records still in the hot pool: accepted, not drafts."""
    return [
        r
        for r in validate(vault).records
        if r.path.startswith(f"projects/{folder}/") and not r.draft and r.status == "accepted"
    ]


def _pressure(
    vault: Path,
    folder: str,
    relative: str,
    data: bytes,
    *,
    adding: bool,
    moving_from: str | None = None,
) -> list[str]:
    """Deterministic maintenance checks. Raises for a hard limit; returns warnings."""
    from .memory_retrieve import _similar, estimate_tokens

    warnings: list[str] = []
    live = _live(vault, folder)
    body = _body_of(data)
    title = _front_matter(data.decode("utf-8"))["title"]
    for record in live:
        if record.path in {relative, moving_from}:
            continue
        other = read_bounded(vault, record.path, MAX_FILE_BYTES)
        # A move takes the text along; only new text can repeat another record.
        if body and moving_from is None and _body_of(other) == body:
            raise MemoryVaultError(f"the same text is already recorded in {record.path}")
        probe = type(record)(
            path=relative, type=record.type, status="accepted", draft=False, title=title
        )
        if _similar(probe, record):
            warnings.append(f"a similar title exists: {record.path}")
    if relative.endswith("/active-context.md"):
        size = estimate_tokens(body)
        if size > CONTEXT_HARD_TOKENS:
            raise MemoryVaultError(
                f"the active context is about {size} tokens; keep it under {CONTEXT_HARD_TOKENS} "
                "by moving history into lessons or decisions"
            )
        if size > CONTEXT_SOFT_TOKENS:
            warnings.append(f"the active context is about {size} tokens; compact it soon")
    elif not relative.endswith("/project.md") and estimate_tokens(body) > RECORD_SOFT_TOKENS:
        warnings.append(
            f"this record is about {estimate_tokens(body)} tokens; split it into one record "
            f"per idea (under {RECORD_SOFT_TOKENS}) so each stays cheap to read"
        )
    if adding:
        if len(live) >= POOL_HARD:
            raise MemoryVaultError(
                f"project {folder} has {len(live)} live records (limit {POOL_HARD}); "
                "run agentbot memory project maintain, then retire, supersede, or delete some"
            )
        if len(live) >= POOL_SOFT:
            warnings.append(f"project {folder} has {len(live)} live records; maintenance is due")
    return warnings


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
    target = _supersede_target(vault, folder, supersedes) if supersedes else None
    data = render(
        record_id=record_id,
        kind=KIND_TYPES[kind],
        title=title,
        folder=folder,
        body=body,
        tags=list(tags or []),
        supersedes=[supersedes] if supersedes else None,
        replaces=[record_link(target[0])] if target else None,
    )
    _require_valid(relative, data)
    warnings = _pressure(vault, folder, relative, data, adding=not supersedes)
    changes: list[tuple[str, bytes | None]] = [(relative, data)]
    if target:
        changes.append(target)
    op = memory_sync.change_project(vault, folder, changes, client=client)
    return _result(op, path=relative, id=record_id, warnings=warnings)


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


def _with_up(head: str, folder: str, relative: str) -> str:
    """Give a record written before links existed its project link."""
    if relative == f"projects/{folder}/project.md":
        return head
    lines = head.split("\n")
    if any(line.startswith("up:") for line in lines):
        return head
    return "\n".join([*lines[:-1], f"up: {json.dumps(project_hub_link(folder))}", lines[-1]])


def _relink(vault: Path, folder: str, old: str, new: str) -> list[tuple[str, bytes | None]]:
    """Rewrite replaces links in this project that point at a moved record."""
    old_link, new_link = json.dumps(record_link(old)), json.dumps(record_link(new))
    changes: list[tuple[str, bytes | None]] = []
    for record in validate(vault).records:
        if not record.path.startswith(f"projects/{folder}/") or record.path == old:
            continue
        text = read_bounded(vault, record.path, MAX_FILE_BYTES).decode("utf-8")
        head, rest = _split(text)
        if old_link not in head:
            continue
        # Anywhere in the front matter, not only a one-line list: Obsidian
        # may rewrite `replaces` as a block list. Only replaces links point at
        # records (up points at project.md, which never moves).
        head = head.replace(old_link, new_link)
        data = (head + "\n" + rest).encode("utf-8")
        _require_valid(record.path, data)
        changes.append((record.path, data))
    return changes


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
    head = _with_up(head, folder, relative)
    new_body = old_body if body is None else "\n" + (body if body.endswith("\n") else body + "\n")
    data = (head + "\n" + new_body).encode("utf-8")
    _require_valid(relative, data)
    warnings = _pressure(vault, folder, relative, data, adding=False)
    op = memory_sync.change_project(vault, folder, [(relative, data)], client=client)
    return _result(op, path=relative, warnings=warnings)


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
    # A new active context is a new live record, so it counts against the pool.
    warnings = _pressure(vault, folder, relative, data, adding=existing is None)
    op = memory_sync.change_project(vault, folder, [(relative, data)], client=client)
    return _result(op, path=relative, warnings=warnings)


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
    head, rest = _split(data.decode("utf-8"))
    data = (_with_up(head, folder, target) + "\n" + rest).encode("utf-8")
    _require_valid(target, data)
    # The record itself is leaving `source`, so its own text is no duplicate.
    warnings = _pressure(vault, folder, target, data, adding=False, moving_from=source)
    changes = [(target, data), (source, None), *_relink(vault, folder, source, target)]
    op = memory_sync.change_project(vault, folder, changes, client=client)
    return _result(op, path=target, moved_from=source, warnings=warnings)


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


def retire(
    vault: Path, cwd: Path, config_home: Path, path: str, *, client: str = "agent"
) -> dict[str, Any]:
    """Take a record out of retrieval without deleting it; it stays readable in the vault."""
    folder = _project(vault, cwd, config_home)
    relative = _inside(folder, path)
    if relative.endswith("/project.md"):
        raise MemoryVaultError("project.md is the project's identity and cannot be retired")
    try:
        text = read_bounded(vault, relative, MAX_FILE_BYTES).decode("utf-8")
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{relative} does not exist") from error
    data = _set_status(text, "retired")
    _require_valid(relative, data)
    op = memory_sync.change_project(vault, folder, [(relative, data)], client=client)
    return _result(op, path=relative)


def promote(
    vault: Path,
    cwd: Path,
    config_home: Path,
    path: str,
    *,
    scope: str = "shared",
    client: str = "agent",
) -> dict[str, Any]:
    """Propose a project lesson or decision for core memory. The source stays in the project."""
    from . import memory_proposals

    folder = _project(vault, cwd, config_home)
    relative = _inside(folder, path)
    try:
        text = read_bounded(vault, relative, MAX_FILE_BYTES).decode("utf-8")
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{relative} does not exist") from error
    fields = _front_matter(text)
    if fields["type"] not in {"lesson", "decision"}:
        raise MemoryVaultError("only lessons and decisions are promoted to core")
    body = _split(text)[1].strip() + f"\n\nPromoted from {relative}.\n"
    return memory_proposals.propose(
        vault,
        kind=fields["type"],
        title=fields["title"],
        body=body,
        scope=scope,
        projects=[folder] if scope == "shared" else [],
        tags=list(fields["tags"]),
        client=client,
    )


def maintain(vault: Path, cwd: Path, config_home: Path) -> dict[str, Any]:
    """A read-only maintenance report for the current project."""
    from .memory import utc_today
    from .memory_retrieve import _similar, estimate_tokens

    folder = _project(vault, cwd, config_home)
    today = utc_today()
    records = [
        r
        for r in validate(vault).records
        if r.path.startswith(f"projects/{folder}/") and not r.draft
    ]
    live = [r for r in records if r.status == "accepted"]
    clusters = []
    for index, record in enumerate(live):
        for other in live[index + 1 :]:
            if _similar(record, other):
                clusters.append([record.path, other.path])
    context = next((r for r in live if r.path.endswith("/active-context.md")), None)
    context_tokens = (
        estimate_tokens(_body_of(read_bounded(vault, context.path, MAX_FILE_BYTES)))
        if context
        else 0
    )
    return {
        "state": "report",
        "project": folder,
        "live": len(live),
        "limits": {"warn": POOL_SOFT, "stop": POOL_HARD},
        "superseded": sum(1 for r in records if r.status == "superseded"),
        "retired": sum(1 for r in records if r.status == "retired"),
        "expired": [r.path for r in live if r.expired(today)],
        "due_for_review": [r.path for r in live if r.due(today)],
        "similar_titles": clusters,
        "context_tokens": context_tokens,
        "suggestions": _suggest(len(live), clusters, context_tokens),
    }


def _suggest(live: int, clusters: list[list[str]], context_tokens: int) -> list[str]:
    tips = []
    if live >= POOL_SOFT:
        tips.append("retire or supersede old records; delete clear duplicates")
    if clusters:
        tips.append("merge similar records with add --supersedes, or delete the weaker one")
    if context_tokens > CONTEXT_SOFT_TOKENS:
        tips.append("shorten the active context; move history into lessons or decisions")
    tips.append(
        "promote lessons that hold across projects with: agentbot memory project promote PATH"
    )
    return tips
