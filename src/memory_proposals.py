"""Tracked core proposals (ADR-0009, ticket M7).

On a schema 3 vault, agents never write core memory. They propose: each
proposal is a Markdown record under ``proposals/core/`` with ``status: draft``.
It is validated and secret-scanned, committed and synced like any other
operation, so it reaches every machine, and it is never returned by search,
show, or brief. The queue warns at 10 open proposals and refuses new ones at
25; a proposal whose title matches an open one is refused as a duplicate.

Only the user approves or rejects. Approval is one operation: the record is
installed in ``core/`` with ``status: accepted``, any core records it
supersedes are marked superseded, and the proposal is removed, all or nothing.
Preference and profile proposals replace ``core/user/preferences.md`` or
``profile.md`` and keep the existing record's ID. Rejection removes the
proposal. The approval commits are pushed like every other write.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import memory_sync
from .memory import (
    MAX_FILE_BYTES,
    Finding,
    MemoryVaultError,
    _decode,
    _destination_v3,
    _front_matter,
    _RecordCheck,
    _Unsafe,
    read_bounded,
    read_marker,
    record_link,
    scan_secrets,
    validate,
)

SOFT_CAP = 10
RECORD_SOFT_TOKENS = 400  # the same advice project writes give
HARD_CAP = 25
KIND_DIRS = {
    "decision": "decisions",
    "lesson": "lessons",
    "preference": "preferences",
    "profile": "profile",
}
USER_FILES = {"preference": "core/user/preferences.md", "profile": "core/user/profile.md"}


def read_body(source: Path | None, stdin: Any = None) -> str:
    """A record or proposal body, bounded and UTF-8, from one named file or stdin."""
    if source is not None:
        try:
            handle = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as error:
            raise MemoryVaultError(
                "--from-file must name a readable regular file, not a symlink"
            ) from error
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            os.close(handle)
            raise MemoryVaultError("--from-file must name a regular file")
        with os.fdopen(handle, "rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    else:
        data = stdin.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise MemoryVaultError(f"the body is larger than {MAX_FILE_BYTES} bytes")
    try:
        return _decode(data)
    except _Unsafe as error:
        raise MemoryVaultError(f"the body {error.message}") from error


def _require_v3(vault: Path) -> None:
    read_marker(vault)


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80].strip("-") or "proposal"


def _findings(relative: str, data: bytes) -> list[Finding]:
    destination = _destination_v3(relative)
    if destination is None:
        return [Finding("error", "MEMORY_LOCATION", relative, "not a v3 record location")]
    text = data.decode("utf-8")
    found = [item for item in scan_secrets(relative, text) if item.severity == "error"]
    check = _RecordCheck(3, relative, destination)
    check.run(_front_matter(text))
    return [*found, *check.findings]


def _require_valid(relative: str, data: bytes) -> None:
    findings = _findings(relative, data)
    if findings:
        raise MemoryVaultError(
            f"{relative} was not written: " + "; ".join(f"{f.rule}: {f.message}" for f in findings)
        )


def queue(vault: Path) -> list[dict[str, Any]]:
    """Open proposals: metadata only, newest first."""
    report = validate(vault)
    counts: dict[str, int] = {}
    for item in report.findings:
        counts[item.path] = counts.get(item.path, 0) + 1
    items = [
        {
            "path": record.path,
            "id": record.id,
            "type": record.type,
            "title": record.title,
            "date": record.date,
            "scope": record.scope,
            "findings": counts.get(record.path, 0),
        }
        for record in report.records
        if record.draft and record.path.startswith("proposals/core/")
    ]
    return sorted(items, key=lambda item: (item["date"], item["path"]), reverse=True)


def propose(
    vault: Path,
    *,
    kind: str,
    title: str,
    body: str,
    scope: str = "global",
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    supersedes: list[str] | None = None,
    client: str = "agent",
) -> dict[str, Any]:
    _require_v3(vault)
    if kind not in KIND_DIRS:
        raise MemoryVaultError(f"a core proposal is one of {', '.join(sorted(KIND_DIRS))}")
    open_items = queue(vault)
    if len(open_items) >= HARD_CAP:
        raise MemoryVaultError(
            f"{len(open_items)} proposals are waiting; review them before proposing more"
        )
    wanted = title.strip().casefold()
    duplicate = next(
        (item for item in open_items if item["title"].strip().casefold() == wanted), None
    )
    if duplicate:
        raise MemoryVaultError(f"an open proposal already has this title: {duplicate['path']}")
    record_id = str(uuid.uuid4())
    today = datetime.now(timezone.utc).date().isoformat()
    relative = f"proposals/core/{KIND_DIRS[kind]}/{today}-{_slug(title)}-{record_id[:8]}.md"
    lines = [
        "---",
        "schema: 3",
        f"id: {record_id}",
        f"type: {kind}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"date: {today}",
        "status: draft",
        f"scope: {scope}",
        f"projects: [{', '.join(projects or [])}]",
        f"tags: [{', '.join(tags or [])}]",
    ]
    if supersedes:
        lines.append(f"supersedes: [{', '.join(supersedes)}]")
    data = ("\n".join([*lines, "---", ""]) + (body if body.endswith("\n") else body + "\n")).encode(
        "utf-8"
    )
    _require_valid(relative, data)
    op = memory_sync.change_core(vault, [(relative, data)], tier="proposal", client=client)
    waiting = len(open_items) + 1
    result: dict[str, Any] = {
        "state": "proposed",
        "path": relative,
        "id": record_id,
        "op": op.id,
        "open": waiting,
    }
    if waiting >= SOFT_CAP:
        result["warning"] = f"{waiting} proposals are waiting for review"
    size = (len(body) + 3) // 4
    if size > RECORD_SOFT_TOKENS:
        advice = (
            f"this proposal is about {size} tokens; split it into one per idea "
            f"(under {RECORD_SOFT_TOKENS}) so each stays cheap to read"
        )
        result["warning"] = "; ".join(filter(None, [result.get("warning"), advice]))
    return result


def _proposal(vault: Path, relative: str) -> tuple[bytes, dict[str, Any]]:
    if (
        not relative.startswith("proposals/core/")
        or not relative.endswith(".md")
        or ".." in relative.split("/")
    ):
        raise MemoryVaultError("give a proposals/core/<...>.md path")
    try:
        data = read_bounded(vault, relative, MAX_FILE_BYTES)
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{relative} does not exist") from error
    return data, _front_matter(data.decode("utf-8"))


def body(vault: Path, relative: str) -> str:
    """A proposal's text below its front matter, for the user reviewing it."""
    data, _ = _proposal(vault, relative)
    lines = data.decode("utf-8").split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---")
    return "\n".join(lines[end + 1 :]).strip("\n")


