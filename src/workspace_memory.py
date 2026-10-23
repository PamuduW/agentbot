"""Dynamic client discovery and owned, project-local memory integration updates."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import memory, memory_session_config
from .atomic_io import write_text_atomic
from .paths import AgentbotPaths
from .workspace_render import RenderAction
from .workspace_state import WorkspaceRecord, WorkspaceStore


def discover_clients() -> tuple[str, ...]:
    return tuple(client for client, adapter in memory_session_config.CLIENT_ADAPTERS.items()
                 if any(shutil.which(name) for name in adapter.executables))


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                          text=True, timeout=15, check=False)


def _check_untracked(config: memory_session_config.SessionConfigPlan) -> None:
    backup = config.path.with_name(config.path.name + ".agentbot-backup")
    tracked = _git(config.checkout, "ls-files", "--", config.path.relative_to(config.checkout).as_posix(),
                   backup.relative_to(config.checkout).as_posix())
    if tracked.returncode or tracked.stdout.strip():
        raise memory.MemoryVaultError("memory configuration or backup is tracked; review it manually")


def exclude_path(checkout: Path) -> Path:
    result = _git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if result.returncode or not result.stdout.strip():
        raise memory.MemoryVaultError("cannot resolve local Git exclusions")
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        raise memory.MemoryVaultError("local Git exclusion path must be absolute")
    common = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise memory.MemoryVaultError("cannot resolve common Git directory")
    anchor = Path(common.stdout.strip()).resolve()
    if not path.is_relative_to(anchor):
        raise memory.MemoryVaultError("local Git exclusion path is outside the Git directory")
    memory_session_config._read(path, anchor)
    return path


@dataclass(frozen=True)
class MemoryWorkspacePlan:
    configs: tuple[memory_session_config.SessionConfigPlan, ...]
    bindings: dict[str, dict[str, dict[str, Any]]]
    exclude: Path
    exclude_before: str
    exclude_after: str


def prepare(
    vault: Path, checkout: Path, paths: AgentbotPaths, *, clients: tuple[str, ...],
    previous: dict[str, dict[str, dict[str, Any]]],
) -> MemoryWorkspacePlan:
    configs = []
    bindings = {}
    checkout = checkout.resolve()
    for client in dict.fromkeys((*previous, *clients)):
        if client not in memory_session_config.CLIENT_FILES:
            raise memory.MemoryVaultError("unsupported memory client selection")
        surfaces = {}
        for surface in memory_session_config.CLIENT_FILES[client]:
            plan = memory_session_config.prepare(
                vault, checkout, paths, client=client, surface=surface,
                previous=previous.get(client, {}).get(surface),
            )
            _check_untracked(plan)
            configs.append(plan)
            surfaces[surface] = plan.entries
        bindings[client] = surfaces
    excluded = exclude_path(checkout)
    before = memory_session_config._read(excluded, excluded.parent)
    lines = before.splitlines()
    for plan in configs:
        for path in (plan.path, plan.path.with_name(plan.path.name + ".agentbot-backup")):
            pattern = "/" + path.relative_to(checkout).as_posix()
            if pattern not in lines:
                lines.append(pattern)
    after = "\n".join(lines) + "\n" if configs else before
    return MemoryWorkspacePlan(tuple(configs), bindings, excluded, before, after)


def affected_paths(plan: MemoryWorkspacePlan, store: WorkspaceStore) -> tuple[Path, ...]:
    result = [store.state_file, plan.exclude]
    for config in plan.configs:
        result.extend((config.path, config.path.with_name(config.path.name + ".agentbot-backup")))
    return tuple(dict.fromkeys(result))


def managed_paths(checkout: Path, clients: tuple[str, ...]) -> tuple[Path, ...]:
    paths = [exclude_path(checkout)]
    for client in clients:
        for relative in memory_session_config.CLIENT_FILES[client].values():
            path = checkout / relative
            paths.extend((path, path.with_name(path.name + ".agentbot-backup")))
    return tuple(paths)


def actions(plan: MemoryWorkspacePlan) -> tuple[RenderAction, ...]:
    return tuple(RenderAction(
        config.path.relative_to(config.checkout).as_posix(),
        "unchanged" if config.same else ("update" if config.before else "create"),
        None, f"{config.client} memory {config.surface}; native trust remains operator-owned",
    ) for config in plan.configs)


def apply(plan: MemoryWorkspacePlan, store: WorkspaceStore, record: WorkspaceRecord) -> None:
    for config in plan.configs:
        _check_untracked(config)
        if memory_session_config._read(config.path, config.checkout) != config.before:
            raise memory.MemoryVaultError("memory configuration changed after preview")
        memory_session_config._read(
            config.path.with_name(config.path.name + ".agentbot-backup"), config.checkout,
        )
    if (plan.configs and exclude_path(plan.configs[0].checkout) != plan.exclude) or (
        memory_session_config._read(plan.exclude, plan.exclude.parent) != plan.exclude_before
    ):
        raise memory.MemoryVaultError("local Git exclusions changed after preview")
    current = next((item for item in store.load() if item.path == record.path), None)
    if current != record:
        raise memory.MemoryVaultError("workspace registration changed after preview")
    snapshots = {}
    for path in affected_paths(plan, store):
        snapshots[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
    expected = {plan.exclude: plan.exclude_after.encode()}
    for config in plan.configs:
        if not config.same:
            expected[config.path] = config.after.encode()
            if snapshots[config.path] is not None:
                expected[config.path.with_name(config.path.name + ".agentbot-backup")] = config.before.encode()
    state_snapshot = snapshots[store.state_file]
    if state_snapshot is not None:
        desired_state = json.loads(state_snapshot[0])
        desired_state["workspaces"] = [
            replace(record, memory_bindings=plan.bindings).to_dict() if item["path"] == record.path else item
            for item in desired_state["workspaces"]
        ]
        expected[store.state_file] = (json.dumps(desired_state, indent=2, sort_keys=True) + "\n").encode()
    try:
        for config in plan.configs:
            memory_session_config.apply_prepared(config)
        if plan.exclude_before != plan.exclude_after:
            write_text_atomic(plan.exclude, plan.exclude_after)
        if record.memory_bindings != plan.bindings:
            store.upsert(replace(record, memory_bindings=plan.bindings))
    except (OSError, ValueError, memory.MemoryVaultError):
        for path, snapshot in reversed(tuple(snapshots.items())):
            if (path not in expected or path.is_symlink() or not path.is_file()
                    or path.read_bytes() != expected[path]):
                continue
            if snapshot is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != snapshot[0]:
                write_text_atomic(path, snapshot[0].decode())
                os.chmod(path, snapshot[1])
        raise
