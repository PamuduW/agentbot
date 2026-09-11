from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

from .boost import BoostIntegration
from .commands import CommandSpec, command_by_name
from .diagnostics import Diagnostics
from .graphify import GraphifyIntegration
from .lifecycle import Lifecycle
from .paths import AgentbotPaths, default_paths
from .skills_installer import SkillsInstallError, parse_update_output
from .ui import (
    print_boost_status,
    print_command_help,
    print_doctor_summary,
    print_graphify_status,
    print_header,
    print_manual_skill_removal_report,
    print_output_refresh_report,
    print_reconciliation_report,
    print_rollup,
    print_skill_prune_report,
    print_skills_report,
    print_skills_update_report,
    print_status_summary,
    print_table,
    print_update_outcome,
    print_update_plan,
    print_vscode_report,
    print_workspace_list,
    print_workspace_removed,
    print_workspace_report,
    print_workspace_resync_report,
)
from .ui.install_log import InstallLog, log_legend
from .workspace_render import WORKSPACE_TARGETS

ARCHIVED_COMMANDS = frozenset(
    {
        "all",
        "interactive",
        "import-local",
        "remove-managed",
        "delete-local",
    }
)


def _workspace_report_has_failures(report) -> bool:
    if report is None:
        return False
    if any(item.status in {"conflict", "failed"} for item in report.results):
        return True
    return any(action.kind == "conflict" for action in report.global_actions)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    command = args.command or "install"
    if command == "help":
        help_topic = " ".join(getattr(args, "help_topic", ())) or None
        return print_help_command(
            help_topic,
            output_format=getattr(args, "help_format", "plain"),
        )

    paths = default_paths(Path(args.root))
    diagnostics = Diagnostics(paths)
    lifecycle = Lifecycle(
        paths,
        diagnostics=diagnostics,
        graphify=GraphifyIntegration(paths),
        boost=BoostIntegration(paths),
    )
    context = CommandContext(args=args, paths=paths, diagnostics=diagnostics, lifecycle=lifecycle)

    if command in ARCHIVED_COMMANDS:
        return _archived_command_error(command)
    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        raise SystemExit(f"unknown command: {command}")

    try:
        return handler(context)
    except (SkillsInstallError, ValueError, OSError) as error:
        return print_skills_error(error)


@dataclass(frozen=True)
class CommandContext:
    """Everything a command handler needs, assembled once by main()."""

    args: argparse.Namespace
    paths: AgentbotPaths
    diagnostics: Diagnostics
    lifecycle: Lifecycle


def _handle_status(context: CommandContext) -> int:
    if bool(getattr(context.args, "status_json", False)):
        print_status_json(context.diagnostics)
        return 0
    return print_status(
        context.diagnostics,
        include_issues=bool(getattr(context.args, "status_doctor", False)),
    )


def _handle_global(context: CommandContext) -> int:
    context.lifecycle.render_global()
    return 0


def _handle_doctor(context: CommandContext) -> int:
    return print_doctor(context.diagnostics)


def _handle_graphify(context: CommandContext) -> int:
    graphify_command = getattr(context.args, "graphify_command", None) or "status"
    status = (
        context.lifecycle.setup_graphify()
        if graphify_command == "setup"
        else context.lifecycle.graphify_status()
    )
    print_graphify_status(status)
    if graphify_command == "status":
        return 1 if status.state == "broken" else 0
    return 0 if status.state in {"ready", "conflict", "stale"} else 1


def _handle_cli_config(context: CommandContext) -> int:
    from . import cli_config as cli_config_module

    command = getattr(context.args, "cli_config_command", None) or "status"
    report = (
        cli_config_module.apply(context.paths)
        if command == "apply"
        else cli_config_module.preview(context.paths)
    )
    print_header("CLI config", "Agentbot \u203a CLI config")
    rows = []
    for name, plan in sorted(report.plans.items()):
        if plan.error:
            rows.append((name, plan.error, "check"))
        elif plan.skipped:
            rows.append((name, plan.skipped, "skipped"))
        elif plan.is_noop:
            rows.append((name, "current", "ok"))
        else:
            summary = f"{len(plan.additions)} to add, {len(plan.changes)} to change"
            rows.append((name, f"{summary} in {plan.path}", "check"))
    ok, check, miss = print_table(rows)
    print_rollup(ok=ok, check=check, miss=miss)
    if report.rolled_back:
        print()
        print(f"  Rolled back: {', '.join(report.rolled_back)}")
    return 1 if report.failures else 0


