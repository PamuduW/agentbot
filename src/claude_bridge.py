from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import default_paths

if TYPE_CHECKING:
    from .paths import AgentbotPaths


@dataclass(frozen=True)
class BridgeAction:
    skill_name: str
    target: Path
    action: str


@dataclass(frozen=True)
class BridgeResult:
    actions: list[BridgeAction]
    linked: int
    updated: int
    skipped: int


def default_agents_skills_home() -> Path:
    return Path.home() / ".agents" / "skills"


def default_claude_skills_home() -> Path:
    return default_paths().claude_skills_home


def bridge_claude_skills(
    agents_home: Path | None = None,
    claude_home: Path | None = None,
    *,
    dry_run: bool = False,
) -> BridgeResult:
    agents_dir = (agents_home or default_agents_skills_home()).expanduser()
    claude_dir = (claude_home or default_claude_skills_home()).expanduser()

    actions: list[BridgeAction] = []
    linked = 0
    updated = 0
    skipped = 0

    if not agents_dir.is_dir():
        return BridgeResult(actions=actions, linked=linked, updated=updated, skipped=skipped)

    if not dry_run:
        claude_dir.mkdir(parents=True, exist_ok=True)

    for source in sorted(
        path for path in agents_dir.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    ):
        target = claude_dir / source.name
        action = _bridge_entry(source, target, dry_run=dry_run)
        actions.append(action)
        if action.action == "linked":
            linked += 1
        elif action.action == "updated":
            updated += 1
        elif action.action == "skip_existing":
            skipped += 1

    return BridgeResult(actions=actions, linked=linked, updated=updated, skipped=skipped)


def _bridge_entry(source: Path, target: Path, *, dry_run: bool) -> BridgeAction:
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return BridgeAction(skill_name=source.name, target=target, action="already_linked")
        if not target.exists():
            if not dry_run:
                target.unlink()
                os.symlink(source, target)
            return BridgeAction(skill_name=source.name, target=target, action="updated")
        return BridgeAction(skill_name=source.name, target=target, action="skip_existing")

    if target.exists():
        return BridgeAction(skill_name=source.name, target=target, action="skip_existing")

    if not dry_run:
        os.symlink(source, target)
    return BridgeAction(skill_name=source.name, target=target, action="linked")


def run_claude_bridge(paths: AgentbotPaths, *, dry_run: bool = False) -> BridgeResult:
    return bridge_claude_skills(
        agents_home=paths.agents_skills_home,
        claude_home=paths.claude_skills_home,
        dry_run=dry_run,
    )
