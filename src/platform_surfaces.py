"""The editor and CLI surfaces, as one install component each.

VS Code, the Cursor statusline and the CLI configuration used to live behind a
Platform submenu with seven entries of their own. An install could therefore
finish while leaving all three untouched and say nothing about any of them,
which is the opposite of what an install report is for.

Each is reduced here to one row: a label, what it found, and a result the
vocabulary knows. The full per-host tables the `vscode`, `cursor` and
`cli-config` commands print are unchanged -- this is the summary line, and the
command is where the detail still lives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import PlatformOutcome

if TYPE_CHECKING:
    from .command_runner import CommandRunner
    from .paths import AgentbotPaths


def _vscode(paths: AgentbotPaths, runner: CommandRunner, *, apply: bool) -> tuple[str, str]:
    from pathlib import Path

    from . import vscode as vscode_module

    home = Path.home()
    report = (
        vscode_module.apply(home, paths.root, runner)
        if apply
        else vscode_module.preview(home, paths.root)
    )
    if report.manifest_error:
        return report.manifest_error, "error"
    if report.failures:
        return f"{len(report.failures)} host(s) need attention", "check"
    if report.has_work:
        return "changes pending; run `agentbot vscode apply`", "check"
    return "extensions and settings current", "ok"


def _cursor(paths: AgentbotPaths, runner: CommandRunner, *, apply: bool) -> tuple[str, str]:
    from .cursor_statusline import inspect_cursor_statusline, install_cursor_statusline

    state = (
        install_cursor_statusline(paths) if apply else inspect_cursor_statusline(paths)
    )
    return f"{state.state}: {state.detail}", state.result


def _cli_config(paths: AgentbotPaths, runner: CommandRunner, *, apply: bool) -> tuple[str, str]:
    from . import cli_config as cli_config_module

    report = cli_config_module.apply(paths) if apply else cli_config_module.preview(paths)
    if report.failures:
        return f"{len(report.failures)} CLI(s) need attention", "check"
    pending = [name for name, plan in report.plans.items() if not plan.is_noop and not plan.skipped]
    if pending:
        return f"{len(pending)} to merge; run `agentbot cli-config apply`", "check"
    declared = [name for name, plan in report.plans.items() if not plan.skipped]
    if not declared:
        # None of the three CLIs is on this machine. Nothing was inspected,
        # which is not the same as everything being fine.
        return "no CLI configuration on this machine", "skipped"
    return f"{len(declared)} CLI(s) current", "ok"


_SURFACES = {"vscode": _vscode, "cursor": _cursor, "cli-config": _cli_config}


def surface_outcome(
    key: str,
    label: str,
    paths: AgentbotPaths,
    runner: CommandRunner,
    *,
    apply: bool,
) -> PlatformOutcome:
    """One surface's row. A surface that raises is reported, never fatal.

    An install that has already written skills and outputs must not be undone
    by an editor probe: the row says what went wrong and the run carries on,
    which is how every other component here behaves.
    """
    try:
        detail, result = _SURFACES[key](paths, runner, apply=apply)
    except Exception as error:
        detail, result = f"{type(error).__name__}: {error}", "error"
    return PlatformOutcome(key=key, label=label, detail=detail, result=result)
