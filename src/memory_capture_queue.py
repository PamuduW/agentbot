"""Bounded private delivery queue; entries are pending input, never accepted memory."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    memory,
    memory_capture,
    memory_project_writes,
    memory_projects,
    memory_proposals,
    memory_sync,
)
from .atomic_io import write_text_atomic

MAX_ENTRIES = 64
MAX_BYTES = 2 * 1024 * 1024
BODY_BYTES = 16_384
REQUIRED = {"event_id", "client", "target", "kind", "title", "body"}
OPTIONAL = {"tags", "supersedes", "review_after", "valid_until"}


def _request(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) - REQUIRED - OPTIONAL or REQUIRED - set(request):
        raise memory.MemoryVaultError("capture request has unknown or missing fields")
    if any(not isinstance(request[key], str) for key in REQUIRED):
        raise memory.MemoryVaultError("capture fields must be strings")
    if request["client"] not in memory_capture.CLIENTS or request["target"] not in {
        "project",
        "proposal",
    }:
        raise memory.MemoryVaultError("capture client or target is unsupported")
    try:
        event = str(uuid.UUID(request["event_id"]))
    except ValueError:
        raise memory.MemoryVaultError("capture event-id must be a UUID") from None
    if len(request["body"].encode()) > BODY_BYTES:
        raise memory.MemoryVaultError("capture body exceeds the private queue limit")
    tags = request.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise memory.MemoryVaultError("capture tags must be strings")
    if any(
        request.get(key) is not None and not isinstance(request[key], str)
        for key in OPTIONAL - {"tags"}
    ):
        raise memory.MemoryVaultError("capture metadata must be strings")
    result = {**request, "event_id": event, "tags": sorted(set(tags))}
    for key in OPTIONAL - {"tags"}:
        result.setdefault(key, None)
    texts = [value for value in result.values() if isinstance(value, str)] + result["tags"]
    memory_capture.check_secrets(texts)
    return result


def _validate_record(result: dict[str, Any], folder: str) -> None:
    if result["target"] == "project":
        if result["kind"] not in memory_project_writes.KIND_DIRS:
            raise memory.MemoryVaultError("unsupported project capture kind")
        data = memory_project_writes.render(
            record_id=str(uuid.uuid4()),
            kind=memory_project_writes.KIND_TYPES[result["kind"]],
            title=result["title"],
            folder=folder,
            body=result["body"],
            tags=result["tags"],
            supersedes=[result["supersedes"]] if result["supersedes"] else None,
            review_after=result["review_after"],
            valid_until=result["valid_until"],
        )
        relative = f"projects/{folder}/{memory_project_writes.KIND_DIRS[result['kind']]}/capture.md"
        findings = memory_project_writes._check(relative, data)
    else:
        if result["kind"] not in memory_proposals.KIND_DIRS:
            raise memory.MemoryVaultError("unsupported proposal capture kind")
        fields = {
            "schema": 3,
            "id": str(uuid.uuid4()),
            "type": result["kind"],
            "title": result["title"],
            "date": datetime.now(timezone.utc).date().isoformat(),
            "status": "draft",
            "scope": "global",
            "projects": [],
            "tags": result["tags"],
        }
        fields.update(
            {key: result[key] for key in ("review_after", "valid_until") if result[key] is not None}
        )
        if result["supersedes"]:
            fields["supersedes"] = [result["supersedes"]]
        relative = f"proposals/core/{memory_proposals.KIND_DIRS[result['kind']]}/capture.md"
        destination = memory._destination_v3(relative)
        assert destination is not None
        check = memory._RecordCheck(3, relative, destination)
        check.run(fields)
        findings = check.findings
    if findings:
        raise memory.MemoryVaultError("capture metadata is invalid")


def _private(path: Path, mode: int, *, directory: bool = False) -> None:
    info = path.lstat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise memory.MemoryVaultError("capture queue needs regular private state paths")


@contextlib.contextmanager
def _locked(config_home: Path) -> Iterator[Path]:
    directory = config_home / "memory-capture"
    if directory.is_symlink():
        raise memory.MemoryVaultError("capture queue directory cannot be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(directory, 0o700, directory=True)
    descriptor = os.open(
        directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    with os.fdopen(descriptor, "rb") as handle:
        _private(directory / "lock", 0o600)
        deadline = time.monotonic() + 0.25
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise memory.MemoryVaultError("capture queue is busy; retry") from None
                time.sleep(0.01)
        try:
            yield directory / "queue.json"
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists() and not path.is_symlink():
        return []
    _private(path.parent, 0o700, directory=True)
    _private(path, 0o600)
    try:
        data = json.loads(memory.read_bounded(path.parent, path.name, MAX_BYTES))
    except (ValueError, memory._Unsafe):
        raise memory.MemoryVaultError(
            "capture queue is unreadable; preserve it for recovery"
        ) from None
    if (
        not isinstance(data, list)
        or len(data) > MAX_ENTRIES
        or any(not _entry(item) for item in data)
    ):
        raise memory.MemoryVaultError("capture queue has invalid entries; preserve it for recovery")
    return data


def _entry(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    for key in ("queue_id", "project_id", "vault", "cwd"):
        if not isinstance(item.get(key), str) or not item[key]:
            return False
    return (
        isinstance(item.get("request"), dict)
        and type(item.get("attempts")) is int
        and item["attempts"] >= 0
    )


def _checkout(cwd: Path) -> Path:
    identity = memory_projects.repo_identity(cwd)
    if identity is None:
        raise memory.MemoryVaultError("capture queue needs a registered checkout")
    return identity.toplevel


def _write(path: Path, entries: list[dict[str, Any]]) -> None:
    encoded = json.dumps(entries, ensure_ascii=False) + "\n"
    if len(encoded.encode()) > MAX_BYTES:
        raise memory.MemoryVaultError("capture queue is full")
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
    _private(path, 0o600)
    write_text_atomic(path, encoded)


def summary(vault: Path, config_home: Path) -> dict[str, int]:
    """Read pending input counts for this vault without creating state or exposing facts."""
    entries = _read(config_home / "memory-capture" / "queue.json")
    own = [item for item in entries if item["vault"] == str(vault.resolve())]
    return {"pending": len(own), "retried": sum(item["attempts"] > 0 for item in own)}


def enqueue(
    vault: Path,
    cwd: Path,
    config_home: Path,
    request: dict[str, Any],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    cwd = _checkout(cwd)
    project = memory_projects.require_project(vault, cwd, config_home)
    if project_id is not None and project["id"] != project_id:
        raise memory.MemoryVaultError("capture project identity changed")
    request = _request(request)
    key = memory_capture._digest([project["id"], request["client"], request["event_id"]])
    entry = {
        "queue_id": key,
        "project_id": project["id"],
        "vault": str(vault.resolve()),
        "cwd": str(cwd.resolve()),
        "request": request,
        "attempts": 0,
    }
    with _locked(config_home) as path:
        entries = _read(path)
        prior = next((item for item in entries if item.get("queue_id") == key), None)
        if prior is not None:
            if (
                prior.get("request") != request
                or prior.get("vault") != entry["vault"]
                or prior.get("cwd") != entry["cwd"]
            ):
                raise memory.MemoryVaultError(
                    "capture event already queued with a different payload or checkout"
                )
        else:
            _validate_record(request, project["folder"])
            if len(entries) >= MAX_ENTRIES:
                raise memory.MemoryVaultError("capture queue is full")
            _write(path, [*entries, entry])
    return {"state": "queued", "queue_id": key}


def _belongs(entry: dict[str, Any], vault: Path, cwd: Path, project_id: str) -> bool:
    return (
        entry.get("vault") == str(vault.resolve())
        and entry.get("cwd") == str(cwd.resolve())
        and entry.get("project_id") == project_id
    )


def status(
    vault: Path, cwd: Path, config_home: Path, *, project_id: str | None = None
) -> dict[str, Any]:
    cwd = _checkout(cwd)
    project = memory_projects.require_project(vault, cwd, config_home)
    if project_id is not None and project["id"] != project_id:
        raise memory.MemoryVaultError("capture project identity changed")
    entries = _read(config_home / "memory-capture" / "queue.json")
    scoped = [item for item in entries if _belongs(item, vault, cwd, project["id"])]
    return {
        "state": "pending",
        "pending": len(scoped),
        "other_pending": len(entries) - len(scoped),
        "events": [
            {key: item.get(key) for key in ("queue_id", "attempts", "reason")} for item in scoped
        ],
    }


def discard(
    vault: Path, cwd: Path, config_home: Path, queue_id: str, *, confirm: bool = False
) -> dict[str, Any]:
    cwd = _checkout(cwd)
    project = memory_projects.require_project(vault, cwd, config_home)
    with _locked(config_home) as path:
        entries = _read(path)
        entry = next(
            (
                item
                for item in entries
                if item["queue_id"] == queue_id and _belongs(item, vault, cwd, project["id"])
            ),
            None,
        )
        if entry is None:
            raise memory.MemoryVaultError("no queued event belongs to this checkout with that ID")
        if confirm:
            entries.remove(entry)
            _write(path, entries)
    return {"state": "discarded" if confirm else "preview", "queue_id": queue_id}


def drain(
    vault: Path,
    cwd: Path,
    config_home: Path,
    *,
    limit: int = 1,
    queue_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 8:
        raise memory.MemoryVaultError("capture drain limit must be between 1 and 8")
    cwd = _checkout(cwd)
    project = memory_projects.require_project(vault, cwd, config_home)
    if project_id is not None and project["id"] != project_id:
        raise memory.MemoryVaultError("capture project identity changed")
    attempted = committed = 0
    events = []
    with _locked(config_home) as path:
        entries = _read(path)
        for entry in list(entries):
            if (
                attempted >= limit
                or not _belongs(entry, vault, cwd, project["id"])
                or (queue_id is not None and entry["queue_id"] != queue_id)
            ):
                continue
            attempted += 1
            state = "pending"
            reason = ""
            try:
                with memory_sync._locked(vault):
                    current = memory_projects.require_project(vault, cwd, config_home)
                    if current["id"] != entry["project_id"]:
                        raise memory.MemoryVaultError("capture project identity changed")
                    request = _request(entry["request"])
                    result = memory_capture.save(
                        vault, cwd, config_home, project_id=current["id"], **request
                    )
                state = result["state"]
            except OSError as error:
                reason = f"io-{error.errno}"
            except memory.MemoryVaultError:
                reason = "request-or-vault-unavailable"
            if state == "committed-locally":
                entries.remove(entry)
                committed += 1
            else:
                entry["attempts"] += 1
                entry["reason"] = reason or state
            _write(path, entries)
            event = {"queue_id": entry["queue_id"], "state": state, "reason": reason}
            if state == "committed-locally":
                event["receipt"] = result["receipt"]
            events.append(event)
    pending = sum(_belongs(item, vault, cwd, project["id"]) for item in entries)
    return {
        "state": "pending" if pending else "drained",
        "attempted": attempted,
        "committed": committed,
        "pending": pending,
        "events": events,
    }
