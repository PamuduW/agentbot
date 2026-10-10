"""Antigravity CLI (`agy`) global outputs.

Antigravity reads global rules from ``~/.gemini/config/AGENTS.md`` and finds
skills through ``~/.gemini/config/skills.json``. Agentbot renders the first like
the Claude and Codex baselines and keeps one entry in the second that points at
the shared ``~/.agents/skills`` store, so no per-skill links are needed.

``~/.gemini`` is also the Gemini CLI's home; nothing here writes outside
``config/``. Both outputs are written only once Antigravity is present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from .atomic_io import write_text_atomic
from .models import DoctorIssue
from .paths import AgentbotPaths
from .workspace_render import RenderAction

SKILLS_CONFIG_LABEL = "~/.gemini/config/skills.json"

_Kind = Literal["create", "update", "unchanged", "conflict"]


def antigravity_present(paths: AgentbotPaths) -> bool:
    home = paths.codex_home.parent
    return (
        (home / ".local" / "bin" / "agy").exists()
        or (paths.antigravity_home / "antigravity-cli").is_dir()
        or paths.antigravity_config.is_dir()
    )


def antigravity_rules_path(paths: AgentbotPaths) -> Path:
    return paths.antigravity_config / "AGENTS.md"


def antigravity_skills_config_path(paths: AgentbotPaths) -> Path:
    return paths.antigravity_config / "skills.json"


def plan_antigravity_skills_config(paths: AgentbotPaths) -> RenderAction:
    content, kind, detail = _skills_config_change(paths)
    return RenderAction(SKILLS_CONFIG_LABEL, kind, content, detail)


def apply_antigravity_skills_config(paths: AgentbotPaths) -> None:
    content, kind, _ = _skills_config_change(paths)
    if kind in {"create", "update"} and content is not None:
        write_text_atomic(antigravity_skills_config_path(paths), content)


def _skills_config_change(paths: AgentbotPaths) -> tuple[str | None, _Kind, str]:
    destination = antigravity_skills_config_path(paths)
    store = str(paths.agents_skills_home)
    entry = {"path": store}
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        return None, "conflict", f"preserving {SKILLS_CONFIG_LABEL}: not a regular file"
    if not destination.exists():
        return _dump({"entries": [entry]}), "create", f"would create {SKILLS_CONFIG_LABEL}"

    try:
        document = json.loads(destination.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeError):
        return None, "conflict", f"preserving unreadable {SKILLS_CONFIG_LABEL}"
    entries = document.get("entries", []) if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return None, "conflict", f"preserving {SKILLS_CONFIG_LABEL}: unexpected shape"

    wanted = Path(store).expanduser()
    for existing in entries:
        if isinstance(existing, dict) and isinstance(existing.get("path"), str):
            if Path(existing["path"]).expanduser() == wanted:
                return None, "unchanged", f"{SKILLS_CONFIG_LABEL} lists the shared skill store"

    # Other entries and keys (inherits, excludes) belong to the user.
    document["entries"] = [*entries, entry]
    return _dump(document), "update", f"would add the shared skill store to {SKILLS_CONFIG_LABEL}"


def _dump(document: object) -> str:
    return json.dumps(document, indent=2) + "\n"


def doctor_antigravity(paths: AgentbotPaths) -> list[DoctorIssue]:
    if not antigravity_present(paths):
        return []
    issues: list[DoctorIssue] = []
    rules = antigravity_rules_path(paths)
    if not rules.is_file():
        issues.append(
            DoctorIssue(
                level="warning",
                scope="antigravity",
                message=(
                    f"Antigravity global rules are not rendered at {rules}; "
                    "run './install.sh global' or './install.sh update --yes'"
                ),
            )
        )
    _, kind, detail = _skills_config_change(paths)
    if kind == "conflict":
        issues.append(DoctorIssue(level="warning", scope="antigravity", message=detail))
    elif kind != "unchanged":
        issues.append(
            DoctorIssue(
                level="warning",
                scope="antigravity",
                message=(
                    f"{SKILLS_CONFIG_LABEL} does not list the shared skill store; "
                    "run './install.sh global' or './install.sh update --yes'"
                ),
            )
        )
    return issues
