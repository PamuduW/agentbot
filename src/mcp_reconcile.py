from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .atomic_io import write_text_atomic
from .mcp_models import (
    McpCatalog,
    McpCatalogEntry,
    McpManagedRecord,
    McpPlan,
    McpPlanItem,
    McpState,
)
from .mcp_render import (
    entry_fingerprint,
    parse_mcp_config,
    remove_owned_entry,
    render_mcp_config,
)
from .mcp_state import McpStateStore
from .paths import AgentbotPaths

_CLIENTS = frozenset({"claude", "codex", "cursor"})
_OPERATION_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_MANIFEST_KEYS = frozenset({"version", "operation_id", "files"})
_FILE_KEYS = frozenset(
    {
        "path",
        "backup_name",
        "original_present",
        "original_mode",
        "original_digest",
        "post_present",
        "post_digest",
    }
)


@dataclass(frozen=True)
class McpReconcileContext:
    paths: AgentbotPaths
    catalog: McpCatalog
    state_store: McpStateStore
    fault: Callable[[str], None] | None = None


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    backup_name: str | None
    original_present: bool
    original_mode: int | None
    original_digest: str | None
    post_present: bool | None = None
    post_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "backup_name": self.backup_name,
            "original_present": self.original_present,
            "original_mode": self.original_mode,
            "original_digest": self.original_digest,
            "post_present": self.post_present,
            "post_digest": self.post_digest,
        }


def plan_mcp(
    context: McpReconcileContext,
    selection: Sequence[str],
    targets: Sequence[str],
) -> McpPlan:
    return _plan(context, "setup", selection, targets)


def plan_mcp_off(
    context: McpReconcileContext,
    selection: Sequence[str],
    targets: Sequence[str],
) -> McpPlan:
    return _plan(context, "off", selection, targets)


def apply_mcp_plan(
    context: McpReconcileContext,
    plan: McpPlan,
    *,
    operation_id: str | None = None,
    now: str | None = None,
) -> str:
    if not plan.can_apply:
        raise ValueError("MCP plan has conflicts and cannot be applied")
    current = _plan(context, plan.action, plan.selection, plan.targets)
    if current != plan:
        raise ValueError("MCP configuration changed after planning")
    operation_id = operation_id or _new_operation_id()
    _validate_operation_id(operation_id)
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    destinations = tuple(_destination(context.paths, client) for client in plan.targets)
    affected = (*destinations, context.paths.mcp_state_file)
    backup_dir, snapshots = _create_backup(context.paths, operation_id, affected)
    try:
        entries = _selected_entries(context.catalog, plan.selection)
        if plan.action == "setup":
            _apply_setup(context, plan, entries, operation_id, now)
        else:
            _apply_off(context, plan)
        completed = tuple(_with_post_digest(snapshot) for snapshot in snapshots)
        _write_manifest(backup_dir, operation_id, completed)
        _fsync_directory(backup_dir)
        _inject(context, "directory-fsync")
    except BaseException:
        _restore_snapshots(backup_dir, snapshots)
        raise
    return operation_id


def restore_mcp_operation(context: McpReconcileContext, operation_id: str) -> None:
    _validate_operation_id(operation_id)
    backup_dir = context.paths.mcp_backup_home / operation_id
    snapshots = _load_manifest(backup_dir, operation_id)
    allowed = {_destination(context.paths, client) for client in ("claude", "codex", "cursor")}
    allowed.add(context.paths.mcp_state_file)
    if any(snapshot.path not in allowed for snapshot in snapshots):
        raise ValueError("MCP backup contains a destination outside managed destinations")
    for snapshot in snapshots:
        present = snapshot.path.exists()
        if snapshot.path.is_symlink() or (present and not snapshot.path.is_file()):
            raise ValueError(f"restore destination is unsafe: {snapshot.path}")
        if snapshot.post_present is None:
            raise ValueError(f"operation did not complete successfully: {operation_id}")
        digest = _digest_path(snapshot.path) if present else None
        if present != snapshot.post_present or digest != snapshot.post_digest:
            raise ValueError(f"destination changed since operation: {snapshot.path}")
    _restore_snapshots(backup_dir, snapshots)


