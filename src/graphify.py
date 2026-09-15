from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from .command_runner import CommandRunner
from .paths import AgentbotPaths


@dataclass(frozen=True)
class GraphifyStatus:
    state: str
    cli_path: Path | None
    cli_version: str | None
    skill_path: Path
    skill_version: str | None
    codex_state: str
    claude_state: str
    message: str


_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
DEFAULT_GRAPHIFY_TIMEOUT_SECONDS = 300
GRAPHIFY_TIMEOUT_ENV = "AGENTBOT_GRAPHIFY_TIMEOUT_SECONDS"


class GraphifyIntegration:
    """Inspect and install Graphify's global, skill-only Agent Skills copy."""

    def __init__(self, paths: AgentbotPaths, *, runner: CommandRunner | None = None) -> None:
        self.paths = paths
        self._runner = runner or CommandRunner()

    @staticmethod
    def _timeout_seconds() -> int:
        raw = os.environ.get(GRAPHIFY_TIMEOUT_ENV, str(DEFAULT_GRAPHIFY_TIMEOUT_SECONDS))
        try:
            timeout = int(raw)
        except ValueError as error:
            raise ValueError(f"{GRAPHIFY_TIMEOUT_ENV} must be a positive integer") from error
        if timeout <= 0:
            raise ValueError(f"{GRAPHIFY_TIMEOUT_ENV} must be a positive integer")
        return timeout

    @property
    def skill_path(self) -> Path:
        return self.paths.agents_skills_home / "graphify" / "SKILL.md"

    @property
    def version_path(self) -> Path:
        return self.skill_path.parent / ".graphify_version"

    def status(self) -> GraphifyStatus:
        cli_path = self._find_cli()
        cli_version = self._cli_version(cli_path)
        skill_path = self.skill_path
        skill_exists = skill_path.is_file()
        skill_version = self._read_version()
        codex_state = self._target_state(self.paths.codex_home / "skills" / "graphify")
        claude_state = self._target_state(self.paths.claude_skills_home / "graphify")

        if cli_path is None and not skill_exists:
            state = "not-installed"
            message = "Graphify CLI and Agent Skills integration are not installed."
        elif cli_path is None:
            state = "skill-without-cli"
            message = "Graphify skill exists, but the graphify CLI is not installed."
        elif not skill_exists:
            state = "cli-only"
            message = "Graphify CLI is installed; the Agent Skills integration is not set up."
        elif skill_version is None or self._version_token(cli_version) != self._version_token(
            skill_version
        ):
            state = "stale"
            message = "Graphify CLI and installed skill versions do not match."
        elif "conflict" in {codex_state, claude_state}:
            state = "conflict"
            targets = [
                label
                for label, target_state in (("Codex", codex_state), ("Claude", claude_state))
                if target_state == "conflict"
            ]
            message = (
                "A user-owned "
                + ", ".join(targets)
                + " target conflicts with Graphify's managed link."
            )
        else:
            state = "ready"
            message = "Graphify CLI and Agent Skills integration are ready."

        return GraphifyStatus(
            state=state,
            cli_path=cli_path,
            cli_version=cli_version,
            skill_path=skill_path,
            skill_version=skill_version,
            codex_state=codex_state,
            claude_state=claude_state,
            message=message,
        )

    def setup(self) -> GraphifyStatus:
        current = self.status()
        if current.cli_path is None:
            return replace(
                current,
                message=(
                    f"{current.message} Install it from Dotfiles > Install Dotfiles > "
                    "Graphify CLI, or run: uv tool install graphifyy"
                ),
            )

        result = self._runner.run(
            [str(current.cli_path), "install", "--platform", "agents"],
            timeout_seconds=self._timeout_seconds(),
        )
        if result.returncode != 0:
            return replace(
                current,
                state="broken",
                message=f"Graphify skill setup failed: {result.detail()}",
            )

        refreshed = self.status()
        if not refreshed.skill_path.is_file():
            return replace(
                refreshed,
                state="broken",
                message="Graphify reported success but did not create the Agent Skills file.",
            )
        return refreshed

    def refresh_if_enabled(self) -> GraphifyStatus:
        if not self.skill_path.is_file():
            return self.status()
        return self.setup()

    def _find_cli(self) -> Path | None:
        command = shutil.which("graphify")
        return Path(command) if command else None

    def _cli_version(self, cli_path: Path | None) -> str | None:
        if cli_path is None:
            return None
        result = self._runner.run(
            [str(cli_path), "--version"],
            timeout_seconds=self._timeout_seconds(),
        )
        if result.returncode != 0:
            return None
        output = (result.stdout or result.stderr).strip()
        return output.splitlines()[0] if output else None

    def _read_version(self) -> str | None:
        try:
            value = self.version_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    @staticmethod
    def _version_token(value: str | None) -> str | None:
        if not value:
            return None
        match = _VERSION_RE.search(value)
        return match.group(0) if match else value.strip()

    def _target_state(self, target: Path) -> str:
        if not target.exists() and not target.is_symlink():
            return "missing"
        if target.is_symlink():
            try:
                resolved = target.resolve(strict=True)
            except OSError:
                return "conflict"
            return "linked" if resolved == self.skill_path.parent.resolve() else "conflict"
        return "conflict"
