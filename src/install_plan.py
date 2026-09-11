"""What an install would do, per selected component.

Shown between the selector and the run, the way the sibling product shows an
execution plan before it installs anything. The selector says what was chosen;
this says what choosing it means, which is the question the operator actually
has in front of a confirm prompt.

Everything here reads local state only. The plan sits between a keystroke and a
prompt, so it must not go to the network -- the update's plan does, and that is
why it takes ten seconds and reports progress while it works.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .lifecycle import Lifecycle


class PlanRow(NamedTuple):
    component: str
    installed: str
    available: str
    action: str

    @property
    def changes(self) -> bool:
        return self.action not in {"up to date", "skip"}


def _skills(lifecycle: Lifecycle) -> tuple[str, str, str]:
    from .skills_sources import load_skills_sources

    installed = len(lifecycle.diagnostics.collect().installed_skills)
    try:
        config = load_skills_sources(lifecycle.paths.skills_sources_file)
        sources = len(config.active_sources())
    except Exception:
        return f"{installed} skill(s)", "manifest unreadable", "check"
    return f"{installed} skill(s)", f"{sources} source(s)", "reconcile"


def _graphify(lifecycle: Lifecycle) -> tuple[str, str, str]:
    status = lifecycle.graphify.status()
    installed = status.cli_version or ("not installed" if status.cli_path is None else "present")
    if status.cli_path is None:
        return installed, "—", "skip"
    return installed, "—", "refresh" if status.skill_path.is_file() else "install"


def _boost(lifecycle: Lifecycle) -> tuple[str, str, str]:
    status = lifecycle.boost.status()
    if status.cli_path is None:
        return "not installed", "—", "skip"
    return status.cli_version or "present", "—", "configure"


def _platform(lifecycle: Lifecycle, key: str) -> tuple[str, str, str]:
    from .platform_surfaces import surface_outcome

    outcome = surface_outcome(key, key, lifecycle.paths, lifecycle.command_runner, apply=False)
    if outcome.result == "ok":
        return outcome.detail, "none", "up to date"
    if outcome.result == "skipped":
        return outcome.detail, "—", "skip"
    return outcome.detail, "pending", "apply"


_BUILDERS = {
    "skills": _skills,
    "graphify": _graphify,
    "boost": _boost,
}

#: Selector label per key, so the plan names components the way the screen
#: before it did.
LABELS = {
    "skills": "Skills",
    "graphify": "Graphify",
    "boost": "Boost",
    "vscode": "VS Code",
    "cursor": "Cursor statusline",
    "cli-config": "CLI config",
}


def build_install_plan(lifecycle: Lifecycle, components: tuple[str, ...]) -> list[PlanRow]:
    """One row per selected component, in the order the selector offers them."""
    rows: list[PlanRow] = []
    for key in lifecycle.SELECTABLE_COMPONENTS:
        if key not in components:
            continue
        builder = _BUILDERS.get(key)
        try:
            installed, available, action = (
                builder(lifecycle) if builder else _platform(lifecycle, key)
            )
        except Exception as error:
            installed, available, action = f"{type(error).__name__}: {error}", "—", "check"
        rows.append(PlanRow(LABELS.get(key, key), installed, available, action))
    return rows