def _plan(
    context: McpReconcileContext,
    action: Literal["setup", "off"],
    selection: Sequence[str],
    targets: Sequence[str],
) -> McpPlan:
    selected = _normalize_selection(selection)
    clients = _normalize_targets(targets)
    entries = _selected_entries(context.catalog, selected)
    state = context.state_store.load()
    records = {record.key: record for record in state.managed}
    items = []
    for client in clients:
        destination = _destination(context.paths, client)
        text = _read_config(destination)
        document = parse_mcp_config(client, text)
        for entry in entries:
            native = _native_from_document(client, document, entry.name)
            current_fingerprint = entry_fingerprint(native) if native is not None else None
            record = records.get((entry.id, client, entry.name))
            item_state = _classify(destination, native is not None, current_fingerprint, record)
            items.append(
                McpPlanItem(
                    action=action,
                    catalog_id=entry.id,
                    client=client,
                    destination=str(destination),
                    name=entry.name,
                    state=item_state,
                )
            )
    return McpPlan(action=action, selection=selected, targets=clients, items=tuple(items))


def _classify(
    destination: Path,
    present: bool,
    current_fingerprint: str | None,
    record: McpManagedRecord | None,
) -> Literal["absent", "owned", "unmanaged-conflict", "managed-drift"]:
    if record is None:
        return "unmanaged-conflict" if present else "absent"
    if record.destination != str(destination) or not present:
        return "managed-drift"
    if current_fingerprint != record.rendered_fingerprint:
        return "managed-drift"
    return "owned"


def _apply_setup(
    context: McpReconcileContext,
    plan: McpPlan,
    entries: tuple[McpCatalogEntry, ...],
    operation_id: str,
    now: str,
) -> None:
    rendered_fingerprints: dict[tuple[str, str], str] = {}
    for client in plan.targets:
        destination = _destination(context.paths, client)
        rendered = render_mcp_config(client, _read_config(destination), entries)
        _inject(context, f"write:{client}")
        write_text_atomic(destination, rendered)
        document = parse_mcp_config(client, destination.read_text(encoding="utf-8"))
        _inject(context, f"post-parse:{client}")
        for entry in entries:
            native = _native_from_document(client, document, entry.name)
            if native is None:
                raise ValueError(f"rendered MCP entry is missing: {client}/{entry.name}")
            rendered_fingerprints[(client, entry.id)] = entry_fingerprint(native)

    selected_keys = {(entry.id, client, entry.name) for client in plan.targets for entry in entries}
    retained = tuple(
        record for record in context.state_store.load().managed if record.key not in selected_keys
    )
    added = tuple(
        McpManagedRecord(
            catalog_id=entry.id,
            client=client,
            scope="user",
            destination=str(_destination(context.paths, client)),
            name=entry.name,
            rendered_fingerprint=rendered_fingerprints[(client, entry.id)],
            catalog_version=context.catalog.version,
            reviewed_on=entry.reviewed_on,
            operation_id=operation_id,
            updated_at=now,
        )
        for client in plan.targets
        for entry in entries
    )
    _inject(context, "state-write")
    context.state_store.replace(McpState(version=1, managed=(*retained, *added)))


def _apply_off(context: McpReconcileContext, plan: McpPlan) -> None:
    entries = _selected_entries(context.catalog, plan.selection)
    for client in plan.targets:
        destination = _destination(context.paths, client)
        rendered = _read_config(destination)
        for entry in entries:
            rendered = remove_owned_entry(client, rendered, entry.name)
        _inject(context, f"write:{client}")
        write_text_atomic(destination, rendered)
        document = parse_mcp_config(client, destination.read_text(encoding="utf-8"))
        _inject(context, f"post-parse:{client}")
        for entry in entries:
            if _native_from_document(client, document, entry.name) is not None:
                raise ValueError(f"removed MCP entry remains: {client}/{entry.name}")
    removed_keys = {(entry.id, client, entry.name) for client in plan.targets for entry in entries}
    retained = tuple(
        record for record in context.state_store.load().managed if record.key not in removed_keys
    )
    _inject(context, "state-write")
    context.state_store.replace(McpState(version=1, managed=retained))


