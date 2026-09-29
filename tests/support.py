from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.paths import AgentbotPaths


def run_cli_main(argv: list[str]) -> tuple[int, str, str]:
    from src.cli import main

    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(sys, "argv", argv),
        patch("sys.stdout", stdout),
        patch("sys.stderr", stderr),
    ):
        return main(), stdout.getvalue(), stderr.getvalue()


class TerminalInput(io.StringIO):
    """Standard input that claims to be a terminal, holding what the person types."""

    def isatty(self) -> bool:
        return True


def isolated_launcher_env(temporary_root: Path) -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    fixture_bin = temporary_root / "path-bin"
    fixture_bin.mkdir(exist_ok=True)
    for command in ("bash", "dirname", "env", "python3"):
        executable = shutil.which(command)
        if executable is None:
            raise RuntimeError(f"required base command is unavailable: {command}")
        link = fixture_bin / command
        if not link.exists():
            link.symlink_to(executable)
    return {
        "AGENTBOT_HOME": str(root),
        "AGENTBOT_TTY": "0",
        "HOME": str(temporary_root / "home"),
        "NO_COLOR": "1",
        "PATH": str(fixture_bin),
        "XDG_CONFIG_HOME": str(temporary_root / "config"),
    }


def run_agentbot_launcher(
    args: tuple[str, ...], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [str(root / "bin" / "agentbot"), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def configure_update(lifecycle: MagicMock, result) -> None:
    from src.models import UpdateOutcome, UpdatePlan, UpdateSnapshot
    from src.skill_reconcile import SkillReconcilePlan
    from src.workspace_service import WorkspaceReport

    workspace_report = result.workspace_report or WorkspaceReport(())
    lifecycle.plan_update.return_value = UpdatePlan(
        UpdateSnapshot("head", "manifest", None),
        SkillReconcilePlan(
            (),
            tuple(result.added_skills),
            tuple(result.removed_skills),
            (),
            (),
            (),
        ),
        "skip",
        workspace_report,
    )
    lifecycle.apply_update.return_value = UpdateOutcome(
        result.status,
        result.message,
        reconcile=result,
        workspace_report=result.workspace_report,
    )


def agentbot_paths(root: Path) -> AgentbotPaths:
    home = root / "home"
    return AgentbotPaths(
        root=root,
        codex_home=home / ".codex",
        claude_home=home / ".claude",
        cursor_home=home / ".cursor",
        config_home=home / ".config" / "agentbot",
        # Explicit, so isolation never depends on when HOME was patched.
        agents_home=home / ".agents",
    )


@contextmanager
def isolated_agentbot_paths() -> Iterator[tuple[Path, AgentbotPaths]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "agentbot"
        root.mkdir()
        yield root, agentbot_paths(root)


@contextmanager
def temporary_git_workspace() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(workspace)],
            check=True,
            capture_output=True,
            text=True,
        )
        yield workspace


def write_skills_manifest(root: Path, *, sources: str = "sources: []\n") -> Path:
    manifest = root / "skills.sources.yaml"
    manifest.write_text(
        f"version: 1\nagents: [cursor, codex, claude-code]\nscope: global\n{sources}",
        encoding="utf-8",
    )
    return manifest


def write_global_lock(paths: AgentbotPaths, skills: dict[str, object]) -> Path:
    paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
    paths.global_skill_lock.write_text(json.dumps({"skills": skills}, indent=2), encoding="utf-8")
    return paths.global_skill_lock


def create_skill(paths: AgentbotPaths, name: str, *, body: str | None = None) -> Path:
    skill = paths.agents_skills_home / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(body or f"# {name}\n", encoding="utf-8")
    return skill
