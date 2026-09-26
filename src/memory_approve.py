"""Promote one reviewed draft into canonical memory, never replacing a file.

Preview is the default. Apply takes a bounded per-vault lock held in
Agentbot's private state (never in the vault), re-reads the draft and the
destination inside it, writes the accepted record to a temporary file in the
destination directory, and installs it with ``link()``, which fails when the
destination exists. The draft is removed only after installation succeeds, and
only if it is still the bytes that were approved. Every failure leaves the
draft intact and no partial destination. Nothing is staged, committed, or
pushed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import (
    MAX_FILE_BYTES,
    WARNING_RULES,
    Finding,
    MemoryVaultError,
    _decode,
    _destination,
    _front_matter,
    _RecordCheck,
    _Unsafe,
    read_bounded,
    read_marker,
    scan_secrets,
    validate,
)
from .memory_drafts import DRAFTS, destination_for

LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.1
_DIRECTORY = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)


class ApprovalConflict(MemoryVaultError):
    """Something changed underneath the approval; the draft is intact."""


@dataclass
class ApprovalResult:
    state: str  # preview, refused, conflict, applied
    draft: str
    destination: str | None = None
    id: str | None = None
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Plan:
    draft: str
    destination: str
    record_id: str
    draft_digest: str
    content: bytes


def lock_path(config_home: Path, root: Path) -> Path:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return config_home / "memory-locks" / f"{digest}.lock"


def approve(
    root: Path,
    relative: str,
    *,
    config_home: Path,
    apply: bool = False,
    acknowledge: Iterable[str] = (),
    lock_wait: float = LOCK_WAIT_SECONDS,
    before_install: Callable[[], None] | None = None,
) -> ApprovalResult:
    """Preview, or with apply=True install, one draft as an accepted record."""
    result = ApprovalResult(state="refused", draft=relative)
    plan = _plan(root, relative, set(acknowledge) & WARNING_RULES, result)
    if plan is None:
        return result
    if os.path.lexists(root / plan.destination):
        result.state = "conflict"
        result.notes.append(f"{plan.destination} already exists")
        return result
    if not apply:
        result.state = "preview"
        return result
    try:
        with _promotion_lock(lock_path(config_home, root), lock_wait, result.notes):
            _recheck(root, plan)
            if before_install is not None:
                before_install()
            _install(root, plan.destination, plan.content)
            result.state = "applied"
            _remove_draft(root, plan, result.notes)
    except ApprovalConflict as error:
        result.state = "conflict"
        result.notes.append(str(error))
    return result


def _plan(
    root: Path, relative: str, acknowledged: set[str], result: ApprovalResult
) -> _Plan | None:
    def refuse(rule: str, message: str) -> None:
        result.findings.append(Finding("error", rule, relative, message))

    if read_marker(root) != 2:
        raise MemoryVaultError(
            "approval needs a schema 2 vault; this vault is schema 1 until it is migrated"
        )
    if not relative.startswith(f"{DRAFTS}/") or not relative.endswith(".md"):
        raise MemoryVaultError("approve takes one drafts/<file>.md path")
    try:
        raw = read_bounded(root, relative, MAX_FILE_BYTES)
        text = _decode(raw)
        fields = _front_matter(text)
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{relative}: no such draft") from error
    except _Unsafe as error:
        refuse(error.rule, error.message)
        return None
    # The whole tree's view of this draft: schema, secrets, and ID uniqueness.
    report = validate(root)
    result.findings.extend(
        item
        for item in report.findings
        if item.path == relative and not (item.severity == "warning" and item.rule in acknowledged)
    )
    # Tree validation reports a shared ID on whichever path sorts later, so
    # check the draft's own ID against every other record explicitly.
    if any(record.id == fields.get("id") and record.path != relative for record in report.records):
        refuse("MEMORY_DUPLICATE_ID", "another record already uses this draft's id")
    if "supersedes" in fields:
        refuse(
            "MEMORY_SUPERSEDES",
            "approving a superseding draft needs the supersession transaction, not built yet",
        )
    if result.findings:
        return None
    destination = destination_for(
        fields["type"], fields["date"], fields["title"], fields["projects"]
    )
    if destination is None:
        refuse("MEMORY_DESTINATION", "no approval destination can be derived")
        return None
    result.destination, result.id = destination, fields["id"]
    accepted = _accept(text)
    if accepted is None:
        refuse("MEMORY_STATUS", "front matter needs exactly one 'status: draft' line")
        return None
    findings = _canonical_findings(destination, accepted, acknowledged)
    if findings:
        result.findings.extend(findings)
        return None
    return _Plan(
        draft=relative,
        destination=destination,
        record_id=fields["id"],
        draft_digest=hashlib.sha256(raw).hexdigest(),
        content=accepted.encode("utf-8"),
    )


def _accept(text: str) -> str | None:
    """The draft with its one status line changed to accepted; the body untouched."""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None)
    if end is None:
        return None
    hits = [i for i in range(1, end) if lines[i].rstrip("\r").rstrip() == "status: draft"]
    if len(hits) != 1:
        return None
    ending = "\r" if lines[hits[0]].endswith("\r") else ""
    lines[hits[0]] = "status: accepted" + ending
    return "\n".join(lines)


def _canonical_findings(destination: str, text: str, acknowledged: set[str]) -> list[Finding]:
    where = _destination(destination)
    if where is None:
        return [Finding("error", "MEMORY_LOCATION", destination, "not a record location")]
    findings = [
        item
        for item in scan_secrets(destination, text)
        if not (item.severity == "warning" and item.rule in acknowledged)
    ]
    check = _RecordCheck(2, destination, where)
    check.run(_front_matter(text))
    return [*findings, *check.findings]


# --- Lock --------------------------------------------------------------------


def _holder(path: Path) -> tuple[int | None, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload["pid"]), str(payload.get("acquired", "unknown time"))
    except (OSError, ValueError, KeyError, TypeError):
        return None, "unknown time"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def _promotion_lock(path: Path, wait: float, notes: list[str]) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise MemoryVaultError("the memory lock directory is a symlink")
    deadline = time.monotonic() + wait
    payload = json.dumps(
        {"pid": os.getpid(), "acquired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    )
    while True:
        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            break
        except FileExistsError:
            pid, acquired = _holder(path)
            if pid is None or not _alive(pid):
                # A holder that is gone cannot release; break it and say so.
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                notes.append(f"broke a stale lock (pid {pid or 'unknown'}, {acquired})")
                continue
            if time.monotonic() >= deadline:
                raise ApprovalConflict(
                    f"another approval holds the vault lock (pid {pid}, since {acquired})"
                ) from None
            time.sleep(LOCK_POLL_SECONDS)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload)
    previous = _raise_on_sigterm()
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _raise_on_sigterm() -> Any:
    """Turn SIGTERM into SystemExit so the lock and temp files are cleaned up."""

    def handler(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    try:
        return signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread
        return None


# --- Install -----------------------------------------------------------------


def _recheck(root: Path, plan: _Plan) -> None:
    try:
        current = read_bounded(root, plan.draft, MAX_FILE_BYTES)
    except (_Unsafe, OSError) as error:
        raise ApprovalConflict(f"{plan.draft} changed or disappeared during approval") from error
    if hashlib.sha256(current).hexdigest() != plan.draft_digest:
        raise ApprovalConflict(f"{plan.draft} changed during approval")
    if os.path.lexists(root / plan.destination):
        raise ApprovalConflict(f"{plan.destination} appeared during approval")


def _open_parent(root: Path, relative: str) -> int:
    """A handle on the destination's directory, created as needed, no symlinks."""
    try:
        directory = os.open(root, _DIRECTORY)
    except OSError as error:
        raise MemoryVaultError("the vault root is a symlink or unreadable") from error
    for part in relative.split("/")[:-1]:
        with contextlib.suppress(FileExistsError):
            os.mkdir(part, 0o755, dir_fd=directory)
        try:
            child = os.open(part, _DIRECTORY, dir_fd=directory)
        except OSError as error:
            os.close(directory)
            raise MemoryVaultError(f"{part}/ is a symlink or not a directory") from error
        os.close(directory)
        directory = child
    return directory


def _install(root: Path, relative: str, content: bytes) -> None:
    directory = _open_parent(root, relative)
    name = relative.rsplit("/", 1)[-1]
    temporary = f".agentbot-approve-{os.getpid()}-{time.monotonic_ns()}.tmp"
    try:
        handle = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise ApprovalConflict(f"{relative} appeared during approval") from error
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def _remove_draft(root: Path, plan: _Plan, notes: list[str]) -> None:
    try:
        current = read_bounded(root, plan.draft, MAX_FILE_BYTES)
    except (_Unsafe, OSError):
        notes.append(f"{plan.draft} was already gone after installation")
        return
    if hashlib.sha256(current).hexdigest() != plan.draft_digest:
        notes.append(f"{plan.draft} changed after installation and was kept; remove it by hand")
        return
    directory = _open_parent(root, plan.draft)
    try:
        os.unlink(plan.draft.rsplit("/", 1)[-1], dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def approval_json(result: ApprovalResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "draft": result.draft,
        "destination": result.destination,
        "id": result.id,
        "notes": result.notes,
        "findings": [
            {
                "severity": item.severity,
                "rule": item.rule,
                "path": item.path,
                "line": item.line,
                "message": item.message,
            }
            for item in result.findings
        ],
    }
