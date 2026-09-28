"""Automatic memory synchronization (ADR-0009, ticket M6).

On a schema 3 vault in ``auto`` mode (the default):

- every Agentbot-owned write is followed by a sync, so it is pushed at once;
- a read fetches first, but at most once per ``fetch_interval`` seconds
  (default 300), so ordinary commands stay fast.

Sync is M1's engine: fetch, replay pending operations on the remote tip,
push, never force. It never breaks the calling command. Offline, writes stay
committed locally and pending; with uncommitted manual edits, automation
pauses until they are committed or reverted. ``manual`` mode does none of
this automatically: writes are committed locally and ``agentbot memory sync``
pushes them. Schema 1 and 2 vaults keep their manual Git workflow.

Conflicts are preserved by the engine. They can be listed, shown (the
current version beside the version that lost), and resolved by keeping
theirs (accept the current state) or keeping mine (re-apply the lost
operation as a new one). Core conflicts are for the human to resolve.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import memory_setup, memory_sync
from .atomic_io import write_text_atomic
from .memory import MemoryVaultError, read_marker

MODES = ("auto", "manual")
# Explicitly off for this process (the vault hooks set it), or already inside
# one of the engine's own Git commands.
NO_SYNC_ENV = "AGENTBOT_MEMORY_NO_SYNC"
DEFAULT_INTERVAL = 300
SHOW_LIMIT = 65_536


# --- Settings ------------------------------------------------------------------


def settings(config_home: Path) -> dict[str, Any]:
    data = memory_setup.read_config(config_home) or {}
    sync = data.get("sync", {})
    mode = sync.get("mode", "auto")
    interval = sync.get("fetch_interval", DEFAULT_INTERVAL)
    return {
        "mode": mode if mode in MODES else "auto",
        "fetch_interval": interval
        if isinstance(interval, int) and interval >= 0
        else DEFAULT_INTERVAL,
    }


def set_settings(
    config_home: Path, *, mode: str | None = None, fetch_interval: int | None = None
) -> dict[str, Any]:
    data = memory_setup.read_config(config_home)
    if data is None:
        raise MemoryVaultError("no vault is configured; run agentbot memory setup first")
    if mode is not None and mode not in MODES:
        raise MemoryVaultError("sync mode must be auto or manual")
    current = settings(config_home)
    if mode is not None:
        current["mode"] = mode
    if fetch_interval is not None:
        if fetch_interval < 0:
            raise MemoryVaultError("fetch interval must be zero or more seconds")
        current["fetch_interval"] = fetch_interval
    data["sync"] = current
    path = memory_setup.config_path(config_home)
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return current


# --- Running sync -----------------------------------------------------------------


def _stamp(vault: Path) -> Path:
    return memory_sync._state(vault) / "last-sync.json"


def last_sync(vault: Path) -> dict[str, Any]:
    path = _stamp(vault)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(vault: Path) -> dict[str, Any]:
    """Sync now, whatever the mode. Never raises for offline or manual edits."""
    if _nested():
        return {"state": "nested", "pending": len(memory_sync.pending(vault))}
    try:
        memory_sync.setup_obsidian(vault)
        result = memory_sync.sync(vault)
    except MemoryVaultError as error:
        message = str(error)
        state = "paused" if "uncommitted" in message or "did not make" in message else "error"
        return {"state": state, "detail": message, "pending": len(memory_sync.pending(vault))}
    outcome = {
        "state": result.state,
        "applied": len(result.applied),
        "conflicts": len(result.conflicts),
        "pending": len(memory_sync.pending(vault)),
        "seconds": round(result.seconds, 3),
    }
    # Every attempt is stamped, so an offline machine does not retry on every
    # read; "at" stays the last successful sync.
    stamp = {**last_sync(vault), "attempted_at": time.time(), "attempt": result.state}
    if result.state == "synced":
        stamp.update(at=stamp["attempted_at"], **outcome)
    write_text_atomic(_stamp(vault), json.dumps(stamp) + "\n")
    return outcome


def _nested() -> bool:
    return bool(os.environ.get(memory_sync.SYNCING_ENV) or os.environ.get(NO_SYNC_ENV))


def _applies(vault: Path, config_home: Path) -> str | None:
    """Why automation does not apply here, or None when it does."""
    if _nested():
        return "nested"
    try:
        read_marker(vault)
    except MemoryVaultError:
        return "schema"
    if settings(config_home)["mode"] != "auto":
        return "manual"
    return None


def after_write(vault: Path, config_home: Path) -> dict[str, Any]:
    reason = _applies(vault, config_home)
    if reason is not None:
        return {
            "state": reason,
            "pending": len(memory_sync.pending(vault)) if reason == "manual" else 0,
        }
    return run(vault)


def before_read(
    vault: Path, config_home: Path, *, now: float | None = None
) -> dict[str, Any] | None:
    """Refresh at most once per interval. None when nothing was attempted."""
    if _applies(vault, config_home) is not None:
        return None
    interval = settings(config_home)["fetch_interval"]
    stamp = last_sync(vault)
    last = max(stamp.get("at", 0), stamp.get("attempted_at", 0))
    if (now if now is not None else time.time()) - last < interval:
        return None
    return run(vault)


def status(vault: Path, config_home: Path) -> dict[str, Any]:
    open_conflicts = [c for c in memory_sync.conflicts(vault) if "resolved" not in c]
    return {
        **settings(config_home),
        "schema": read_marker(vault),
        "pending": len(memory_sync.pending(vault)),
        "conflicts": len(open_conflicts),
        "last_sync": last_sync(vault),
    }


# --- Conflicts --------------------------------------------------------------------


def open_conflicts(vault: Path) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in conflict.items() if key != "op_data"}
        for conflict in memory_sync.conflicts(vault)
        if "resolved" not in conflict
    ]


def _find(vault: Path, op_id: str) -> dict[str, Any]:
    for conflict in memory_sync.conflicts(vault):
        if conflict["op"] == op_id and "resolved" not in conflict:
            return conflict
    raise MemoryVaultError(f"no open conflict for operation {op_id}")


def _at_ref(vault: Path, ref: str, path: str) -> str | None:
    shown = subprocess.run(
        ["git", "-C", str(vault), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if shown.returncode != 0:
        return None
    return shown.stdout[:SHOW_LIMIT].decode("utf-8", "replace")


def _current_text(vault: Path, path: str) -> str | None:
    target = vault / path
    if not target.is_file() or target.is_symlink():
        return None
    return target.read_bytes()[:SHOW_LIMIT].decode("utf-8", "replace")


def _paths(conflict: dict[str, Any]) -> list[str]:
    data = conflict.get("op_data") or {}
    if conflict["kind"] == "multi":
        return [item["path"] for item in json.loads(data.get("content") or "[]")]
    return [conflict["path"]]


def show(vault: Path, op_id: str) -> dict[str, Any]:
    conflict = _find(vault, op_id)
    return {
        "op": op_id,
        "kind": conflict["kind"],
        "tier": conflict["tier"],
        "reason": conflict["reason"],
        "resolver": conflict["resolver"],
        "files": [
            {
                "path": path,
                "current": _current_text(vault, path),
                "mine": _at_ref(vault, conflict["recovery_ref"], path),
            }
            for path in _paths(conflict)
        ],
    }


def resolve(
    vault: Path, config_home: Path, op_id: str, keep: str, *, cwd: Path | None = None
) -> dict[str, Any]:
    """Keep theirs (accept the current state) or mine (re-apply the lost operation).

    With ``cwd`` (every CLI call), a project or registry conflict is resolved
    only from the repository it belongs to, like any other project write.
    """
    conflict = _find(vault, op_id)
    if keep not in {"mine", "theirs"}:
        raise MemoryVaultError("keep must be mine or theirs")
    if cwd is not None:
        _require_owner(vault, config_home, conflict, cwd)
    resolution = None
    if keep == "mine":
        resolution = _reapply(vault, conflict).id
    memory_sync.mark_resolved(vault, op_id, keep, resolution)
    return {
        "op": op_id,
        "kept": keep,
        "resolution_op": resolution,
        "sync": after_write(vault, config_home),
    }


def _require_owner(vault: Path, config_home: Path, conflict: dict[str, Any], cwd: Path) -> None:
    from . import memory_projects

    tier = conflict["tier"]
    if tier in {"core", "proposal"} or conflict["kind"] in {"obsidian", "untrack"}:
        return  # core is the human's (the CLI asks for the typed code)
    elsewhere = "resolve it from the repository it belongs to"
    if tier == "meta":
        entry = json.loads((conflict.get("op_data") or {}).get("content") or "{}")
        identity = memory_projects.repo_identity(cwd)
        ours = identity is not None and (
            entry.get("origin") == identity.canonical
            or memory_setup.bindings(config_home).get(str(identity.toplevel)) == entry.get("id")
        )
        if not ours:
            raise MemoryVaultError(f"this registry conflict is not this repository's; {elsewhere}")
        return
    entry = memory_projects.require_project(vault, cwd, config_home)
    if entry["folder"] != conflict.get("project"):
        raise MemoryVaultError(
            f"this conflict belongs to project {conflict.get('project')}, not {entry['folder']}; "
            f"{elsewhere}"
        )


def _reapply(vault: Path, conflict: dict[str, Any]) -> memory_sync.Op:
    """Re-issue the lost operation, with fresh expectations, from its own bytes."""
    op = memory_sync.Op(**conflict["op_data"])
    rel = (
        op.path.split("/", 2)[2]
        if op.path.startswith("projects/") and op.path.count("/") >= 2
        else ""
    )
    if op.kind == "multi":
        changes: list[tuple[str, bytes | None]] = []
        for item in json.loads(op.content or "[]"):
            if item["content"] is None:
                if (vault / item["path"]).exists():
                    changes.append((item["path"], None))
            else:
                changes.append((item["path"], base64.b64decode(item["content"])))
        if op.tier in {"core", "proposal"}:
            # A core approval (and the proposal it removes) goes back
            # through the core path, not the project one.
            return memory_sync.change_core(vault, changes, tier=op.tier, client="human")
        return memory_sync.change_project(vault, op.project or "", changes, client="resolution")
    if op.kind == "put":
        return memory_sync.put_project(
            vault, op.project or "", rel, op.data() or b"", client="resolution"
        )
    if op.kind == "delete":
        return memory_sync.delete_project(vault, op.project or "", rel, client="resolution")
    if op.kind == "forget":
        return memory_sync.forget_project(vault, op.project or "", client="resolution")
    if op.kind == "approve":
        return memory_sync.approve_core(
            vault, op.path.split("/", 1)[1], op.data() or b"", client="human"
        )
    if op.kind == "register":
        entry = json.loads(op.content or "{}")
        origin = entry["origin"]
        url = origin if origin.startswith("local/") else f"https://{origin}"
        return memory_sync.register_origin(
            vault, url, entry["folder"], entry["id"], client="resolution"
        )
    raise MemoryVaultError(f"cannot re-apply a {op.kind} operation")
