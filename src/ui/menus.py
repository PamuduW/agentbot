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


def menu(name: str) -> Menu:
    try:
        return MENUS[name]
    except KeyError:
        raise KeyError(f"unknown menu: {name}") from None


def as_spec(name: str, *, cols: int, color: bool) -> dict:
    """A definition in the shape the selection loop takes."""
    defined = menu(name)
    return {
        "title": defined.title,
        "breadcrumb": defined.breadcrumb,
        "labels": list(defined.labels),
        "keys": list(defined.keys),
        "descs": list(defined.descs),
        "types": list(defined.types),
        "cols": cols,
        "color": color,
    }