def _normalize_selection(selection: Sequence[str]) -> tuple[str, ...]:
    if not selection:
        raise ValueError("select at least one MCP server")
    if any(not isinstance(value, str) or not value for value in selection):
        raise ValueError("MCP selection contains an invalid id")
    if len(set(selection)) != len(selection):
        raise ValueError("MCP selection contains a duplicate id")
    return tuple(sorted(selection))


def _normalize_targets(targets: Sequence[str]) -> tuple[str, ...]:
    if not targets:
        raise ValueError("select at least one MCP client")
    if any(target not in _CLIENTS for target in targets):
        invalid = next(target for target in targets if target not in _CLIENTS)
        raise ValueError(f"unsupported MCP client: {invalid}")
    if len(set(targets)) != len(targets):
        raise ValueError("MCP targets contain a duplicate client")
    return tuple(sorted(targets))


def _selected_entries(catalog: McpCatalog, selection: Sequence[str]) -> tuple[McpCatalogEntry, ...]:
    by_id = {entry.id: entry for entry in catalog.entries}
    selected = []
    for entry_id in selection:
        entry = by_id.get(entry_id)
        if entry is None:
            raise ValueError(f"unknown MCP catalog id: {entry_id}")
        if not entry.eligible:
            raise ValueError(f"MCP catalog entry is not eligible: {entry_id}")
        selected.append(entry)
    return tuple(sorted(selected, key=lambda entry: entry.id))


def _destination(paths: AgentbotPaths, client: str) -> Path:
    if client == "claude":
        return paths.claude_home.parent / ".claude.json"
    if client == "codex":
        return paths.codex_home / "config.toml"
    if client == "cursor":
        return paths.cursor_home / "mcp.json"
    raise ValueError(f"unsupported MCP client: {client}")


def _read_config(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"MCP configuration must not be a symlink: {path}")
    if not path.exists():
        return ""
    if not path.is_file():
        raise ValueError(f"MCP configuration is not a regular file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read MCP configuration: {path}: {error}") from error


def _native_from_document(client: str, document: Any, name: str) -> object | None:
    key = "mcp_servers" if client == "codex" else "mcpServers"
    servers = document.get(key)
    return None if servers is None else servers.get(name)


def _create_backup(
    paths: AgentbotPaths, operation_id: str, affected: Sequence[Path]
) -> tuple[Path, tuple[_Snapshot, ...]]:
    paths.config_home.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.config_home, 0o700)
    paths.mcp_backup_home.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.mcp_backup_home, 0o700)
    backup_dir = paths.mcp_backup_home / operation_id
    if backup_dir.exists() or backup_dir.is_symlink():
        raise ValueError(f"MCP backup operation already exists: {operation_id}")
    backup_dir.mkdir(mode=0o700)
    snapshots = []
    for index, path in enumerate(affected):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"MCP backup source is unsafe: {path}")
        if path.exists():
            content = path.read_bytes()
            backup_name = f"{index:02d}.bin"
            _write_private(backup_dir / backup_name, content)
            snapshots.append(
                _Snapshot(
                    path=path,
                    backup_name=backup_name,
                    original_present=True,
                    original_mode=stat.S_IMODE(path.stat().st_mode),
                    original_digest=_digest_bytes(content),
                )
            )
        else:
            snapshots.append(
                _Snapshot(
                    path=path,
                    backup_name=None,
                    original_present=False,
                    original_mode=None,
                    original_digest=None,
                )
            )
    normalized = tuple(snapshots)
    _write_manifest(backup_dir, operation_id, normalized)
    return backup_dir, normalized


