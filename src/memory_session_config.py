"""Opt-in, project-bound hook/MCP configuration, one atomic file per invocation."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from . import memory, memory_client_hooks, memory_projects
from .atomic_io import write_text_atomic
from .mcp_render import parse_mcp_config
from .paths import AgentbotPaths


@dataclass(frozen=True)
class ClientAdapter:
    executables: tuple[str, ...]
    files: dict[str, str]


CLIENT_ADAPTERS = {
    "claude": ClientAdapter(("claude",), {"hooks": ".claude/settings.local.json", "mcp": ".mcp.json"}),
    "codex": ClientAdapter(("codex",), {"hooks": ".codex/hooks.json", "mcp": ".codex/config.toml"}),
    # `cursor` can be an editor launcher rather than the agent CLI.
    "cursor": ClientAdapter(("cursor-agent",), {"hooks": ".cursor/hooks.json", "mcp": ".cursor/mcp.json"}),
    "antigravity": ClientAdapter(("agy",), {"hooks": ".agents/hooks.json", "mcp": ".agents/mcp_config.json"}),
}
CLIENT_FILES = {client: adapter.files for client, adapter in CLIENT_ADAPTERS.items()}


@dataclass(frozen=True)
class SessionConfigPlan:
    checkout: Path
    path: Path
    client: str
    surface: str
    before: str
    after: str
    entries: dict[str, Any]
    same: bool


def _remove_owned_hooks(
    document: dict[str, Any], client: str, previous: dict[str, Any],
) -> None:
    if client == "antigravity":
        if document.get("agentbot-memory") != previous:
            raise memory.MemoryVaultError("owned memory hooks were edited; review manually")
        document.pop("agentbot-memory")
        return
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        return
    for event, groups in previous.items():
        existing = hooks.get(event)
        if not isinstance(existing, list) or any(group not in existing for group in groups):
            raise memory.MemoryVaultError("owned memory hooks were edited; review manually")
        hooks[event] = [item for item in existing if item not in groups]


def _read(path: Path, checkout: Path) -> str:
    for candidate in (path, *path.parents):
        if candidate == checkout:
            break
        if candidate.is_symlink():
            raise memory.MemoryVaultError("memory configuration paths cannot be symlinks")
    if not path.exists():
        return ""
    if not path.is_file() or path.stat().st_size > 1_048_576:
        raise memory.MemoryVaultError("memory configuration needs a bounded regular file")
    return path.read_text(encoding="utf-8")


def _memory_command(value: Any) -> bool:
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and "memory session hook" in command:
            return True
        return any(_memory_command(item) for item in value.values())
    return isinstance(value, list) and any(_memory_command(item) for item in value)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _merge_hooks(
    text: str, client: str, command: list[str], previous: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    document = json.loads(text, object_pairs_hook=_json_object) if text.strip() else {}
    if not isinstance(document, dict):
        raise memory.MemoryVaultError("memory hook configuration needs an object")
    if previous and text.strip():
        _remove_owned_hooks(document, client, previous)
    hooks = document.setdefault("hooks", {}) if client != "antigravity" else document
    if not isinstance(hooks, dict):
        raise memory.MemoryVaultError("memory hooks must be an object")
    desired = {}
    for event in (memory_client_hooks.START_EVENTS[client],
                  memory_client_hooks.CHECKPOINT_EVENTS[client]):
        handler: dict[str, Any] = {
            "command": shlex.join([*command, "--event", event]), "timeout": 10,
        }
        if client in {"claude", "codex", "antigravity"}:
            handler["type"] = "command"
        if client in {"claude", "codex"}:
            handler = {"matcher": "startup|resume|clear|compact|fork" if event == "SessionStart"
                       else "*", "hooks": [handler]}
        desired[event] = [handler]
    if client == "antigravity":
        existing = hooks.get("agentbot-memory")
        others = {key: value for key, value in hooks.items() if key != "agentbot-memory"}
        if _memory_command(others):
            raise memory.MemoryVaultError("existing memory hook differs; review manually")
        if "agentbot-memory" in hooks and existing != desired:
            raise memory.MemoryVaultError("existing Agentbot memory hooks differ; review manually")
        hooks["agentbot-memory"] = desired
    else:
        for event, groups in desired.items():
            existing = hooks.setdefault(event, [])
            if not isinstance(existing, list):
                raise memory.MemoryVaultError("memory hook event must contain a list")
            other_groups = [item for item in existing if item != groups[0]]
            if _memory_command(other_groups) or existing.count(groups[0]) > 1:
                raise memory.MemoryVaultError("existing memory hook differs; review manually")
            if groups[0] not in existing:
                existing.append(groups[0])
        if client == "cursor":
            if document.get("version", 1) != 1:
                raise memory.MemoryVaultError("unsupported Cursor hook version")
            document["version"] = 1
    return json.dumps(document, indent=2) + "\n", desired


def prepare(
    vault: Path, checkout: Path, paths: AgentbotPaths, *, client: str,
    surface: str, previous: dict[str, Any] | None = None,
) -> SessionConfigPlan:
    if client not in CLIENT_FILES or surface not in {"hooks", "mcp"}:
        raise memory.MemoryVaultError("unsupported memory configuration surface")
    checkout = checkout.resolve()
    identity = memory_projects.repo_identity(checkout)
    if identity is None or identity.toplevel.resolve() != checkout:
        raise memory.MemoryVaultError("memory configuration requires the repository root")
    memory_projects.require_project(vault, checkout, paths.config_home)
    if paths.config_home.name != "agentbot":
        raise memory.MemoryVaultError("memory configuration requires the standard Agentbot config directory")
    executable = paths.root.resolve() / "bin/agentbot"
    if not executable.is_file():
        raise memory.MemoryVaultError("Agentbot launcher is unavailable")
    environment = {
        "AGENTBOT_HOME": str(paths.root.resolve()),
        "AGENTBOT_MEMORY_DIR": str(vault.resolve()),
        "XDG_CONFIG_HOME": str(paths.config_home.parent.resolve()),
    }
    path = checkout / CLIENT_FILES[client][surface]
    before = _read(path, checkout)
    _read(path.with_name(path.name + ".agentbot-backup"), checkout)
    try:
        if surface == "hooks":
            command = ["env", *[f"{key}={value}" for key, value in environment.items()],
                       str(executable), "memory", "session", "hook", "--client", client,
                       "--project-root", str(checkout)]
            after, entries = _merge_hooks(before, client, command, previous)
            same = bool(before.strip()) and json.loads(before) == json.loads(after)
        else:
            if client != "codex" and before.strip():
                json.loads(before, object_pairs_hook=_json_object)
            document = parse_mcp_config(client, before)
            key = "mcp_servers" if client == "codex" else "mcpServers"
            if previous and document.get(key, {}).get("agentbot_memory") == previous.get("agentbot_memory"):
                document[key].pop("agentbot_memory", None)
            server = {"command": str(executable), "args": ["memory", "serve", "--project-root",
                      str(checkout), "--client", client], "env": environment}
            existing = document.get(key, {}).get("agentbot_memory")
            if "agentbot_memory" in document.get(key, {}) and existing != server:
                raise memory.MemoryVaultError("existing Agentbot memory server differs; review manually")
            document.setdefault(key, {})["agentbot_memory"] = server
            after = (tomlkit.dumps(document) if client == "codex"
                     else json.dumps(document, indent=2) + "\n")
            same = (parse_mcp_config(client, before).get(key, {}).get("agentbot_memory") == server)
            entries = {"agentbot_memory": server}
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise memory.MemoryVaultError("memory configuration is invalid; inspect its format privately") from None
    return SessionConfigPlan(checkout, path, client, surface, before, after, entries, same)


def apply_prepared(plan: SessionConfigPlan) -> None:
    if not plan.same:
        # A changed file requires a new preview; never overwrite a concurrent edit.
        if _read(plan.path, plan.checkout) != plan.before:
            raise memory.MemoryVaultError("memory configuration changed during preparation")
        _read(plan.path.with_name(plan.path.name + ".agentbot-backup"), plan.checkout)
        write_text_atomic(plan.path, plan.after, backup=True)


def configure(
    vault: Path, checkout: Path, paths: AgentbotPaths, *, client: str,
    surface: str, apply: bool = False,
) -> dict[str, Any]:
    plan = prepare(vault, checkout, paths, client=client, surface=surface)
    if apply:
        apply_prepared(plan)
    return {
        "state": "unchanged" if plan.same else ("applied" if apply else "preview"),
        "client": client, "surface": surface, "path": str(plan.path),
        "managed_entries": plan.entries,
        "native_trust": "operator-required", "model_use_verified": False,
        "native_memory_control": "unchanged; use verified CLI settings separately",
    }
