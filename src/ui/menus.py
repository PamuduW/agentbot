"""The menus themselves.

Where the text of a menu lives. It used to live in `MENU_SIMPLE_*` arrays spread
through scripts/menus/, one block per menu, which meant the labels an operator
reads and the keys the dispatch switches on were written twice in two languages
and kept in step by hand.

Definitions only: what a menu says, not what its choices do. Dispatch is still
Bash, because that is what runs `install.sh` and the CLI, and
tests/test_menu_definitions.sh pins the two halves together -- every key here
has a branch there, and every branch has a key here.

Descriptions are two lines by convention: what the choice does, then what it
touches. The footer draws exactly two rows, so a third line would not be shown
and a missing second line leaves a blank row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Menu:
    title: str
    breadcrumb: str
    labels: tuple[str, ...]
    keys: tuple[str, ...]
    descs: tuple[str, ...] = ()
    types: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if len(self.labels) != len(self.keys):
            raise ValueError(f"{self.title}: {len(self.labels)} labels, {len(self.keys)} keys")
        if self.descs and len(self.descs) != len(self.labels):
            raise ValueError(f"{self.title}: {len(self.descs)} descriptions for {len(self.labels)} entries")


MENUS: dict[str, Menu] = {
    "main": Menu(
        title="Agentbot",
        breadcrumb="Agentbot",
        labels=(
            "Check Status",
            "Install Agentbot",
            "Update",
            "Prune Skills",
            "GitHub Token Config",
            "Workspaces",
            "Platform",
            "Libraries",
            "Quit",
        ),
        keys=(
            "status",
            "install",
            "update",
            "prune-skills",
            "token",
            "workspaces",
            "platform",
            "libraries",
            "quit",
        ),
        descs=(
            "Check the installed Agentbot components and baseline.\n"
            "Read-only status; no updates or writes are performed.",
            "Choose what to set up, then install: skills, Graphify, Boost.\n"
            "Managed outputs, Doctor and the launcher link always run.",
            "Update the repository, reconcile skills, and refresh workspaces plus global outputs.\n"
            "A preview and explicit confirmation are required before mutation.",
            "Select and permanently remove manual, orphaned, excluded, or stale skills.\n"
            "Each candidate shows its classification and source detail before confirmation.",
            "Configure the optional shared GitHub API token.\n"
            "The token is stored outside this repository.",
            "List, preview, and resync locally registered workspaces.\n"
            "Apply actions require explicit confirmation.",
            "Manage VS Code extensions and settings, the Cursor statusline, and the agent CLI configs.\n"
            "Previews are read-only; every apply confirms first.",
            "Open the Agentbot and Graphify command reference libraries.\n"
            "Read-only command and safety information.",
            "Exit the Agentbot menu.\nReturn to the calling process.",
        ),
    ),
    "libraries": Menu(
        title="Libraries",
        breadcrumb="Agentbot › Libraries",
        labels=("Command Lib", "Graphify Lib"),
        keys=("command_lib", "graphify_lib"),
        descs=(
            "Show Agentbot commands and whether they read or mutate state.\n"
            "Use this as the local command reference.",
            "Show Graphify assistant and shell commands plus safety boundaries.\n"
            "Read-only; Install and Update own generic skill synchronization.",
        ),
    ),
    "platform": Menu(
        title="Platform",
        breadcrumb="Agentbot › Platform",
        labels=(
            "VS Code status",
            "VS Code seed",
            "VS Code apply",
            "Cursor statusline status",
            "Cursor statusline install",
            "CLI config status",
            "CLI config apply",
        ),
        keys=(
            "vscode-status",
            "vscode-seed",
            "vscode-apply",
            "cursor-status",
            "cursor-install",
            "cli-config-status",
            "cli-config-apply",
        ),
        descs=(
            "Preview the selected extensions and owned settings for each host.\n"
            "Read-only; writes nothing.",
            "Record the currently installed extensions into vscode.yaml.\n"
            "Writes the manifest in this repository, not your editor.",
            "Install missing extensions and merge owned settings into each host.\n"
            "Backs each settings file up first. Requires confirmation.",
            "Report whether the managed Cursor statusline is installed and current.\n"
            "Read-only; writes nothing.",
            "Install the managed statusline and point the Cursor CLI at it.\n"
            "Writes ~/.cursor and the statusLine block. Requires confirmation.",
            "Preview the declared Claude, Codex, and Cursor CLI configuration keys.\n"
            "Read-only; writes nothing.",
            "Merge the declared keys into each CLI config, rolling back on failure.\n"
            "Backs each config up first. Requires confirmation.",
        ),
    ),
    "workspaces": Menu(
        title="Workspaces",
        breadcrumb="Agentbot › Workspaces",
        labels=(
            "List recorded workspaces",
            "Preview resync (all)",
            "Apply resync (all)",
            "Remove recorded workspaces",
        ),
        keys=("list", "preview", "apply", "remove"),
        descs=(
            "Read the private local workspace registry.\n"
            "No repository or state changes are performed.",
            "Preview managed changes for every enabled workspace plus global Codex/Claude outputs.\n"
            "No files are written.",
            "Apply managed workspace changes and refresh global AGENTS/CLAUDE/statusline outputs.\n"
            "Confirmation is required before mutation.",
            "Select one recorded path and stop managing it.\n"
            "No workspace files are changed or removed.",
        ),
    ),
    "graphify_lib": Menu(
        title="Graphify Lib",
        breadcrumb="Agentbot › Graphify Lib",
        labels=(
            "Assistant commands",
            "Shell query and export commands",
            "Manual platform setup",
            "Agentbot lifecycle boundary",
        ),
        keys=("assistant", "shell", "platform", "boundary"),
        descs=(
            "Commands used inside supported coding-agent conversations.",
            "Read-only queries plus explicit local extraction and export commands.",
            "Manual platform copies; Agentbot does not run these platform-specific paths.",
            "What Agentbot Install and Update own, and what remains manual.",
        ),
    ),
}


# Some menus are not written down, they are derived: the Command Lib lists the
# commands this build actually has. Those come from the same metadata the help
# output does, so a command added to src/commands.py appears in the menu with
# nothing else edited -- which is the whole reason not to write them out here.
BUILDERS: dict[str, Callable[[], Menu]] = {}


def builder(name: str) -> Callable[[Callable[[], Menu]], Callable[[], Menu]]:
    def register(build: Callable[[], Menu]) -> Callable[[], Menu]:
        BUILDERS[name] = build
        return build

    return register


def _command_entries(surface: str) -> tuple[tuple[str, str, str], ...]:
    from src.commands import COMMANDS

    return tuple(
        (
            spec.name,
            f"{spec.name} [{spec.behavior}] — {spec.summary}",
            f"agentbot help {spec.name}",
        )
        for spec in COMMANDS
        if spec.surface == surface
    )


@builder("command_lib")
def _command_lib() -> Menu:
    """The public commands, plus a way into the bootstrap ones.

    `__bootstrap__` is a key with no command behind it; the caller opens the
    other menu when it sees it. It is spelled unmistakably so it cannot collide
    with a real command name.
    """
    entries = _command_entries("public")
    return Menu(
        title="Command Lib",
        breadcrumb="Agentbot › Command Lib",
        labels=(*(label for _, label, _ in entries), "Bootstrap commands"),
        keys=(*(name for name, _, _ in entries), "__bootstrap__"),
        descs=(
            *(desc for _, _, desc in entries),
            "Commands exposed by install.sh for setup and repair.",
        ),
    )


@builder("command_lib_bootstrap")
def _command_lib_bootstrap() -> Menu:
    entries = _command_entries("bootstrap")
    return Menu(
        title="Bootstrap commands",
        breadcrumb="Agentbot › Command Lib › Bootstrap commands",
        labels=tuple(label for _, label, _ in entries),
        keys=tuple(name for name, _, _ in entries),
        descs=tuple(desc for _, _, desc in entries),
    )


@builder("graphify_assistant")
def _graphify_assistant() -> Menu:
    return _graphify_section("assistant")


@builder("graphify_shell")
def _graphify_shell() -> Menu:
    return _graphify_section("shell")


@builder("graphify_platform")
def _graphify_platform() -> Menu:
    return _graphify_section("platform")


def _graphify_section(section: str) -> Menu:
    """The key is the command itself: the detail page needs no other handle on
    the row, and a synthetic id would be one more thing to keep in step."""
    from . import graphify_lib

    rows = graphify_lib.rows(section)
    return Menu(
        title="Graphify commands",
        breadcrumb=f"Agentbot › Graphify Lib › {section.capitalize()}",
        labels=tuple(f"{row.label} — {row.description}" for row in rows),
        keys=tuple(row.command for row in rows),
        descs=tuple(row.command for row in rows),
    )


def menu(name: str) -> Menu:
    if name in BUILDERS:
        return BUILDERS[name]()
    try:
        return MENUS[name]
    except KeyError:
        raise KeyError(f"unknown menu: {name}") from None


def names() -> tuple[str, ...]:
    return tuple(sorted({*MENUS, *BUILDERS}))


# A root menu offers Quit as an entry; a submenu leaves with `q` and has to say
# so. Bash sets MENU_SUBMENU_HINT for the shared loop, but a named menu is drawn
# by the Python loop, which never saw that variable -- so every Agentbot submenu
# accepted `q` and advertised only "Up/Down navigate   Enter confirm", which is
# the gap the Bash-side comment claims to have closed.
#
# Derived from the keys rather than declared per menu: the distinction *is* "does
# this menu have its own way out", and a hand-set field is one more thing a new
# submenu forgets.
SUBMENU_HINT = "Up/Down navigate   Enter confirm   q back"


def as_spec(name: str, *, cols: int, color: bool) -> dict:
    """A definition in the shape the selection loop takes."""
    defined = menu(name)
    from .menu import DEFAULT_HINT

    return {
        "title": defined.title,
        "breadcrumb": defined.breadcrumb,
        "labels": list(defined.labels),
        "keys": list(defined.keys),
        "descs": list(defined.descs),
        "types": list(defined.types),
        "hint": DEFAULT_HINT if "quit" in defined.keys else SUBMENU_HINT,
        "cols": cols,
        "color": color,
    }