def _write_manifest(backup_dir: Path, operation_id: str, snapshots: Sequence[_Snapshot]) -> None:
    encoded = (
        json.dumps(
            {
                "version": 1,
                "operation_id": operation_id,
                "files": [snapshot.to_dict() for snapshot in snapshots],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _write_private(backup_dir / "manifest.json", encoded)


def _load_manifest(backup_dir: Path, operation_id: str) -> tuple[_Snapshot, ...]:
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise ValueError(f"MCP backup operation does not exist: {operation_id}")
    if stat.S_IMODE(backup_dir.stat().st_mode) != 0o700:
        raise ValueError(f"MCP backup directory must have mode 700: {backup_dir}")
    manifest = backup_dir / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError(f"MCP backup manifest is unsafe: {manifest}")
    if stat.S_IMODE(manifest.stat().st_mode) != 0o600:
        raise ValueError(f"MCP backup manifest must have mode 600: {manifest}")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid MCP backup manifest: {error}") from error
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_KEYS:
        raise ValueError("invalid MCP backup manifest keys")
    if raw["version"] != 1 or raw["operation_id"] != operation_id:
        raise ValueError("invalid MCP backup manifest identity")
    files = raw["files"]
    if not isinstance(files, list):
        raise ValueError("invalid MCP backup file list")
    snapshots = tuple(_decode_snapshot(value) for value in files)
    if len({snapshot.path for snapshot in snapshots}) != len(snapshots):
        raise ValueError("duplicate MCP backup destination")
    return snapshots


def _decode_snapshot(raw: object) -> _Snapshot:
    if not isinstance(raw, dict) or set(raw) != _FILE_KEYS:
        raise ValueError("invalid MCP backup file record")
    path_value = raw["path"]
    if not isinstance(path_value, str):
        raise ValueError("invalid MCP backup destination")
    path = Path(path_value)
    if not path.is_absolute() or str(path.resolve(strict=False)) != path_value:
        raise ValueError("MCP backup destination must be absolute and canonical")
    backup_name = raw["backup_name"]
    if backup_name is not None and (
        not isinstance(backup_name, str) or not re.fullmatch(r"[0-9]{2}\.bin", backup_name)
    ):
        raise ValueError("invalid MCP backup filename")
    original_present = raw["original_present"]
    post_present = raw["post_present"]
    if not isinstance(original_present, bool) or not (
        post_present is None or isinstance(post_present, bool)
    ):
        raise ValueError("invalid MCP backup presence flag")
    original_mode = raw["original_mode"]
    if original_mode is not None and (
        isinstance(original_mode, bool) or not isinstance(original_mode, int)
    ):
        raise ValueError("invalid MCP backup mode")
    for key in ("original_digest", "post_digest"):
        value = raw[key]
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
        ):
            raise ValueError(f"invalid MCP backup {key}")
    return _Snapshot(
        path=path,
        backup_name=backup_name,
        original_present=original_present,
        original_mode=original_mode,
        original_digest=raw["original_digest"],
        post_present=post_present,
        post_digest=raw["post_digest"],
    )


def _restore_snapshots(backup_dir: Path, snapshots: Sequence[_Snapshot]) -> None:
    for snapshot in snapshots:
        if snapshot.original_present:
            if snapshot.backup_name is None or snapshot.original_mode is None:
                raise ValueError("MCP backup snapshot is incomplete")
            source = backup_dir / snapshot.backup_name
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"MCP backup file is unsafe: {source}")
            if stat.S_IMODE(source.stat().st_mode) != 0o600:
                raise ValueError(f"MCP backup file must have mode 600: {source}")
            content = source.read_bytes()
            if _digest_bytes(content) != snapshot.original_digest:
                raise ValueError(f"MCP backup digest mismatch: {source}")
            _write_bytes_atomic(snapshot.path, content, snapshot.original_mode)
        else:
            if snapshot.path.is_symlink():
                raise ValueError(f"restore destination is unsafe: {snapshot.path}")
            if snapshot.path.exists():
                if not snapshot.path.is_file():
                    raise ValueError(f"restore destination is unsafe: {snapshot.path}")
                snapshot.path.unlink()
                _fsync_directory(snapshot.path.parent)


def _write_private(path: Path, content: bytes) -> None:
    _write_bytes_atomic(path, content, 0o600)


def _write_bytes_atomic(path: Path, content: bytes, mode: int) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"atomic destination is unsafe: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.agentbot-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _with_post_digest(snapshot: _Snapshot) -> _Snapshot:
    present = snapshot.path.exists()
    return replace(
        snapshot,
        post_present=present,
        post_digest=_digest_path(snapshot.path) if present else None,
    )


def _digest_path(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_operation_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not _OPERATION_PATTERN.fullmatch(operation_id):
        raise ValueError(f"invalid MCP operation id: {operation_id}")


def _inject(context: McpReconcileContext, stage: str) -> None:
    if context.fault is not None:
        context.fault(stage)
