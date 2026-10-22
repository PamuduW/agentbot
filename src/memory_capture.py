"""Project-bound capture receipts over the existing validated writer transaction."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import memory_project_writes, memory_projects, memory_proposals, memory_read, memory_sync
from .memory import MemoryVaultError, scan_secrets

CLIENTS = ("claude", "codex", "cursor", "antigravity")


def check_secrets(texts: Iterable[str]) -> None:
    if any(f.severity == "error" for text in texts for f in scan_secrets("capture", text)):
        raise MemoryVaultError("capture blocked by secret scanner")


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def _result(op: str, capture: dict[str, str], *, state: str, replayed: bool) -> dict[str, Any]:
    return {
        "state": state,
        "replayed": replayed,
        "receipt": {
            "op": capture.get("op", op),
            **{key: capture[key] for key in ("id", "path", "target")},
        },
    }


def _history(vault: Path, key: str, payload: str) -> dict[str, Any] | None:
    # Receipts travel with the same operation when sync recreates its commit.
    found = memory_sync._git(
        vault,
        "log",
        "-1",
        "--format=%B",
        "--fixed-strings",
        f'--grep="key": "{key}"',
        "HEAD",
    ).stdout
    if found:
        try:
            capture = json.loads(
                next(
                    line.removeprefix("Agentbot-Capture: ")
                    for line in found.splitlines()
                    if line.startswith("Agentbot-Capture: ")
                )
            )
            op = next(
                line.removeprefix("Agentbot-Op: ")
                for line in found.splitlines()
                if line.startswith("Agentbot-Op: ")
            )
            if capture["key"] != key:
                raise ValueError("incorrect key")
        except (ValueError, KeyError, StopIteration, TypeError):
            raise MemoryVaultError("capture receipt is invalid; inspect vault history") from None
        return _checked_result(op, capture, payload, "committed-locally")
    return None


def _existing(vault: Path, key: str, payload: str) -> dict[str, Any] | None:
    found = _history(vault, key, payload)
    if found is not None:
        return found
    for item in memory_sync.pending(vault):
        capture = item.get("capture")
        if capture and capture.get("key") == key:
            return _checked_result(item["id"], capture, payload, "pending")
    for item in memory_sync.conflicts(vault):
        capture = item["op_data"].get("capture")
        if capture and capture.get("key") == key:
            state = "resolved" if item.get("resolved") else "conflict"
            return _checked_result(item["op"], capture, payload, state)
    return None


def receipt(
    vault: Path, cwd: Path, config_home: Path, request: dict[str, Any], *, project_id: str
) -> dict[str, Any] | None:
    """Look up a committed event without creating or recovering writer state."""
    try:
        event_id = str(uuid.UUID(request["event_id"]))
    except (ValueError, TypeError, KeyError):
        raise MemoryVaultError("capture event-id must be a UUID") from None
    fields = {
        key: request.get(key)
        for key in ("kind", "title", "body", "supersedes", "review_after", "valid_until")
    }
    fields["tags"] = sorted(set(request.get("tags") or []))
    payload = _digest({"target": request["target"], **fields})

    def read() -> dict[str, Any] | None:
        project = memory_projects.require_project(vault, cwd, config_home)
        if project["id"] != project_id:
            raise MemoryVaultError("capture project identity changed")
        key = _digest([project_id, request["client"], event_id])
        return _history(vault, key, payload)

    return memory_read.consistent(vault, read)


def _checked_result(op: str, capture: dict[str, str], payload: str, state: str) -> dict[str, Any]:
    if capture.get("payload") != payload:
        raise MemoryVaultError("capture event already exists with a different payload")
    try:
        return _result(op, capture, state=state, replayed=True)
    except KeyError:
        raise MemoryVaultError("capture receipt is incomplete; inspect vault history") from None


def save(
    vault: Path,
    cwd: Path,
    config_home: Path,
    *,
    event_id: str,
    client: str,
    target: str,
    kind: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    supersedes: str | None = None,
    review_after: str | None = None,
    valid_until: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Commit an extracted fact once per project/client/event in this clone's history.

    Native hooks supply a stable event UUID. They cannot select another project
    or promote core memory; identity comes from the registered calling repository.
    """
    if client not in CLIENTS or target not in {"project", "proposal"}:
        raise MemoryVaultError("capture needs a supported client and project or proposal target")
    try:
        event_id = str(uuid.UUID(event_id))
    except (ValueError, AttributeError):
        raise MemoryVaultError("capture event-id must be a UUID") from None
    tags = sorted(set(tags or []))
    check_secrets([title, body, *tags])
    options = {
        "kind": kind,
        "title": title,
        "body": body,
        "tags": tags,
        "supersedes": supersedes,
        "review_after": review_after,
        "valid_until": valid_until,
    }
    payload = _digest({"target": target, **options})
    with memory_sync._locked(vault):
        project = memory_projects.require_project(vault, cwd, config_home)
        if project_id is not None and project["id"] != project_id:
            raise MemoryVaultError("capture project identity changed")
        key = _digest([project["id"], client, event_id])
        existing = _existing(vault, key, payload)
        if existing is not None:
            return existing
        capture = {"key": key, "payload": payload, "target": target}
        if target == "project":
            written = memory_project_writes.add(
                vault,
                cwd,
                config_home,
                kind=kind,
                title=title,
                body=body,
                tags=tags,
                supersedes=supersedes,
                review_after=review_after,
                valid_until=valid_until,
                client=client,
                capture=capture,
                project_id=project["id"],
            )
        else:
            written = memory_proposals.propose(
                vault,
                kind=kind,
                title=title,
                body=body,
                tags=tags,
                supersedes=[supersedes] if supersedes else None,
                review_after=review_after,
                valid_until=valid_until,
                client=client,
                capture=capture,
            )
        return _result(
            written["op"],
            {**capture, "id": written["id"], "path": written["path"]},
            state="committed-locally",
            replayed=False,
        )
