from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentbotPaths:
    root: Path
    codex_home: Path
    claude_home: Path
    cursor_home: Path
    config_home: Path = field(default_factory=lambda: _default_config_home())
    # Injectable like the other homes. This used to be computed from
    # Path.home() inside the properties below, which meant a caller could point
    # root/claude/codex/cursor at a temp directory and still read and write the
    # real ~/.agents skill store. Declared last so the existing positional
    # order (root, codex, claude, cursor, config) is unchanged.
    agents_home: Path = field(default_factory=lambda: Path.home() / ".agents")
    # Antigravity's home (~/.gemini). Left unset it follows codex_home's home
    # folder, never Path.home(), so a caller that injects the other homes cannot
    # reach the real ~/.gemini through a forgotten default.
    gemini_home: Path | None = None

    @property
    def antigravity_home(self) -> Path:
        return self.gemini_home or self.codex_home.parent / ".gemini"

    @property
    def antigravity_config(self) -> Path:
        return self.antigravity_home / "config"

    @property
    def global_agents(self) -> Path:
        return self.root / "global" / "AGENTS.md"

    @property
    def skills_sources_file(self) -> Path:
        return self.root / "skills.sources.yaml"

    @property
    def skills_lock_file(self) -> Path:
        return self.root / "skills-lock.json"

    @property
    def agents_skills_home(self) -> Path:
        return self.agents_home / "skills"

    @property
    def claude_skills_home(self) -> Path:
        return self.claude_home / "skills"

    @property
    def global_skill_lock(self) -> Path:
        return self.agents_home / ".skill-lock.json"

    @property
    def workspace_profiles_file(self) -> Path:
        return self.root / "agentos.yaml"

    @property
    def workspace_state_file(self) -> Path:
        return self.config_home / "workspaces.json"

    @property
    def mcp_catalog_file(self) -> Path:
        return self.root / "mcp" / "catalog.json"

    @property
    def mcp_state_file(self) -> Path:
        return self.config_home / "mcp.json"

    @property
    def mcp_backup_home(self) -> Path:
        return self.config_home / "backups" / "mcp"


def default_paths(root: Path | None = None) -> AgentbotPaths:
    home = Path.home()
    product_root = root or Path(
        os.environ.get("AGENTBOT_HOME", Path(__file__).resolve().parents[1])
    )
    return AgentbotPaths(
        root=product_root,
        codex_home=Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser(),
        claude_home=Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser(),
        cursor_home=home / ".cursor",
        agents_home=home / ".agents",
        gemini_home=home / ".gemini",
    )


def _default_config_home() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"
    return base / "agentbot"
