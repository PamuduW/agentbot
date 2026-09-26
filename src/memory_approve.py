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
    Record,
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
class _Target:
    path: str
    digest: str
    original: bytes
    content: bytes | None  # None: already superseded, so nothing to rewrite


@dataclass(frozen=True)
class _Plan:
    draft: str
    destination: str
    record_id: str
    draft_digest: str
    content: bytes
    targets: tuple[_Target, ...] = ()


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
    allow_cross_scope: bool = False,
    lock_wait: float = LOCK_WAIT_SECONDS,
    before_install: Callable[[], None] | None = None,
) -> ApprovalResult:
    """Preview, or with apply=True install, one draft as an accepted record.

    A draft that supersedes records is one transition: the new record is
    installed and every accepted target is marked superseded, or nothing is.
    """
    result = ApprovalResult(state="refused", draft=relative)
    plan = _plan(root, relative, set(acknowledge) & WARNING_RULES, result, allow_cross_scope)
    if plan is None:
        return result
    for target in plan.targets:
        result.notes.append(
            f"marks {target.path} superseded"
            if target.content is not None
            else f"{target.path} is already superseded; left as is"
        )
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
            _commit(root, plan, result.notes)
            result.state = "applied"
            _remove_draft(root, plan, result.notes)
    except ApprovalConflict as error:
        result.state = "conflict"
        result.notes.append(str(error))
    return result


def _plan(
    root: Path,
    relative: str,
    acknowledged: set[str],
    result: ApprovalResult,
    allow_cross_scope: bool = False,
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
    if result.findings:
        return None
    targets = _plan_targets(root, fields, report.records, allow_cross_scope, refuse)
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
        targets=tuple(targets),
    )


def _plan_targets(
    root: Path,
    fields: dict[str, Any],
    records: list[Record],
    allow_cross_scope: bool,
    refuse: Callable[[str, str], None],
) -> list[_Target]:
    canonical = {record.id: record for record in records if record.id and not record.draft}
    targets: list[_Target] = []
    for target_id in fields.get("supersedes") or ():
        record = canonical.get(target_id)
        if record is None:
            refuse("MEMORY_SUPERSEDES_TARGET", "supersedes an ID with no canonical record")
            continue
        if record.status == "retired":
            refuse(
                "MEMORY_SUPERSEDES_STATUS",
                f"{record.path} is retired; supersede it by hand if that is intended",
            )
            continue
        if not allow_cross_scope and (record.type, record.scope) != (
            fields["type"],
            fields["scope"],
        ):
            refuse(
                "MEMORY_SUPERSEDES_SCOPE",
                f"{record.path} is a {record.scope} {record.type}; replacing it across type or "
                "scope needs --allow-cross-scope after human review",
            )
            continue
        raw = read_bounded(root, record.path, MAX_FILE_BYTES)
        content = None
        if record.status == "accepted":
            changed = _set_status(_decode(raw), "accepted", "superseded")
            where = _destination(record.path)
            if changed is None or where is None:
                refuse("MEMORY_STATUS", f"{record.path} needs exactly one 'status: accepted' line")
                continue
            # The target's state after the transition must itself be valid.
            check = _RecordCheck(2, record.path, where)
            check.run(_front_matter(changed))
            for item in check.findings:
                refuse(item.rule, f"{record.path} cannot become superseded: {item.message}")
            if check.findings:
                continue
            content = changed.encode("utf-8")
        targets.append(_Target(record.path, hashlib.sha256(raw).hexdigest(), raw, content))
    return targets


def _accept(text: str) -> str | None:
    """The draft with its one status line changed to accepted; the body untouched."""
    return _set_status(text, "draft", "accepted")


def _set_status(text: str, old: str, new: str) -> str | None:
    """Change the one front-matter status line; everything else stays byte-for-byte."""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None)
    if end is None:
        return None
    hits = [i for i in range(1, end) if lines[i].rstrip("\r").rstrip() == f"status: {old}"]
    if len(hits) != 1:
        return None
    ending = "\r" if lines[hits[0]].endswith("\r") else ""
    lines[hits[0]] = f"status: {new}" + ending
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
    for target in plan.targets:
        _check_unchanged(root, target.path, target.digest)


def _check_unchanged(root: Path, relative: str, digest: str) -> None:
    try:
        current = read_bounded(root, relative, MAX_FILE_BYTES)
    except (_Unsafe, OSError) as error:
        raise ApprovalConflict(f"{relative} changed or disappeared during approval") from error
    if hashlib.sha256(current).hexdigest() != digest:
        raise ApprovalConflict(f"{relative} changed during approval")


def _commit(root: Path, plan: _Plan, notes: list[str]) -> None:
    """Install the record, then mark each target; undo our own writes on failure."""
    _install(root, plan.destination, plan.content)
    done: list[_Target] = []
    try:
        for target in plan.targets:
            if target.content is None:
                _check_unchanged(root, target.path, target.digest)
                continue
            _rewrite(root, target.path, target.digest, target.content)
            done.append(target)
    except BaseException:
        _rollback(root, plan, done, notes)
        raise


def _rewrite(root: Path, relative: str, digest: str, content: bytes) -> None:
    """Replace one record's bytes, only while they are still the ones reviewed."""
    directory = _open_parent(root, relative)
    name = relative.rsplit("/", 1)[-1]
    temporary = f".agentbot-approve-{os.getpid()}-{time.monotonic_ns()}.tmp"
    try:
        handle = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory
        )
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Last look before replacing: a person's edit wins over this transition.
        _check_unchanged(root, relative, digest)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def _rollback(root: Path, plan: _Plan, done: list[_Target], notes: list[str]) -> None:
    """Undo only what this approval wrote, and only where it is still ours."""
    for target in reversed(done):
        if target.content is None:
            continue
        try:
            _rewrite(root, target.path, hashlib.sha256(target.content).hexdigest(), target.original)
        except (ApprovalConflict, OSError):
            notes.append(f"{target.path} changed after this approval marked it; restore it by hand")
    try:
        _check_unchanged(root, plan.destination, hashlib.sha256(plan.content).hexdigest())
    except ApprovalConflict:
        notes.append(f"{plan.destination} changed after installation; it was kept")
        return
    directory = _open_parent(root, plan.destination)
    try:
        os.unlink(plan.destination.rsplit("/", 1)[-1], dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


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
