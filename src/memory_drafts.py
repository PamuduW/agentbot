"""Propose one local v2 draft, and review drafts without changing anything.

A proposal is a file under the vault's Git-ignored ``drafts/`` directory with a
fresh UUID, ``status: draft``, and the body the caller supplied. It is never
accepted memory, never tracked, and never a retrieval result; ``memory
approve`` is the only way out of ``drafts/``.

Nothing is captured implicitly: the body comes from a named file or standard
input, never a conversation. A proposal that fails schema checks or trips a
blocking secret rule writes nothing. Installation is exclusive creation in the
drafts directory, reached without following a symlink, so an existing file is
never replaced.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .memory import (
    MAX_FILE_BYTES,
    README_EXEMPT,
    WARNING_RULES,
    Finding,
    MemoryVaultError,
    Record,
    _decode,
    _Destination,
    _front_matter,
    _git,
    _RecordCheck,
    _Unsafe,
    _walk_drafts,
    read_bounded,
    read_marker,
    scan_secrets,
    validate,
)

DRAFTS = "drafts"
REVIEW_LIST_MAX = 100
TITLE_SLUG_MAX = 80
DESTINATION_DIRECTORIES = {"decision": "decisions", "lesson": "lessons"}
SECRET_RULE_PREFIXES = ("SECRET_", "WARN_")
REDACTED = "[redacted: secret finding]"


def title_slug(title: str) -> str:
    """Lowercase ASCII, other runs to one hyphen, trimmed, at most 80 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:TITLE_SLUG_MAX].rstrip("-")


def destination_for(kind: str, created: str, title: str, projects: Iterable[str]) -> str | None:
    """The exact path approval would install a draft at, or None when undefined."""
    slug = title_slug(title)
    if not slug:
        return None
    name = f"{created}-{slug}.md"
    if kind == "project":
        listed = list(projects)
        return f"projects/{listed[0]}/{name}" if len(listed) == 1 else None
    directory = DESTINATION_DIRECTORIES.get(kind)
    return f"{directory}/{name}" if directory else None


# --- Propose -----------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    kind: str
    title: str
    scope: str
    projects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass
class ProposalResult:
    created: bool
    path: str | None = None
    id: str | None = None
    destination: str | None = None
    findings: list[Finding] = field(default_factory=list)


def read_body(source: Path | None, stdin: Any = None) -> str:
    """The proposal body, bounded and UTF-8, from one named file or stdin."""
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


def _yaml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(values) + "]"


def _yaml_string(value: str) -> str:
    # Double-quoted with JSON escaping is valid YAML and survives any title.
    return json.dumps(value, ensure_ascii=False)


def render_draft(proposal: Proposal, record_id: str, created: str, body: str) -> str:
    lines = [
        "---",
        "schema: 2",
        f"id: {record_id}",
        f"type: {proposal.kind}",
        f"title: {_yaml_string(proposal.title)}",
        f"date: {created}",
        "status: draft",
        f"scope: {proposal.scope}",
        f"projects: {_yaml_list(proposal.projects)}",
        f"tags: {_yaml_list(proposal.tags)}",
        "---",
        "",
    ]
    return "\n".join(lines) + body if body.endswith("\n") else "\n".join(lines) + body + "\n"


def propose(
    root: Path,
    proposal: Proposal,
    body: str,
    *,
    acknowledge: Iterable[str] = (),
    today: date | None = None,
) -> ProposalResult:
    """Write one draft, or write nothing and say why."""
    if read_marker(root) != 2:
        raise MemoryVaultError(
            "proposals need a schema 2 vault; this vault is schema 1 until it is migrated"
        )
    created = (today or datetime.now(timezone.utc).date()).isoformat()
    taken = {record.id for record in validate(root).records if record.id}
    record_id = str(uuid.uuid4())
    while record_id in taken:
        record_id = str(uuid.uuid4())
    name = f"{created}-{title_slug(proposal.title) or 'draft'}-{record_id[:8]}.md"
    relative = f"{DRAFTS}/{name}"
    text = render_draft(proposal, record_id, created, body)

    result = ProposalResult(created=False, path=relative, id=record_id)
    result.destination = destination_for(proposal.kind, created, proposal.title, proposal.projects)
    result.findings = _proposal_findings(relative, text, set(acknowledge) & WARNING_RULES)
    if result.destination is None and not result.findings:
        result.findings.append(
            Finding(
                "error", "MEMORY_DESTINATION", relative, "no approval destination can be derived"
            )
        )
    if result.findings:
        return result
    if not _is_ignored(root, relative):
        result.findings.append(
            Finding(
                "error",
                "MEMORY_DRAFT_TRACKED",
                relative,
                "drafts/ is not Git-ignored in this vault",
            )
        )
        return result
    _install(root, name, text.encode("utf-8"))
    result.created = True
    return result


