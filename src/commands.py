from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CommandOption:
    usage: str
    description: str
    default: str


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    behavior: Literal["read-only", "mutating"]
    summary: str
    options: tuple[CommandOption, ...]
    effects: str
    examples: tuple[str, ...]
    related: tuple[str, ...]
    surface: Literal["public", "bootstrap"]
    parser_commands: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


def option(usage: str, description: str, default: str) -> CommandOption:
    return CommandOption(usage, description, default)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "status",
        "agentbot status [--json]",
        "read-only",
        "Show installed skills, managed outputs, and diagnostics state.",
        (option("--json", "Emit machine-readable status.", "off"),),
        "Reads skills, locks, links, rendered outputs, and Doctor state.",
        ("agentbot status", "agentbot status --json"),
        ("doctor", "update"),
        "public",
        ("status",),
    ),
    CommandSpec(
        "install",
        "agentbot install [--components L] [--menu]",
        "mutating",
        "Install skills, synchronize optional integrations and editor surfaces, refresh outputs, run Doctor, and link Agentbot.",
        (
            option(
                "--components L",
                "Comma-separated subset of skills, graphify, boost, vscode, cursor, cli-config.",
                "all",
            ),
            option(
                "--menu",
                "Open the component selector and execution plan first; needs a terminal.",
                "off",
            ),
        ),
        "May install skills, reconcile editor surfaces, and write managed global outputs and the launcher link.",
        ("agentbot install", "agentbot install --components skills,boost", "agentbot install --menu"),
        ("status", "doctor"),
        "public",
        ("install",),
    ),
    CommandSpec(
        "full",
        "agentbot full",
        "mutating",
        "Run install, then update, in one command.",
        (),
        "Runs both stages with one exit contract and restarts once if the checkout moves forward.",
        # No parser_commands: `full` is sequenced by install.sh rather than
        # being a Python subcommand.
        ("agentbot full",),
        ("install", "update"),
        "public",
    ),
    CommandSpec(
        "update",
        "agentbot update|upgrade [--dry-run] [--yes]",
        "mutating",
        "Run the repository-first update transaction.",
        (
            option("--dry-run", "Preview reconciliation and managed-surface changes.", "off"),
            option(
                "--yes", "Pre-approve source-owned additions, removals, and manifest edits.", "off"
            ),
        ),
        "May fast-forward the checkout, reconcile source-owned skills, and refresh registered workspaces and global outputs.",
        ("agentbot update --dry-run", "agentbot update --yes"),
        ("install", "status"),
        "public",
        ("update", "upgrade"),
        ("upgrade",),
    ),
    CommandSpec(
        "token",
        "agentbot token",
        "mutating",
        "Configure the optional GitHub API token through the private TTY screen.",
        (),
        "Writes only the private Agentbot token file after confirmation.",
        ("agentbot token",),
        ("skills install", "update"),
        "public",
    ),
    CommandSpec(
        "boot",
        "agentbot boot [SELECTORS] [--profile NAME] [TARGET]",
        "mutating",
        "Create or preserve Agentbot policy outputs in one target and register it.",
        (
            option("--agents | --codex", "Include canonical AGENTS.md.", "always"),
            option("--claude", "Include generated Claude output.", "profile default"),
            option("--cursor", "Include generated Cursor rules.", "profile default"),
            option("--profile NAME", "Select a workspace profile.", "active profile"),
            option("TARGET", "Target directory.", "current directory"),
        ),
        "May write selected Agentbot-managed policy outputs and update the private workspace registry.",
        ("agentbot boot", "agentbot boot --cursor /path/to/repo"),
        ("workspace", "workspaces"),
        "public",
        ("boot",),
    ),
    CommandSpec(
        "workspace",
        "agentbot workspace [--profile NAME] [--targets LIST] [--yes] PATH",
        "mutating",
        "Preview or apply one workspace render.",
        (
            option("--profile NAME", "Select a workspace profile.", "active profile"),
            option(
                "--targets LIST", "Select agents, claude, or cursor outputs.", "profile defaults"
            ),
            option("--yes", "Apply and register instead of previewing.", "off"),
            option("PATH", "Workspace directory.", "required"),
        ),
        "Preview reads target files; --yes may write managed outputs and update the private registry.",
        ("agentbot workspace /path/to/repo", "agentbot workspace --yes /path/to/repo"),
        ("boot", "workspaces", "resync"),
        "public",
        ("workspace",),
    ),
    CommandSpec(
        "workspaces",
        "agentbot workspaces [--paths0 | --remove PATH]",
        "mutating",
        "List registered workspaces or stop managing one recorded path.",
        (
            option("--paths0", "Print canonical paths separated by NUL bytes.", "off"),
            option(
                "--remove PATH",
                "Forget one registry record without changing workspace files.",
                "off",
            ),
        ),
        "Listing is read-only; --remove changes only the private registry.",
        ("agentbot workspaces",),
        ("workspace", "resync"),
        "public",
        ("workspaces",),
    ),
    CommandSpec(
        "resync",
        "agentbot resync [--all | PATH ...] [--yes | --dry-run]",
        "mutating",
        "Preview or refresh registered workspaces and managed global outputs.",
        (
            option("--all", "Select every enabled registered workspace.", "off"),
            option("--yes", "Apply managed changes.", "off"),
            option("--dry-run", "Force preview mode.", "preview"),
            option("PATH ...", "Select explicit registered paths.", "required unless --all"),
        ),
        "Preview reads managed surfaces; --yes may update them.",
        ("agentbot resync --dry-run --all", "agentbot resync --yes --all"),
        ("workspace", "workspaces"),
        "public",
        ("resync",),
    ),
    CommandSpec(
        "doctor",
        "agentbot doctor",
        "read-only",
        "Validate skills, locks, links, rendered outputs, and configuration.",
        (),
        "Reads local Agentbot-managed state without repairing it.",
        ("agentbot doctor",),
        ("status", "update"),
        "public",
        ("doctor",),
    ),
    CommandSpec(
        "graphify",
        "agentbot graphify status|setup",
        "mutating",
        "Inspect or repair the optional generic Graphify Agent Skills integration.",
        (
            option("status", "Inspect CLI, skill, and assistant-link state.", "default"),
            option("setup", "Run generic Agent Skills setup and refresh links.", "explicit"),
        ),
        "Status is read-only; setup may write the generic Graphify skill and managed links.",
        ("agentbot graphify status",),
        ("install", "update"),
        "public",
        ("graphify status", "graphify setup"),
    ),
    CommandSpec(
        "boost",
        "agentbot boost status|setup|off",
        "mutating",
        "Inspect, set up, or remove the optional Boost shell-output integration.",
        (
            option("status", "Inspect CLI, safety config, and per-host state.", "default"),
            option("setup", "Wire Boost for whichever of Claude, Codex, and Cursor are installed.", "explicit"),
            option("off", "Remove Boost integration, including leftover host files.", "explicit"),
        ),
        "Status is read-only; setup/off may update Boost config and agent hooks.",
        ("agentbot boost status",),
        ("install", "doctor"),
        "public",
        ("boost status", "boost setup", "boost off"),
    ),
    CommandSpec(
        "cli-config",
        "agentbot cli-config status|apply",
        "mutating",
        "Merge the declared Claude, Codex, and Cursor CLI configuration keys.",
        (
            option("status", "Preview what a run would change. Writes nothing.", "default"),
            option("apply", "Merge the declared keys, rolling back if any target fails.", "explicit"),
        ),
        "Status is read-only; apply merges owned keys into each CLI config after backing it up.",
        ("agentbot cli-config status", "agentbot cli-config apply"),
        ("doctor", "status"),
        "public",
        ("cli-config status", "cli-config apply"),
    ),
    CommandSpec(
        "cursor",
        "agentbot cursor status|statusline",
        "mutating",
        "Inspect or install the managed Cursor CLI statusline.",
        (
            option("status", "Report the Cursor statusline state. Read-only.", "default"),
            option("statusline", "Install the managed statusline and point Cursor at it.", "explicit"),
        ),
        "Status is read-only; statusline writes ~/.cursor/statusline-command.sh and the statusLine block in cli-config.json.",
        ("agentbot cursor status", "agentbot cursor statusline"),
        ("doctor", "status"),
        "public",
        ("cursor status", "cursor statusline"),
    ),
    CommandSpec(
        "vscode",
        "agentbot vscode status|seed|apply",
        "mutating",
        "Reconcile the selected VS Code extensions and settings for each host.",
        (
            option("status", "Preview what a run would change. Writes nothing.", "default"),
            option("seed", "Record the currently installed extensions in vscode.yaml.", "explicit"),
            option("apply", "Install missing extensions and merge owned settings keys.", "explicit"),
        ),
        "Status is read-only; apply installs extensions and merges owned settings after backing files up.",
        ("agentbot vscode status", "agentbot vscode seed", "agentbot vscode apply"),
        ("doctor", "status"),
        "public",
        ("vscode status", "vscode seed", "vscode apply"),
    ),
    CommandSpec(
        "help",
        "agentbot help [COMMAND]",
        "read-only",
        "Show the command index or one command's complete reference.",
        (option("COMMAND", "Select one public or bootstrap command.", "all commands"),),
        "Prints local metadata and performs no external action.",
        ("agentbot help", "agentbot help workspace"),
        (),
        "public",
        ("help",),
    ),
    CommandSpec(
        "skills install",
        "./install.sh skills install",
        "mutating",
        "Install enabled upstream skills from skills.sources.yaml.",
        (),
        "May update the global skill store, lock, and managed assistant links.",
        ("./install.sh skills install",),
        ("skills update", "skills prune", "skills doctor"),
        "bootstrap",
        ("skills install",),
    ),
    CommandSpec(
        "skills update",
        "./install.sh skills update|upgrade",
        "mutating",
        "Refresh globally installed skills from the lock.",
        (),
        "May update source-owned global skills and managed assistant links.",
        ("./install.sh skills update",),
        ("update", "skills prune", "skills list"),
        "bootstrap",
        ("skills update", "skills upgrade"),
        ("skills upgrade",),
    ),
    CommandSpec(
        "skills list",
        "./install.sh skills list",
        "read-only",
        "List installed global skills.",
        (),
        "Reads the global skill store.",
        ("./install.sh skills list",),
        ("skills install", "skills prune", "skills doctor"),
        "bootstrap",
        ("skills list",),
    ),
    CommandSpec(
        "skills doctor",
        "./install.sh skills doctor",
        "read-only",
        "Validate skill sources and tooling prerequisites.",
        (),
        "Reads the manifest, lock, installed store, and tool availability.",
        ("./install.sh skills doctor",),
        ("doctor", "skills install", "skills prune"),
        "bootstrap",
        ("skills doctor",),
    ),
    CommandSpec(
        "skills prune",
        "./install.sh skills prune [SKILL ...] [--yes] [--include-manual] [--candidates0]",
        "mutating",
        "Preview or remove installed skills no active manifest source wants.",
        (
            option(
                "SKILL ...",
                "Select exact prune candidate names.",
                "all non-manual candidates",
            ),
            option("--yes", "Apply the planned removals.", "off"),
            option(
                "--include-manual",
                "Also remove user-placed skills that have no lock entry.",
                "off",
            ),
            option(
                "--candidates0",
                "Print candidate name, reason, and detail as NUL-separated fields.",
                "off",
            ),
        ),
        "Without --yes it only reads state; with --yes it removes only the selected candidates, or every default candidate when no names are given.",
        (
            "./install.sh skills prune",
            "./install.sh skills prune gitlab-ci terraform --yes",
        ),
        ("skills doctor", "skills update", "skills list"),
        "bootstrap",
        ("skills prune",),
    ),
    CommandSpec(
        "skills remove-manual",
        "./install.sh skills remove-manual [SKILL ...] [--yes] [--names0]",
        "mutating",
        "List, preview, or selectively remove user-placed global skills.",
        (
            option("SKILL ...", "Select exact manual skill names.", "all in preview; none on apply"),
            option("--yes", "Permanently remove the selected skills.", "off"),
            option("--names0", "Print eligible names separated by NUL bytes.", "off"),
        ),
        "Without --yes it only reads state; with --yes it removes only selected manual skills and Agentbot-owned links.",
        (
            "./install.sh skills remove-manual",
            "./install.sh skills remove-manual gpt-taste mermaid --yes",
        ),
        ("skills prune", "skills install", "skills doctor"),
        "bootstrap",
        ("skills remove-manual",),
    ),
    CommandSpec(
        "global",
        "./install.sh global",
        "mutating",
        "Render managed global Codex and Claude outputs.",
        (),
        "Writes Agentbot-managed global outputs from canonical sources.",
        ("./install.sh global",),
        ("status", "doctor"),
        "bootstrap",
        ("global",),
    ),
)


def command_by_name(name: str) -> CommandSpec:
    normalized = " ".join(name.strip().split())
    for command in COMMANDS:
        if normalized == command.name or normalized in command.aliases:
            return command
    raise KeyError(name)


def commands_for_surface(surface: Literal["public", "bootstrap"]) -> tuple[CommandSpec, ...]:
    return tuple(command for command in COMMANDS if command.surface == surface)
