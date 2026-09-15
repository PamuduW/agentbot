"""Install Claude Code statusline script and settings during global render."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import write_text_atomic
from .models import DoctorIssue
from .paths import AgentbotPaths

MANAGED_MARKER = "# Managed by Agentbot."
# Boost's `status-line` component edits this script in place rather than
# replacing it, so the Agentbot marker survives its edit. Without an explicit
# check, the managed-file path below reads that as "stale" and overwrites
# Boost's work -- and `boost setup` refreshes outputs immediately after
# `boost init`, so the revert would land in the same command. Boost has no
# --no-status-line flag to opt out of, so detect it here instead.
#
# Match only comment markers Boost authors itself. The managed script invokes
# `boost status-line` on purpose to render the savings segment, so anything
# matching that invocation would flag our own file as wrapped.
BOOST_STATUSLINE_MARKERS = ("boost-status-line-prev-command", "boost-hook-version")
STATUSLINE_SCRIPT_NAME = "statusline-command.sh"
STATUSLINE_SETTINGS_COMMAND = f"~/.claude/{STATUSLINE_SCRIPT_NAME}"
_STATUSLINE_SHELL_WRAPPERS = {
    "bash",
    "sh",
    "/bin/bash",
    "/bin/sh",
    "/usr/bin/bash",
    "/usr/bin/sh",
}


@dataclass(frozen=True)
class StatuslineState:
    """Observed Claude statusline install state."""

    source_exists: bool
    installed: bool
    in_sync: bool
    managed: bool
    settings_wired: bool
    jq_available: bool
    script_action: str = "unchanged"
    settings_action: str = "unchanged"
    # Declared last so the existing positional order is unchanged.
    boost_wrapped: bool = False

    @property
    def status_label(self) -> str:
        if not self.source_exists:
            return "missing source"
        if not self.installed:
            return "not installed"
        if self.boost_wrapped:
            return "boost-wrapped"
        if self.managed and not self.in_sync:
            return "stale"
        if not self.managed:
            return "user-owned"
        if not self.settings_wired:
            return "script only"
        if not self.jq_available:
            return "needs jq"
        return "ok"

    @property
    def status_result(self) -> str:
        label = self.status_label
        if label == "ok":
            return "ok"
        if label in {"user-owned", "script only", "boost-wrapped"}:
            return "check"
        if label == "needs jq":
            return "check"
        return "missing" if label in {"missing source", "not installed"} else "check"


@dataclass(frozen=True)
class StatuslineInstallResult:
    state: StatuslineState
    script_action: str
    settings_action: str


def statusline_source(paths: AgentbotPaths) -> Path:
    return paths.root / "global" / "claude" / STATUSLINE_SCRIPT_NAME


def statusline_destination(paths: AgentbotPaths) -> Path:
    return paths.claude_home / STATUSLINE_SCRIPT_NAME


def inspect_claude_statusline(paths: AgentbotPaths) -> StatuslineState:
    source = statusline_source(paths)
    destination = statusline_destination(paths)
    source_exists = source.is_file()
    desired = _read_text(source) if source_exists else ""
    installed = destination.is_file()
    existing = _read_text(destination) if installed else ""
    # bool() on the whole expression: `desired and ...` yields "" rather than
    # False when `desired` is empty, and StatuslineState.managed is a bool.
    managed = bool(existing) and bool(
        MANAGED_MARKER in existing or (desired and existing == desired)
    )
    in_sync = bool(desired) and existing == desired
    boost_wrapped = _has_boost_statusline(existing)
    settings_wired = _settings_points_at_managed(paths.claude_home / "settings.json")
    return StatuslineState(
        boost_wrapped=boost_wrapped,
        source_exists=source_exists,
        installed=installed,
        in_sync=in_sync,
        managed=managed if installed else False,
        settings_wired=settings_wired,
        jq_available=shutil.which("jq") is not None,
    )


def doctor_claude_statusline(
    paths: AgentbotPaths, *, state: StatuslineState | None = None
) -> list[DoctorIssue]:
    state = state or inspect_claude_statusline(paths)
    issues: list[DoctorIssue] = []
    source = statusline_source(paths)
    destination = statusline_destination(paths)

    if not state.source_exists:
        issues.append(
            DoctorIssue(
                level="error",
                scope="claude-statusline",
                message=f"Missing managed Claude statusline source: {source}",
            )
        )
        return issues

    if not state.installed:
        issues.append(
            DoctorIssue(
                level="warning",
                scope="claude-statusline",
                message=(
                    f"Claude statusline is not installed at {destination}; "
                    "run './install.sh global' or './install.sh update --yes'"
                ),
            )
        )
    elif state.boost_wrapped:
        issues.append(
            DoctorIssue(
                level="warning",
                scope="claude-statusline",
                message=(
                    f"Claude statusline at {destination} was wrapped by Boost; "
                    "Agentbot is leaving it alone and will not refresh it from "
                    f"{source}. Run 'agentbot boost off' to restore it"
                ),
            )
        )
    elif state.managed and not state.in_sync:
        issues.append(
            DoctorIssue(
                level="warning",
                scope="claude-statusline",
                message=(
                    f"Claude statusline at {destination} is stale versus {source}; "
                    "run './install.sh global' or './install.sh update --yes'"
                ),
            )
        )

    if state.installed and not state.settings_wired:
        settings = paths.claude_home / "settings.json"
        issues.append(
            DoctorIssue(
                level="warning",
                scope="claude-statusline",
                message=(
                    f"Claude settings at {settings} do not wire statusLine to "
                    f"{STATUSLINE_SETTINGS_COMMAND}; run './install.sh global'"
                ),
            )
        )

    if state.source_exists and not state.jq_available:
        issues.append(
            DoctorIssue(
                level="warning",
                scope="claude-statusline",
                message=(
                    "jq is not installed; Claude statusline-command.sh requires jq "
                    "(install with: sudo apt-get install -y jq)"
                ),
            )
        )

    return issues


def install_claude_statusline(paths: AgentbotPaths) -> StatuslineInstallResult:
    """Install managed ~/.claude/statusline-command.sh and wire settings.json."""
    source = statusline_source(paths)
    if not source.is_file():
        state = inspect_claude_statusline(paths)
        return StatuslineInstallResult(
            state=state, script_action="missing_source", settings_action="skipped"
        )

    desired = source.read_text(encoding="utf-8")
    if not desired.endswith("\n"):
        desired += "\n"

    paths.claude_home.mkdir(parents=True, exist_ok=True)
    destination = statusline_destination(paths)
    script_action = _sync_statusline_script(destination, desired)
    settings_action = _ensure_statusline_settings(paths.claude_home / "settings.json")
    state = inspect_claude_statusline(paths)
    return StatuslineInstallResult(
        state=state,
        script_action=script_action,
        settings_action=settings_action,
    )


def _has_boost_statusline(content: str) -> bool:
    return any(marker in content for marker in BOOST_STATUSLINE_MARKERS)


def _read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _sync_statusline_script(destination: Path, desired: str) -> str:
    if destination.is_symlink():
        raise ValueError(f"claude statusline script is a symlink: {destination}")
    if destination.exists() and not destination.is_file():
        raise ValueError(f"claude statusline script is not a regular file: {destination}")

    if destination.is_file():
        existing = destination.read_text(encoding="utf-8")
        if not existing.endswith("\n"):
            existing += "\n"
        if _has_boost_statusline(existing):
            # Boost wrapped the script. Reverting it here would silently undo an
            # integration the user asked for; `agentbot boost off` is the
            # supported way to remove it.
            _ensure_executable(destination)
            return "preserved_boost"
        if MANAGED_MARKER not in existing and existing != desired:
            # Preserve a user-authored script that Agentbot does not own.
            _ensure_executable(destination)
            return "preserved"
        if existing != desired:
            write_text_atomic(destination, desired)
            _ensure_executable(destination)
            return "updated"
        _ensure_executable(destination)
        return "unchanged"

    write_text_atomic(destination, desired)
    _ensure_executable(destination)
    return "created"


def _ensure_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _ensure_statusline_settings(settings_path: Path) -> str:
    """Merge statusLine into settings.json without clobbering unrelated keys."""
    if settings_path.is_symlink():
        raise ValueError(f"claude settings is a symlink: {settings_path}")
    if settings_path.exists() and not settings_path.is_file():
        raise ValueError(f"claude settings is not a regular file: {settings_path}")

    data: dict = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"claude settings is not valid JSON: {settings_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"claude settings root must be an object: {settings_path}")
        data = loaded

    desired_block = {
        "type": "command",
        "command": STATUSLINE_SETTINGS_COMMAND,
    }
    existing = data.get("statusLine")
    if isinstance(existing, dict):
        command = str(existing.get("command") or "")
        if command and not _is_managed_statusline_command(command):
            # User already pointed statusLine elsewhere; leave it alone.
            return "preserved"
        if existing.get("type") == desired_block["type"] and command in {
            STATUSLINE_SETTINGS_COMMAND,
            os.path.expanduser(STATUSLINE_SETTINGS_COMMAND),
        }:
            merged = dict(existing)
            merged.update(desired_block)
            if merged == existing:
                return "unchanged"
            data["statusLine"] = merged
        else:
            data["statusLine"] = {**existing, **desired_block}
    else:
        data["statusLine"] = desired_block

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        settings_path,
        json.dumps(data, indent=2) + "\n",
        backup=True,
    )
    return "updated"


def _settings_points_at_managed(settings_path: Path) -> bool:
    if not settings_path.is_file():
        return False
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    block = loaded.get("statusLine")
    if not isinstance(block, dict):
        return False
    command = str(block.get("command") or "")
    return bool(command) and _is_managed_statusline_command(command)


def _normalized_command_path(value: str) -> Path | None:
    expanded = Path(os.path.expanduser(value))
    if not expanded.is_absolute():
        return None
    return expanded.resolve(strict=False)


def _is_managed_statusline_command(command: str) -> bool:
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return False
    if len(tokens) == 1:
        candidate = tokens[0]
    elif len(tokens) == 2 and tokens[0] in _STATUSLINE_SHELL_WRAPPERS:
        candidate = tokens[1]
    else:
        return False
    observed = _normalized_command_path(candidate)
    managed = _normalized_command_path(STATUSLINE_SETTINGS_COMMAND)
    return observed is not None and managed is not None and observed == managed
