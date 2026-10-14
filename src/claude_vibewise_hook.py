"""Install VibeWise's Claude Code SessionStart hook while its source is enabled.

VibeWise ships the hook only in its Claude plugin; Agentbot installs skills, not
plugins. So Agentbot carries an adapted copy (`global/claude/hooks/`, provenance
in its header), installs it to ~/.claude/hooks/, and keeps exactly one
SessionStart entry in ~/.claude/settings.json pointing at it. Every other hook,
Boost's included, is left as it is. When the `vibe-wise` source is disabled or
removed, the entry and the managed script are removed again.

The hook is silent in a project without an active .vibe-wise/profile.md, writes
nothing and makes no network call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import write_text_atomic
from .models import DoctorIssue
from .paths import AgentbotPaths
from .skills_sources import SkillsSourcesError, load_skills_sources

SOURCE_ID = "vibe-wise"
SCRIPT_NAME = "vibe-wise-session-start.py"
MANAGED_MARKER = "# Managed by Agentbot."
COMMAND = f"python3 ~/.claude/hooks/{SCRIPT_NAME}"
MATCHER = "startup|resume|clear|compact|fork"
TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class VibewiseHookState:
    wanted: bool
    script_in_sync: bool
    settings_wired: bool

    @property
    def status_label(self) -> str:
        if not self.wanted:
            return "not wanted" if not self.settings_wired else "left over"
        if not self.script_in_sync:
            return "script missing or stale"
        if not self.settings_wired:
            return "not wired"
        return "ok"


def hook_source(paths: AgentbotPaths) -> Path:
    return paths.root / "global" / "claude" / "hooks" / SCRIPT_NAME


def hook_destination(paths: AgentbotPaths) -> Path:
    return paths.claude_home / "hooks" / SCRIPT_NAME


def vibewise_wanted(paths: AgentbotPaths) -> bool:
    try:
        config = load_skills_sources(paths.skills_sources_file)
    except SkillsSourcesError:
        return False
    return any(source.id == SOURCE_ID for source in config.active_sources())


def _desired_entry() -> dict:
    return {
        "matcher": MATCHER,
        "hooks": [{"type": "command", "command": COMMAND, "timeout": TIMEOUT_SECONDS}],
    }


def _is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(hook, dict) and hook.get("command") == COMMAND for hook in hooks
    )


def _read_settings(settings_path: Path) -> dict:
    if settings_path.is_symlink():
        raise ValueError(f"claude settings is a symlink: {settings_path}")
    if not settings_path.is_file():
        return {}
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"claude settings is not valid JSON: {settings_path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"claude settings root must be an object: {settings_path}")
    return loaded


def _wired(data: dict) -> bool:
    hooks = data.get("hooks")
    entries = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return isinstance(entries, list) and _desired_entry() in entries


def inspect_vibewise_hook(paths: AgentbotPaths) -> VibewiseHookState:
    source, destination = hook_source(paths), hook_destination(paths)
    in_sync = (
        source.is_file()
        and destination.is_file()
        and not destination.is_symlink()
        and destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    )
    try:
        wired = _wired(_read_settings(paths.claude_home / "settings.json"))
    except ValueError:
        wired = False
    return VibewiseHookState(vibewise_wanted(paths), in_sync, wired)


def _set_entry(settings_path: Path, wanted: bool) -> str:
    """Add or remove only Agentbot's SessionStart entry; return the action."""
    data = _read_settings(settings_path)
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError(f"claude settings hooks must be an object: {settings_path}")
    hooks = dict(hooks or {})
    entries = hooks.get("SessionStart", [])
    if not isinstance(entries, list):
        raise ValueError(f"claude settings hooks.SessionStart must be a list: {settings_path}")
    kept = [entry for entry in entries if not _is_ours(entry)]
    updated = [*kept, _desired_entry()] if wanted else kept
    if updated == entries:
        return "unchanged"
    if updated:
        hooks["SessionStart"] = updated
    else:
        hooks.pop("SessionStart", None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(settings_path, json.dumps(data, indent=2) + "\n", backup=True)
    return "updated" if wanted else "removed"


def _sync_script(source: Path, destination: Path, wanted: bool) -> str:
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError(f"VibeWise hook path is not a regular file: {destination}")
    existing = destination.read_text(encoding="utf-8") if destination.is_file() else None
    if existing is not None and MANAGED_MARKER not in existing:
        return "preserved"
    if not wanted:
        if existing is None:
            return "absent"
        destination.unlink()
        return "removed"
    desired = source.read_text(encoding="utf-8")
    if existing == desired:
        return "unchanged"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(destination, desired)
    return "updated" if existing is not None else "created"


def install_vibewise_hook(paths: AgentbotPaths) -> tuple[str, str]:
    """Bring the script and the settings entry in line with the manifest."""
    wanted = vibewise_wanted(paths) and hook_source(paths).is_file()
    script = _sync_script(hook_source(paths), hook_destination(paths), wanted)
    if wanted and script == "preserved":
        # A file Agentbot did not write sits at the path; never point the hook at it.
        wanted = False
    settings = _set_entry(paths.claude_home / "settings.json", wanted)
    return script, settings


def doctor_vibewise_hook(
    paths: AgentbotPaths, *, state: VibewiseHookState | None = None
) -> list[DoctorIssue]:
    state = state or inspect_vibewise_hook(paths)
    if state.status_label in {"ok", "not wanted"}:
        return []
    return [
        DoctorIssue(
            level="warning",
            scope="claude-vibewise-hook",
            message=(
                f"VibeWise SessionStart hook is {state.status_label} "
                f"({hook_destination(paths)}); run './install.sh global'"
            ),
        )
    ]