def _handle_cursor(context: CommandContext) -> int:
    from .cursor_statusline import inspect_cursor_statusline, install_cursor_statusline

    command = getattr(context.args, "cursor_command", None) or "status"
    state = (
        install_cursor_statusline(context.paths)
        if command == "statusline"
        else inspect_cursor_statusline(context.paths)
    )
    print_header("Cursor", "Agentbot \u203a Cursor")
    ok, check, miss = print_table(
        [("Statusline", f"{state.state}: {state.detail}", state.result)]
    )
    print_rollup(ok=ok, check=check, miss=miss)
    # Non-zero means something is wrong, not something is pending -- the same
    # rule _handle_boost and _handle_vscode follow, and the one
    # doctor_cursor_statusline already encodes by rating only "broken" an error.
    # "missing" is the expected state before `agentbot cursor statusline` runs;
    # reporting it as process failure made the command unusable in any && chain
    # or CI gate, and disagreed with its two siblings on the same menu.
    return 1 if state.state == "broken" else 0


def _handle_vscode(context: CommandContext) -> int:
    from . import vscode as vscode_module

    command = getattr(context.args, "vscode_command", None) or "status"
    root = context.paths.root
    home = Path.home()
    if command == "seed":
        hosts = vscode_module.resolve_hosts(home)
        target = vscode_module.manifest_path(root)
        manifest = vscode_module.seed_manifest(target, hosts)
        counts = ", ".join(
            f"{host}: {len(values)}" for host, values in sorted(manifest.extensions.items())
        )
        print(f"  Recorded installed extensions in {target}")
        print(f"    {counts or 'no hosts available'}")
        return 0

    report = (
        vscode_module.apply(home, root, context.lifecycle.command_runner)
        if command == "apply"
        else vscode_module.preview(home, root)
    )
    print_vscode_report(report)
    if report.manifest_error:
        return 1
    return 1 if report.failures else 0


def _handle_boost(context: CommandContext) -> int:
    command = getattr(context.args, "boost_command", None) or "status"
    if command == "setup":
        status = context.lifecycle.setup_boost()
    elif command == "off":
        status = context.lifecycle.disable_boost()
    else:
        status = context.lifecycle.boost_status()
    print_boost_status(status)
    return 1 if status.state in {"broken", "forbidden", "unsafe-config"} else 0


def _handle_update(context: CommandContext) -> int:
    args = context.args
    command = args.command
    plan = context.lifecycle.plan_update()
    print_update_plan(plan, command=command)
    if bool(getattr(args, "dry_run", False)):
        return 0
    if bool(getattr(args, "interactive", False)):
        if not confirm_update_plan():
            print("  Update cancelled.")
            return 0
    elif not bool(getattr(args, "confirm", False)) and (
        plan.reconcile.wildcard_additions
        or plan.reconcile.wildcard_removals
        or plan.reconcile.manifest_changes
    ):
        print("  confirmation_required: rerun with --yes to apply this plan")
        return 0

    outcome = context.lifecycle.apply_update(plan)
    print_update_outcome(outcome)
    if outcome.reconcile is not None:
        print_reconciliation_report(outcome.reconcile)
    workspace_report = outcome.workspace_report
    if workspace_report is not None:
        print_workspace_resync_report(workspace_report)
    if outcome.status not in {"applied", "applied-with-local-changes"}:
        return 1
    return 1 if _workspace_report_has_failures(workspace_report) else 0


