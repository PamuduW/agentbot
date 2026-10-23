"""Interactive enrollment inside boot; subsequent updates use saved ownership."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import memory, memory_projects, memory_session_config, workspace_memory
from .paths import AgentbotPaths
from .workspace_state import WorkspaceStore


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Preview boot without any writes")
    parser.add_argument("--yes", action="store_true", help="Confirm explicitly selected memory integrations")
    parser.add_argument("--memory-clients", type=parse_clients,
                        help="Memory clients for scripted boot, comma-separated; or none")


def parse_clients(value: str) -> tuple[str, ...]:
    if value.strip().lower() == "none":
        return ()
    names = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not names or any(name not in memory_session_config.CLIENT_FILES for name in names):
        raise ValueError("choose supported memory clients or none")
    return names


def enrolled(paths: AgentbotPaths, checkout: Path) -> bool:
    return any(item.path == str(checkout.resolve()) and item.memory_bindings
               for item in WorkspaceStore(paths.workspace_state_file).load())


def setup(
    paths: AgentbotPaths, checkout: Path, *, clients: tuple[str, ...] | None,
    confirmed: bool, dry_run: bool, interactive: bool,
) -> tuple[tuple[str, str, str], int]:
    label = "Memory integrations"
    try:
        store = WorkspaceStore(paths.workspace_state_file)
        record = next((item for item in store.load() if item.path == str(checkout.resolve())), None)
        if record is None and not dry_run:
            return (label, "workspace registration unavailable", "skipped"), 0
        previous = record.memory_bindings if record else {}
        discovered = workspace_memory.discover_clients()
        selected = clients
        if selected is None:
            selected = tuple(previous) or discovered
            if interactive and not dry_run:
                print("\n  Memory clients detected: " + (", ".join(discovered) or "none"))
                print("  Saved clients remain managed; select additional clients or none.")
                answer = input("  Memory clients [" + (",".join(selected) or "none") + "]: ").strip()
                if answer:
                    selected = parse_clients(answer)
        if not selected and not previous:
            return (label, "no clients selected; CLI memory remains available", "skipped"), 0
        vault, _ = memory.find_vault(paths.root)
        if vault is None:
            return (label, "no memory vault configured", "skipped"), 0
        print("\n  Memory integrations (preview; native trust is unchanged):")
        print(f"    Workspace: {checkout.resolve()}")
        print(f"    Vault: {vault.resolve()}")
        print(f"    Agentbot: {paths.root.resolve() / 'bin/agentbot'}")
        for client in dict.fromkeys((*previous, *selected)):
            for surface, relative in memory_session_config.CLIENT_FILES[client].items():
                print(f"    {client} {surface}: {relative}")
        if dry_run and memory_projects.resolve(vault, checkout, paths.config_home).state != "resolved":
            return (label, "preview only; project registration is required on apply", "preview"), 0
        plan = workspace_memory.prepare(vault, checkout, paths, clients=selected, previous=previous)
        changed = (previous != plan.bindings or plan.exclude_before != plan.exclude_after
                   or any(not item.same for item in plan.configs))
        if dry_run:
            return (label, "preview only; nothing applied", "preview"), 0
        if not changed:
            return (label, ", ".join(plan.bindings) + "; current", "ok"), 0
        print("    Local Git exclusions cover generated files and backups.")
        if interactive and not confirmed:
            confirmed = input("  Apply these memory integrations? [y/N]: ").strip().lower() == "y"
        elif clients is None:
            confirmed = False
        if not confirmed:
            return (label, "not applied; rerun boot interactively or select clients with --yes", "check"), 1
        assert record is not None
        workspace_memory.apply(plan, store, record)
        return (label, ", ".join(plan.bindings) + "; configured; approve native trust in each client", "applied"), 0
    except (OSError, ValueError, memory.MemoryVaultError) as error:
        return (label, str(error), "conflict"), 1