def _set(text: str, values: dict[str, str]) -> str:
    lines = text.split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---")
    for index in range(1, end):
        key = lines[index].split(":", 1)[0].strip()
        if key in values:
            lines[index] = f"{key}: {values[key]}"
    return "\n".join(lines)


def _with_field(text: str, key: str, value: str) -> str:
    """Set one front-matter key, adding it before the closing marker if absent."""
    if any(line.startswith(f"{key}:") for line in text.split("\n")[1:]):
        return _set(text, {key: value})
    lines = text.split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---")
    return "\n".join([*lines[:end], f"{key}: {value}", *lines[end:]])


def approve(vault: Path, relative: str, *, client: str = "human") -> dict[str, Any]:
    """Human-only: install the proposal in core, all or nothing."""
    _require_v3(vault)
    data, fields = _proposal(vault, relative)
    findings = [item for item in validate(vault).findings if item.path == relative]
    if findings:
        raise MemoryVaultError(f"{relative} has {len(findings)} finding(s); fix or reject it first")
    kind = fields["type"]
    values = {"status": "accepted"}
    if kind in USER_FILES:
        destination = USER_FILES[kind]
        existing = next((r for r in validate(vault).records if r.path == destination), None)
        if existing is not None and existing.id:
            values["id"] = existing.id
    else:
        destination = f"core/{KIND_DIRS[kind]}/{relative.rsplit('/', 1)[-1]}"
        if (vault / destination).exists():
            raise MemoryVaultError(f"{destination} already exists")
    accepted_text = _set(data.decode("utf-8"), values)
    changes: list[tuple[str, bytes | None]] = [(relative, None)]
    replaced: list[str] = []
    records = {
        record.id: record for record in validate(vault).records if record.id and not record.draft
    }
    for target_id in fields.get("supersedes") or []:
        target = records.get(target_id)
        if target is None or not target.path.startswith("core/"):
            raise MemoryVaultError(f"{target_id} is not a core record; supersession stays in core")
        if target.status != "accepted":
            raise MemoryVaultError(f"{target.path} is {target.status}, not accepted")
        original = read_bounded(vault, target.path, MAX_FILE_BYTES).decode("utf-8")
        marked = _set(original, {"status": "superseded"}).encode("utf-8")
        _require_valid(target.path, marked)
        changes.append((target.path, marked))
        replaced.append(json.dumps(record_link(target.path)))
    if replaced:
        accepted_text = _with_field(accepted_text, "replaces", f"[{', '.join(replaced)}]")
    accepted = accepted_text.encode("utf-8")
    _require_valid(destination, accepted)
    changes.insert(0, (destination, accepted))
    op = memory_sync.change_core(vault, changes, tier="core", client=client)
    return {"state": "approved", "path": destination, "removed": relative, "op": op.id}


def reject(vault: Path, relative: str, *, client: str = "human") -> dict[str, Any]:
    """Human-only: remove a proposal without installing it."""
    _require_v3(vault)
    _proposal(vault, relative)
    op = memory_sync.change_core(vault, [(relative, None)], tier="proposal", client=client)
    return {"state": "rejected", "removed": relative, "op": op.id}