def caller_path(value: str) -> Path:
    """Resolve a user-supplied path against the directory the operator ran from.

    `install.sh` cd's to the Agentbot checkout before dispatching, so the
    process working directory is never the operator's. It exports the original
    directory first; without this, every relative workspace path -- including
    `boot`'s `.` default -- silently resolved to the Agentbot checkout itself.
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    caller = os.environ.get("AGENTBOT_CALLER_PWD")
    if caller:
        base = Path(caller)
        if base.is_absolute() and base.is_dir():
            return base / path
    return path


def _handle_workspace(context: CommandContext) -> int:
    args = context.args
    targets = parse_workspace_targets(args.targets)
    path = caller_path(args.path)
    if args.yes:
        result = context.lifecycle.apply_workspace(
            path, profile=args.profile, targets=targets, register=True
        )
    else:
        result = context.lifecycle.preview_workspace(
            path, profile=args.profile, targets=targets
        )
    print_workspace_report(result)
    return 1 if result.status in {"conflict", "failed"} else 0


def _handle_boot(context: CommandContext) -> int:
    args = context.args
    target = caller_path(args.path)
    if not target.is_dir() or not os.access(target, os.W_OK):
        raise ValueError(f"boot target must be a writable directory: {target}")
    selectors_seen = args.agents or args.claude or args.cursor
    # No selector means the profile decides. Hardcoding the full target list
    # here rendered the opt-in Cursor rule into every booted workspace.
    targets: tuple[str, ...] | None = None
    if selectors_seen:
        selected = ["agents"]
        if args.claude:
            selected.append("claude")
        if args.cursor:
            selected.append("cursor")
        targets = tuple(selected)
    result = context.lifecycle.apply_workspace(
        target,
        profile=args.profile,
        targets=targets,
        register=True,
    )
    print_workspace_report(result)
    return 1 if result.status in {"conflict", "failed"} else 0


def _handle_workspaces(context: CommandContext) -> int:
    args = context.args
    if args.remove:
        print_workspace_removed(context.lifecycle.remove_workspace(caller_path(args.remove)))
    elif args.paths0:
        for record in context.lifecycle.list_workspaces():
            sys.stdout.write(f"{record.path}\0")
    else:
        print_workspace_list(context.lifecycle.list_workspaces())
    return 0


def _handle_resync(context: CommandContext) -> int:
    args = context.args
    if args.yes and args.dry_run:
        raise ValueError("resync cannot use --yes and --dry-run together")
    if args.all and args.paths:
        raise ValueError("resync cannot combine --all with explicit PATH values")
    if not args.all and not args.paths:
        raise ValueError("resync requires --all or at least one PATH")
    report = context.lifecycle.resync_workspaces(
        apply=bool(args.yes),
        paths=() if args.all else tuple(caller_path(path) for path in args.paths),
    )
    print_workspace_resync_report(report)
    return 1 if any(item.status in {"conflict", "failed"} for item in report.results) else 0


def _handle_install(context: CommandContext) -> int:
    selected = getattr(context.args, "components", None)
    components: tuple[str, ...] | None = None
    if selected:
        wanted = tuple(part.strip() for part in selected.split(",") if part.strip())
        # The class attribute, not the instance: which components exist is a
        # fact about Lifecycle, and reading it off the object would make
        # validation depend on having built one.
        known_components = Lifecycle.SELECTABLE_COMPONENTS
        unknown = [c for c in wanted if c not in known_components]
        if unknown:
            known = ", ".join(known_components)
            raise SystemExit(f"unknown component(s): {', '.join(unknown)} (known: {known})")
        components = wanted
    return run_agentbot_install(context.lifecycle, context.paths, components=components)


def _handle_skills(context: CommandContext) -> int:
    if context.args.skills_command == "prune":
        return handle_skills_prune(
            context.lifecycle,
            names=tuple(context.args.prune_names),
            apply=bool(getattr(context.args, "confirm", False)),
            include_manual=bool(getattr(context.args, "include_manual", False)),
            candidates0=bool(getattr(context.args, "candidates0", False)),
        )
    if context.args.skills_command == "remove-manual":
        return handle_skills_remove_manual(
            context.lifecycle,
            names=tuple(context.args.manual_names),
            apply=bool(context.args.confirm),
            names0=bool(context.args.names0),
        )
    return handle_skills_command(context.lifecycle, context.args.skills_command)


COMMAND_HANDLERS: dict[str, Callable[[CommandContext], int]] = {
    "status": _handle_status,
    "global": _handle_global,
    "doctor": _handle_doctor,
    "graphify": _handle_graphify,
    "boost": _handle_boost,
    "cli-config": _handle_cli_config,
    "cursor": _handle_cursor,
    "vscode": _handle_vscode,
    "update": _handle_update,
    "upgrade": _handle_update,
    "workspace": _handle_workspace,
    "boot": _handle_boot,
    "workspaces": _handle_workspaces,
    "resync": _handle_resync,
    "install": _handle_install,
    "skills": _handle_skills,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentbot")
    parser.add_argument(
        "--root",
        dest="root",
        default=os.environ.get("AGENTBOT_HOME", str(Path(__file__).resolve().parents[1])),
    )
    subparsers = parser.add_subparsers(dest="command")

    help_parser = subparsers.add_parser("help", help="Show the command reference")
    help_parser.add_argument("help_topic", nargs="*", metavar="COMMAND")
    help_parser.add_argument(
        "--format",
        # `menu` was the tab-separated dump the Command Lib used to parse into
        # Bash arrays before src/ui/menus.py built the menu directly. Nothing
        # has called for it since; it outlived its only caller.
        choices=("plain", "tui"),
        default="plain",
        dest="help_format",
        help=argparse.SUPPRESS,
    )

    install_parser = subparsers.add_parser(
        "install", help="Install Agentbot into this machine's agent homes"
    )
    install_parser.add_argument(
        "--components",
        default="",
        help="Comma-separated subset to set up: skills, graphify, boost. "
        "Omit for all of them.",
    )
    status_parser = subparsers.add_parser("status", help="Show skills and global render status")
    status_output = status_parser.add_mutually_exclusive_group()
    status_output.add_argument("--json", action="store_true", dest="status_json")
    status_output.add_argument(
        "--doctor",
        action="store_true",
        dest="status_doctor",
        help="Show status and Doctor issues from one diagnostics snapshot",
    )
    subparsers.add_parser("global", help="Render global outputs")
    subparsers.add_parser("doctor", help="Validate skills and global baseline")
    graphify = subparsers.add_parser("graphify", help="Inspect or set up Graphify integration")
    graphify_sub = graphify.add_subparsers(dest="graphify_command")
    graphify_sub.add_parser("status", help="Show Graphify CLI and skill state")
    graphify_sub.add_parser("setup", help="Install or refresh the generic Agent Skills copy")
    boost = subparsers.add_parser("boost", help="Inspect or manage Boost shell integration")
    boost_sub = boost.add_subparsers(dest="boost_command")
    boost_sub.add_parser("status", help="Show Boost CLI, safety, and integration state")
    boost_sub.add_parser("setup", help="Set up Boost for installed Claude, Codex, and Cursor CLIs")
    boost_sub.add_parser("off", help="Remove Boost integration from Claude, Codex, and Cursor")
    cli_config = subparsers.add_parser("cli-config", help="Manage agent CLI configuration")
    cli_config_sub = cli_config.add_subparsers(dest="cli_config_command")
    cli_config_sub.add_parser("status", help="Preview CLI config changes without writing")
    cli_config_sub.add_parser("apply", help="Merge declared keys into each CLI config")
    cursor = subparsers.add_parser("cursor", help="Inspect or install the Cursor statusline")
    cursor_sub = cursor.add_subparsers(dest="cursor_command")
    cursor_sub.add_parser("status", help="Report the Cursor statusline state")
    cursor_sub.add_parser("statusline", help="Install the managed Cursor statusline")
    vscode = subparsers.add_parser("vscode", help="Manage VS Code extensions and settings")
    vscode_sub = vscode.add_subparsers(dest="vscode_command")
    vscode_sub.add_parser("status", help="Preview VS Code changes without writing")
    vscode_sub.add_parser("seed", help="Record installed extensions into vscode.yaml")
    vscode_sub.add_parser("apply", help="Install extensions and merge owned settings")
    for command in ("update", "upgrade"):
        update = subparsers.add_parser(
            command,
            help="Refresh upstream skills and managed workspace/global outputs",
        )
        update.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview reconciliation and managed-surface changes without writing",
        )
        update.add_argument(
            "--yes",
            dest="confirm",
            action="store_true",
            help="Pre-approve source-owned skill and manifest changes",
        )
        update.add_argument(
            "--interactive",
            action="store_true",
            help="Preview, confirm, and apply one update plan in this process",
        )

    workspace = subparsers.add_parser("workspace", help="Preview or render one workspace")
    workspace.add_argument("--profile", help="Workspace profile name")
    workspace.add_argument(
        "--targets",
        help="Comma-separated outputs: agents,claude,cursor (codex aliases agents)",
    )
    workspace.add_argument("--yes", action="store_true", help="Apply and register the render")
    workspace.add_argument("path", help="Workspace directory")

    boot = subparsers.add_parser("boot", help="Render and register one workspace")
    boot.add_argument("--profile", help="Workspace profile name")
    boot.add_argument(
        "--agents",
        "--codex",
        action="store_true",
        dest="agents",
        help="Select only canonical AGENTS.md unless combined with another selector",
    )
    boot.add_argument(
        "--claude",
        action="store_true",
        help="Include generated Claude output (overrides the profile defaults)",
    )
    boot.add_argument(
        "--cursor",
        action="store_true",
        help="Include generated Cursor rules (overrides the profile defaults)",
    )
    boot.add_argument("path", nargs="?", default=".", help="Workspace directory")

    workspaces = subparsers.add_parser(
        "workspaces", help="List or forget locally registered workspaces"
    )
    workspaces_action = workspaces.add_mutually_exclusive_group()
    workspaces_action.add_argument(
        "--paths0",
        action="store_true",
        help="Print canonical recorded paths separated by NUL bytes",
    )
    workspaces_action.add_argument(
        "--remove",
        metavar="PATH",
        help="Stop managing one recorded workspace without changing its files",
    )

    resync = subparsers.add_parser("resync", help="Preview or refresh registered workspaces")
    resync_group = resync.add_mutually_exclusive_group()
    resync_group.add_argument("--yes", action="store_true", help="Apply Agentbot-managed changes")
    resync_group.add_argument("--dry-run", action="store_true", help="Preview without writing")
    resync.add_argument(
        "--all", action="store_true", help="Include all enabled registered workspaces"
    )
    resync.add_argument("paths", nargs="*", help="Explicit registered workspace paths")

    skills = subparsers.add_parser("skills", help="Install and manage curated skills")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_sub.add_parser("install", help="Install skills from skills.sources.yaml")
    skills_sub.add_parser(
        "update",
        help="Refresh globally installed skills from ~/.agents/.skill-lock.json",
    )
    skills_sub.add_parser(
        "upgrade",
        help="Alias for updating globally installed skills",
    )
    skills_sub.add_parser("list", help="List installed skills under ~/.agents/skills")
    skills_sub.add_parser("doctor", help="Validate skills sources and tooling")
    prune = skills_sub.add_parser(
        "prune",
        help="Remove installed skills that no active manifest source wants",
    )
    prune.add_argument(
        "prune_names",
        nargs="*",
        metavar="SKILL",
        help="Exact prune candidate names to preview or remove",
    )
    prune.add_argument(
        "--yes",
        dest="confirm",
        action="store_true",
        help="Apply the removals; without it this only reports",
    )
    prune.add_argument(
        "--include-manual",
        action="store_true",
        help="Also remove user-placed skills that have no lock entry",
    )
    prune.add_argument(
        "--candidates0",
        action="store_true",
        help="Print candidate name, reason, and detail as NUL-separated fields",
    )
    remove_manual = skills_sub.add_parser(
        "remove-manual",
        help="Selectively remove user-placed skills that have no lock entry",
    )
    remove_manual.add_argument(
        "manual_names",
        nargs="*",
        metavar="SKILL",
        help="Exact manual skill names to preview or remove",
    )
    remove_manual.add_argument(
        "--yes",
        dest="confirm",
        action="store_true",
        help="Remove the selected skills; without it this only previews",
    )
    remove_manual.add_argument(
        "--names0",
        action="store_true",
        help="Print removable manual skill names separated by NUL bytes",
    )

    return parser


def _archived_command_error(command: str) -> int:
    print(
        f"  Error: '{command}' is archived. See archive/docs/README.md for catalog, "
        "MCP, and interactive control-plane features.",
        file=sys.stderr,
    )
    return 1


def _update_tty_default_path() -> str:
    """Return the one fallback terminal path for interactive update prompts."""
    return "/dev/tty"


def _open_update_tty_stream(*, descriptor_name: str, path_name: str, mode: str):
    descriptor = os.environ.get(descriptor_name)
    if descriptor:
        return os.fdopen(os.dup(int(descriptor)), mode, encoding="utf-8")
    path = os.environ.get(path_name, _update_tty_default_path())
    return open(path, mode, encoding="utf-8")


def confirm_update_plan() -> bool:
    with ExitStack() as streams:
        output_stream = streams.enter_context(
            _open_update_tty_stream(
                descriptor_name="AGENTBOT_UPDATE_TTY_OUT_FD",
                path_name="AGENTBOT_UPDATE_TTY_OUTPUT",
                mode="a",
            )
        )
        output_stream.write("\nApply this Agentbot update plan? [y/N] ")
        output_stream.flush()
        input_stream = streams.enter_context(
            _open_update_tty_stream(
                descriptor_name="AGENTBOT_UPDATE_TTY_IN_FD",
                path_name="AGENTBOT_UPDATE_TTY_INPUT",
                mode="r",
            )
        )
        answer = input_stream.readline().strip()
        return answer.lower() in {"y", "yes"}


def parse_workspace_targets(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    raw_targets = tuple(part.strip() for part in value.split(",") if part.strip())
    if not raw_targets:
        raise ValueError("--targets must name at least one target")
    targets: list[str] = []
    for raw_target in raw_targets:
        target = "agents" if raw_target == "codex" else raw_target
        if target not in WORKSPACE_TARGETS:
            raise ValueError(f"unsupported workspace target: {raw_target}")
        if target in targets:
            raise ValueError(f"--targets contains duplicates: {target}")
        targets.append(target)
    return ("agents", *(target for target in targets if target != "agents"))


def handle_skills_prune(
    lifecycle: Lifecycle,
    *,
    names: tuple[str, ...] = (),
    apply: bool,
    include_manual: bool,
    candidates0: bool = False,
) -> int:
    from .skill_prune import PruneReport, apply_prune, plan_prune
    from .skills_sources import load_skills_sources

    if candidates0 and (names or apply or include_manual):
        raise ValueError(
            "--candidates0 cannot be combined with skill names, --yes, or --include-manual"
        )
    if include_manual and names:
        raise ValueError("--include-manual cannot be combined with exact skill names")
    if len(set(names)) != len(names):
        raise ValueError("prune candidate names must not contain duplicates")

    paths = lifecycle.paths
    config = load_skills_sources(paths.skills_sources_file)
    report = plan_prune(paths, config)
    if report.blocked_reason is not None and (candidates0 or names or apply):
        raise ValueError(report.blocked_reason)
    candidates_by_name = {item.name: item for item in report.candidates}
    invalid = sorted(set(names) - set(candidates_by_name))
    if invalid:
        raise ValueError(f"not prune candidates: {', '.join(invalid)}")

    if candidates0:
        for item in report.candidates:
            for field in (item.name, item.reason, item.detail):
                sys.stdout.write(f"{field}\0")
        return 0

    selected_report = report
    if names:
        selected_report = PruneReport(
            candidates=tuple(candidates_by_name[name] for name in names)
        )
    if apply:
        selected_report = apply_prune(
            paths,
            report,
            include_manual=include_manual,
            candidate_names=names or None,
        )
    return print_skill_prune_report(selected_report, include_manual=include_manual)


def handle_skills_remove_manual(
    lifecycle: Lifecycle,
    *,
    names: tuple[str, ...],
    apply: bool,
    names0: bool,
) -> int:
    from .skill_prune import PruneReport, apply_prune, plan_prune
    from .skills_sources import load_skills_sources

    if names0 and (names or apply):
        raise ValueError("--names0 cannot be combined with skill names or --yes")
    if apply and not names:
        raise ValueError("manual skill removal requires at least one manual skill name")
    if len(set(names)) != len(names):
        raise ValueError("manual skill names must not contain duplicates")

    paths = lifecycle.paths
    config = load_skills_sources(paths.skills_sources_file)
    report = plan_prune(paths, config)
    if report.blocked_reason is not None:
        raise ValueError(report.blocked_reason)
    manual_by_name = {item.name: item for item in report.manual}

    if names0:
        for name in manual_by_name:
            sys.stdout.write(f"{name}\0")
        return 0

    invalid = sorted(set(names) - set(manual_by_name))
    if invalid:
        raise ValueError(f"not removable manual skills: {', '.join(invalid)}")
    selected = tuple(manual_by_name[name] for name in names) if names else report.manual
    selected_report = PruneReport(candidates=selected)
    if apply:
        selected_report = apply_prune(paths, selected_report, manual_names=names)
    return print_manual_skill_removal_report(selected_report)


def handle_skills_command(lifecycle: Lifecycle, skills_command: str) -> int:
    if skills_command == "install":
        results = lifecycle.install_skills()
        outputs = lifecycle.refresh_outputs()
        install_rc = print_skills_report(results, title="Skills install")
        print_output_refresh_report(
            linked=outputs.claude_linked,
            updated=outputs.claude_updated,
            skipped=outputs.claude_skipped,
        )
        return install_rc
    if skills_command in {"update", "upgrade"}:
        title = skills_command.capitalize()
        result = lifecycle.update_skills()
        update_report = parse_update_output(result.stdout, result.stderr)
        outputs = lifecycle.refresh_outputs()
        print_header(f"Skills {skills_command}", f"Agentbot › Skills {title}")
        return print_skills_update_report(
            linked=outputs.claude_linked,
            skipped=outputs.claude_skipped,
            updated=outputs.claude_updated,
            updated_skills=update_report.updated_skills,
            upstream_deleted_skills=update_report.deleted_skills,
        )
    if skills_command == "list":
        skills = lifecycle.list_skills()
        if not skills:
            print_header("Installed Skills", "Agentbot › Installed Skills")
            print("  No installed skills found.")
            return 0
        print_header("Installed Skills", "Agentbot › Installed Skills")
        for skill in skills:
            print(f"  {skill}")
        return 0
    if skills_command == "doctor":
        return print_skills_doctor(lifecycle.diagnostics)
    raise SystemExit(f"unknown skills command: {skills_command}")


def print_skills_error(error: Exception) -> int:
    print(f"  Error: {error}", file=sys.stderr)
    return 1


def run_agentbot_install(
    lifecycle: Lifecycle, paths: AgentbotPaths, *, components: tuple[str, ...] | None = None
) -> int:
    print_header("Install Agentbot", "Agentbot › Install Agentbot")
    if components is not None:
        skipped = [c for c in lifecycle.SELECTABLE_COMPONENTS if c not in components]
        print(f"  Selected: {', '.join(components) or 'nothing'}")
        if skipped:
            print(f"  Skipping: {', '.join(skipped)}")
        # The selection is about what was asked for; the legend is about the run
        # that follows. A blank keeps them from reading as one block.
        print()
    # The key to the prefixes below, as the sibling product prints under its own
    # install heading. Without it [STEP] and [OK] are markers the reader is left
    # to infer.
    log_legend()
    print()
    # Emitted whether or not anyone is watching live. These are plain [STEP]
    # and [OK] lines with no cursor control, so a pipe degrades cleanly rather
    # than losing them -- and the piped case is `dotfiles full-update`, which is
    # where the silence this progress exists to fix was longest.
    outcome = lifecycle.install(
        progress=InstallLog().stage,
        components=components,
    )
    print()
    skills_rc = print_skills_report(list(outcome.skills), title="Skills install")
    print_output_refresh_report(
        linked=outcome.outputs.claude_linked,
        updated=outcome.outputs.claude_updated,
        skipped=outcome.outputs.claude_skipped,
    )
    if outcome.graphify.cli_path is not None or outcome.graphify.state == "broken":
        print_graphify_status(outcome.graphify)
    if outcome.boost.cli_path is not None or outcome.boost.state == "broken":
        print_boost_status(outcome.boost)
    doctor_rc = print_doctor_summary(list(outcome.diagnostics.issues))
    print(f"  AGENTBOT_HOME={paths.root.resolve()}")
    if skills_rc != 0:
        return skills_rc
    if outcome.graphify.state == "broken":
        return 1
    if outcome.boost.state in {"broken", "forbidden", "unsafe-config"}:
        return 1
    return doctor_rc


def print_skills_doctor(diagnostics: Diagnostics) -> int:
    issues = diagnostics.skills_doctor_issues()
    print_header("Skills Doctor", "Agentbot › Skills Doctor")
    if not issues:
        print("  No issues found.")
        return 0
    print(f"  Found {len(issues)} issue(s):")
    errors = 0
    for issue in issues:
        if issue.level.lower() == "error":
            errors += 1
        print(f"  - [{issue.level.upper()}] {issue.scope}: {issue.message}")
    return 1 if errors else 0


def print_status(diagnostics: Diagnostics, *, include_issues: bool = False) -> int:
    snapshot = diagnostics.collect()
    print_status_summary(
        installed_skills=len(snapshot.installed_skills),
        global_agents_exists=snapshot.global_agents_exists,
        skills_sources_exists=snapshot.skills_sources_exists,
        enabled_sources=snapshot.enabled_sources,
        global_lock_exists=snapshot.global_lock_exists,
        global_lock_skills=snapshot.global_lock_skills,
        claude_bridge_links=snapshot.claude_bridge_links,
        claude_statusline_state=snapshot.claude_statusline_state,
        manual_skill_count=snapshot.manual_skill_count,
        doctor_issue_count=len(snapshot.issues),
        # The checkout Diagnostics was built against, so status reports on the
        # repository it actually inspected rather than the process's cwd.
        repo_root=getattr(getattr(diagnostics, 'paths', None), 'root', None),
    )
    if include_issues:
        return print_doctor_summary(list(snapshot.issues), include_header=False)
    return 0


def print_status_json(diagnostics: Diagnostics) -> None:
    snapshot = diagnostics.collect()
    payload = asdict(snapshot)
    payload["installed_skills"] = len(snapshot.installed_skills)
    payload["doctor_issue_count"] = len(snapshot.issues)
    payload.pop("issues")
    print(json.dumps(payload, indent=2, sort_keys=True))


def print_doctor(diagnostics: Diagnostics) -> int:
    return print_doctor_summary(list(diagnostics.collect().issues))


def print_help_command(topic: str | None, *, output_format: str = "plain") -> int:
    spec: CommandSpec | None
    try:
        spec = command_by_name(topic) if topic else None
    except KeyError:
        print(f"  Error: unknown help topic: {topic}", file=sys.stderr)
        return 2
    print_command_help(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
