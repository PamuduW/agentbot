"""Bounded local recall and capture reminders; receipts never prove model use."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import memory, memory_capture, memory_projects, memory_read, memory_retrieve
from .atomic_io import write_text_atomic

MAX_RECEIPTS = 256
REMINDER = (
    "Memory checkpoint: keep verified small tool workarounds, repeated corrections and active "
    "task details that matter later. Use Agentbot capture/project writes for project facts; "
    "personal or global facts must remain proposals. Exclude secrets, transcripts and guesses. "
    "Give each extracted fact its own stable event UUID for retries. Queued is pending, not "
    "saved; report saved only after a committed receipt. This reminder saved no fact."
)


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise memory.MemoryVaultError("invalid memory session identifier")
    return value


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not kind
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
    ):
        raise memory.MemoryVaultError("memory receipts need private regular state paths")


def _record(config_home: Path, key: str, client: str, kind: str) -> dict[str, Any]:
    directory = config_home / "memory-sessions"
    if directory.is_symlink():
        raise memory.MemoryVaultError("memory receipt directory cannot be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(directory, directory=True)
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as handle:
        _private(directory / "lock")
        deadline = time.monotonic() + 0.25
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise memory.MemoryVaultError("memory receipt state is busy") from None
                time.sleep(0.01)
        path = directory / "receipts.json"
        try:
            if path.exists() or path.is_symlink():
                _private(path)
                if path.stat().st_size > 131_072:
                    raise memory.MemoryVaultError("memory receipts exceed the state limit")
                items = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(items, dict)
                    or len(items) > MAX_RECEIPTS
                    or any(
                        not re.fullmatch(r"[a-f0-9]{64}", key)
                        or not isinstance(item, dict)
                        or set(item) != {"client", "kind", "state", "time"}
                        or item["client"] not in memory_capture.CLIENTS
                        or item["kind"] not in ("recall", "checkpoint")
                        or item["state"] != "prepared"
                        or not isinstance(item["time"], str)
                        or len(item["time"]) > 40
                        for key, item in items.items()
                    )
                ):
                    raise memory.MemoryVaultError("invalid memory receipt state")
            else:
                items = {}
            duplicate = key in items
            items.pop(key, None)
            items[key] = {
                "client": client,
                "kind": kind,
                "state": "prepared",
                "time": datetime.now(timezone.utc).isoformat(),
            }
            items = dict(list(items.items())[-MAX_RECEIPTS:])
            if not path.exists():
                created = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(created)
            write_text_atomic(path, json.dumps(items) + "\n")
            return {"id": key, "state": "recorded", "duplicate": duplicate}
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _prepare(
    vault: Path,
    cwd: Path,
    config_home: Path,
    *,
    client: str,
    session_id: str,
    event_id: str,
    kind: str,
    include_memory: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if client not in memory_capture.CLIENTS:
        raise memory.MemoryVaultError("unsupported memory client")
    _identifier(session_id)
    _identifier(event_id)

    def read() -> tuple[str, dict[str, Any]]:
        project = memory_projects.require_project(vault, cwd, config_home)
        data = (
            memory_retrieve.brief_json(
                memory_retrieve.brief(vault, memory_retrieve.Request(project=project["folder"]))
            )
            if include_memory
            else {}
        )
        return project["id"], data

    project, data = memory_read.consistent(vault, read)
    key = hashlib.sha256(
        json.dumps([str(vault.resolve()), project, client, session_id, kind, event_id]).encode()
    ).hexdigest()
    try:
        receipt = _record(config_home, key, client, kind)
    except (OSError, ValueError, RecursionError, memory.MemoryVaultError):
        receipt = {"id": key, "state": "unrecorded", "duplicate": False}
    return data, receipt


def recall(
    vault: Path,
    cwd: Path,
    config_home: Path,
    *,
    client: str,
    session_id: str,
    event_id: str = "recall",
) -> dict[str, Any]:
    data, receipt = _prepare(
        vault,
        cwd,
        config_home,
        client=client,
        session_id=session_id,
        event_id=event_id,
        kind="recall",
        include_memory=True,
    )
    return {
        "state": "prepared",
        "memory": data,
        "receipt": receipt,
        "model_use_verified": False,
        "refresh": {"state": "skipped", "reason": "local-only", "local_only": True},
    }


def checkpoint(
    vault: Path,
    cwd: Path,
    config_home: Path,
    *,
    client: str,
    session_id: str,
    event_id: str,
) -> dict[str, Any]:
    _, receipt = _prepare(
        vault,
        cwd,
        config_home,
        client=client,
        session_id=session_id,
        event_id=event_id,
        kind="checkpoint",
        include_memory=False,
    )
    return {
        "state": "prepared",
        "saved": False,
        "receipt": receipt,
        "duplicate": receipt["duplicate"],
        "reminder": "" if receipt["duplicate"] else REMINDER,
    }