def _proposal_findings(relative: str, text: str, acknowledged: set[str]) -> list[Finding]:
    findings = [
        item
        for item in scan_secrets(relative, text)
        if not (item.severity == "warning" and item.rule in acknowledged)
    ]
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        findings.append(
            Finding("error", "MEMORY_SIZE", relative, f"larger than {MAX_FILE_BYTES} bytes")
        )
    try:
        fields = _front_matter(text)
    except _Unsafe as error:
        return [*findings, Finding("error", error.rule, relative, error.message)]
    check = _RecordCheck(2, relative, _Destination(None, draft=True))
    check.run(fields)
    return [*findings, *check.findings]


def _is_ignored(root: Path, relative: str) -> bool:
    return _git(root, "check-ignore", "-q", "--no-index", relative).returncode == 0


def _install(root: Path, name: str, data: bytes) -> None:
    """Exclusive creation beneath drafts/, following no symlink."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    try:
        vault = os.open(root, flags)
    except OSError as error:
        raise MemoryVaultError("the vault root is a symlink or unreadable") from error
    try:
        try:
            os.mkdir(DRAFTS, 0o700, dir_fd=vault)
        except FileExistsError:
            pass
        try:
            drafts = os.open(DRAFTS, flags, dir_fd=vault)
        except OSError as error:
            raise MemoryVaultError("drafts/ is a symlink or not a directory") from error
    finally:
        os.close(vault)
    try:
        create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            handle = os.open(name, create, 0o600, dir_fd=drafts)
        except FileExistsError as error:
            raise MemoryVaultError("a draft with that name already exists") from error
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name, dir_fd=drafts)
            raise
        os.fsync(drafts)
    finally:
        os.close(drafts)


# --- Review ------------------------------------------------------------------


@dataclass(frozen=True)
class DraftSummary:
    path: str
    title: str | None
    type: str | None
    date: str | None
    projects: tuple[str, ...]
    blocking: int
    warnings: int


@dataclass
class DraftReview:
    path: str
    fields: dict[str, Any]
    findings: list[Finding]
    destination: str | None
    destination_exists: bool
    duplicate_titles: list[str]
    body_bytes: int
    body_lines: int


def _pending(root: Path) -> list[str]:
    return sorted(
        path for path in _walk_drafts(root) if path.endswith(".md") and path not in README_EXEMPT
    )


def _lenient_fields(root: Path, relative: str) -> dict[str, Any]:
    try:
        return _front_matter(_decode(read_bounded(root, relative, MAX_FILE_BYTES)))
    except (_Unsafe, OSError):
        return {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def list_drafts(root: Path) -> tuple[list[DraftSummary], int]:
    """At most 100 pending drafts, newest first, and how many exist in total."""
    report = validate(root)
    counts: dict[str, list[int]] = {}
    for item in report.findings:
        tally = counts.setdefault(item.path, [0, 0])
        tally[0 if item.severity == "error" else 1] += 1
    flagged = {item.path for item in report.findings if item.rule.startswith(SECRET_RULE_PREFIXES)}
    summaries = []
    for relative in _pending(root):
        fields = {} if relative in flagged else _lenient_fields(root, relative)
        projects = fields.get("projects")
        summaries.append(
            DraftSummary(
                path=relative,
                title=REDACTED if relative in flagged else _text(fields.get("title")),
                type=_text(fields.get("type")),
                date=_text(fields.get("date")),
                projects=tuple(p for p in projects if isinstance(p, str))
                if isinstance(projects, list)
                else (),
                blocking=counts.get(relative, [0, 0])[0],
                warnings=counts.get(relative, [0, 0])[1],
            )
        )
    summaries.sort(key=lambda item: item.path)
    summaries.sort(key=lambda item: item.date or "", reverse=True)
    return summaries[:REVIEW_LIST_MAX], len(summaries)


def review_draft(root: Path, relative: str) -> DraftReview:
    """Everything a reviewer needs about one draft, without its body."""
    if (
        not relative.startswith(f"{DRAFTS}/")
        or not relative.endswith(".md")
        or relative in README_EXEMPT
    ):
        raise MemoryVaultError("review takes one drafts/<file>.md path")
    try:
        text = _decode(read_bounded(root, relative, MAX_FILE_BYTES))
    except _Unsafe as error:
        raise MemoryVaultError(f"{relative}: {error.message}") from error
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{relative}: no such draft") from error
    report = validate(root)
    findings = [item for item in report.findings if item.path == relative]
    fields = _lenient_fields(root, relative)
    body = _body(text)
    if not body.strip():
        findings.append(
            Finding("warning", "MEMORY_EVIDENCE_EMPTY", relative, "the draft body is empty")
        )
    title = _text(fields.get("title"))
    kind = _text(fields.get("type"))
    created = _text(fields.get("date"))
    listed = fields.get("projects")
    projects = listed if isinstance(listed, list) else []
    destination = (
        destination_for(kind, created, title, [str(p) for p in projects])
        if title and kind and created
        else None
    )
    # A secret can sit in front matter as easily as in the body. When the
    # scanner fires, show which keys exist and nothing they hold.
    if any(item.rule.startswith(SECRET_RULE_PREFIXES) for item in findings):
        shown: dict[str, Any] = dict.fromkeys(sorted(fields), REDACTED)
    else:
        shown = {key: fields[key] for key in sorted(fields)}
    return DraftReview(
        path=relative,
        fields=shown,
        findings=findings,
        destination=destination,
        destination_exists=destination is not None and os.path.lexists(root / destination),
        duplicate_titles=_duplicates(report.records, relative, title),
        body_bytes=len(body.encode("utf-8")),
        body_lines=len(body.splitlines()),
    )


def _body(text: str) -> str:
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None)
    return "" if end is None else "\n".join(lines[end + 1 :])


def _duplicates(records: list[Record], relative: str, title: str | None) -> list[str]:
    if not title:
        return []
    wanted = title.strip().casefold()
    return sorted(
        record.path
        for record in records
        if record.path != relative and record.title.strip().casefold() == wanted
    )


# --- JSON --------------------------------------------------------------------


def _finding_json(item: Finding) -> dict[str, Any]:
    return {
        "severity": item.severity,
        "rule": item.rule,
        "path": item.path,
        "line": item.line,
        "message": item.message,
    }


def proposal_json(result: ProposalResult) -> dict[str, Any]:
    return {
        "state": "created" if result.created else "refused",
        "path": result.path,
        "id": result.id,
        "destination": result.destination,
        "findings": [_finding_json(item) for item in result.findings],
    }


def draft_list_json(summaries: list[DraftSummary], total: int) -> dict[str, Any]:
    return {
        "total": total,
        "shown": len(summaries),
        "drafts": [
            {
                "path": item.path,
                "title": item.title,
                "type": item.type,
                "date": item.date,
                "projects": list(item.projects),
                "blocking": item.blocking,
                "warnings": item.warnings,
            }
            for item in summaries
        ],
    }


def draft_review_json(review: DraftReview) -> dict[str, Any]:
    return {
        "path": review.path,
        "fields": review.fields,
        "destination": review.destination,
        "destination_exists": review.destination_exists,
        "duplicate_titles": review.duplicate_titles,
        "body_bytes": review.body_bytes,
        "body_lines": review.body_lines,
        "findings": [_finding_json(item) for item in review.findings],
    }
