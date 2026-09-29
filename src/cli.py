from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .boost import BoostIntegration
from .commands import CommandSpec, command_by_name
from .diagnostics import Diagnostics
from .graphify import GraphifyIntegration
from .lifecycle import Lifecycle
from .memory import WARNING_RULES
from .paths import AgentbotPaths, default_paths
from .skills_installer import (
    SkillsInstallError,
    apply_skill_update,
    parse_update_output,
    plan_skill_update,
    restore_skills,
)
from .ui import (
    format_shortcuts,
    print_boost_status,
    print_command_help,
    print_doctor_summary,
    print_four_column_table,
    print_graphify_status,
    print_header,
    print_install_closing_line,
    print_install_summary,
    print_manual_skill_removal_report,
    print_mcp_catalog,
    print_mcp_plan,
    print_mcp_status,
    print_memory_backup,
    print_memory_due,
    print_memory_hooks,
    print_memory_project,
    print_memory_project_write,
    print_memory_record,
    print_memory_restore,
    print_memory_result,
    print_memory_search,
    print_memory_setup,
    print_memory_status,
    print_memory_sync,
    print_memory_validation,
    print_output_refresh_report,
    print_rollup,
    print_skill_prune_report,
    print_skills_report,
    print_skills_update_report,
    print_status_summary,
    print_table,
    print_update_plan,
    print_update_result,
    print_vscode_report,
    print_workspace_list,
    print_workspace_removed,
    print_workspace_report,
    print_workspace_resync_report,
    skills_report_status,
)
from .ui.install_log import InstallLog, log_legend, print_timing, stop_progress_animation
from .ui.table import CollapseBlankLines
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


def configure_output(stream: object) -> CollapseBlankLines:
    """Line-buffered, and never two blank lines in a row.

    Line buffering because the interactive update asks its question on the
    terminal while its output goes through a pipe. The menu runs this backend
    under tui_run_to_output, which pipes stdout through a Bash colouring loop,
    and Python block-buffers a pipe -- so the plan table sat in an unflushed
    buffer until the process exited while `Apply this Agentbot update plan?
    [y/N]` went straight to the terminal on its own descriptor. The operator
    was asked to approve a plan that had not been printed yet.

    This is also why the progress lines carry flush=True. They no longer need
    it, and keep it: an explicit flush on a progress line is not wrong, and
    removing them would be a change with nothing behind it.
    """
    # Guarded rather than assumed: sys.stdout is typed TextIO, reconfigure
    # belongs to the TextIOWrapper it usually is, and a caller may have
    # replaced it with something else entirely.
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(line_buffering=True)
    return CollapseBlankLines(stream)


def main() -> int:
    if _requires_raw_stdout(tuple(sys.argv[1:])):
        return _run()
    original = sys.stdout
    sys.stdout = configure_output(original)
    try:
        return _run()
    finally:
        sys.stdout.flush()
        sys.stdout = original


def _requires_raw_stdout(argv: tuple[str, ...]) -> bool:
    return any(argv[index : index + 2] == ("mcp", "serve") for index in range(len(argv) - 1))


def _run() -> int:
    parser = build_parser()
    args = parser.parse_args()

    command = args.command or "install"
    if command == "help":
        help_topic = " ".join(getattr(args, "help_topic", ())) or None
        return print_help_command(help_topic)

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
    from . import codex_remote_control
    from .command_runner import CommandRunner

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
        elif report.applied:
            summary = f"{len(plan.additions)} added, {len(plan.changes)} changed"
            rows.append((name, f"{summary} in {plan.path}", "applied"))
        else:
            summary = f"{len(plan.additions)} to add, {len(plan.changes)} to change"
            rows.append((name, f"{summary} in {plan.path}", "check"))
    runner = CommandRunner()
    remote_detail, remote_result = (
        codex_remote_control.ensure(context.paths, runner)
        if command == "apply"
        else codex_remote_control.inspect(context.paths, runner)
    )
    rows.append(("codex remote control", remote_detail, remote_result))
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
    ok, check, miss = print_table([("Statusline", f"{state.state}: {state.detail}", state.result)])
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


def _print_memory_state(status, *, as_json: bool) -> int:
    from . import memory

    if as_json:
        print(json.dumps(memory.status_json(status), indent=2))
    else:
        print_memory_status(status)
    # Dirty or invalid are reported states, not a failure to report them.
    return {"unconfigured": 2, "broken": 1}.get(status.state, 0)


def _confirm_by_hand(code: str, action: str, command: str) -> None:
    """A human-only action needs a person at a terminal typing its short code.

    Agent shells run commands without a terminal, so an agent cannot confirm
    even when asked to: in Ticket 12 run 4, Codex ran `approve --yes` itself
    after the user said "approve it now". This is a deterrent, not a security
    boundary; an agent driving a real terminal on purpose is not stopped.
    """
    if not sys.stdin.isatty():
        raise ValueError(
            f"{action} is human-only and needs a terminal. Run it yourself in your own "
            f"terminal: {command}"
        )
    print(f"  Type {code} to {action}: ", end="", file=sys.stderr, flush=True)
    if input().strip() != code:
        raise ValueError(f"{action} cancelled: the code did not match")


def _handle_memory_hook(context: CommandContext) -> int:
    from . import memory, memory_hook

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    try:
        action = args.hook_action
        if action == "status" or not args.confirm:
            state = memory_hook.inspect(root, context.paths.root)
        elif action == "install":
            memory.read_marker(root)
            state = memory_hook.install(root, context.paths.root)
        else:
            state = memory_hook.remove(root, context.paths.root)
        if as_json:
            print(json.dumps(memory_hook.hook_json(state), indent=2))
        else:
            print_memory_hooks(state, action=action, applied=args.confirm)
        return 1 if state.problem or "unowned" in state.states.values() else 0
    except memory.MemoryVaultError as error:
        return _print_memory_state(
            memory.VaultStatus(state="broken", looked_in=looked, root=root, problem=str(error)),
            as_json=as_json,
        )


def _handle_memory_due(context: CommandContext) -> int:
    from . import memory

    as_json = bool(getattr(context.args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    today = memory.utc_today()
    try:
        items, total, schema = memory.due_queue(root, today=today, limit=context.args.limit)
    except memory.MemoryVaultError as error:
        return _print_memory_state(
            memory.VaultStatus(state="broken", looked_in=looked, root=root, problem=str(error)),
            as_json=as_json,
        )
    if as_json:
        print(json.dumps(memory.due_json(items, total, schema, today), indent=2))
    else:
        print_memory_due(items, total=total, today=today)
    return 0


def _handle_memory_retrieve(context: CommandContext) -> int:
    from . import memory
    from . import memory_retrieve as retrieve

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    project, source = args.project, "explicit" if args.project else None
    if project is None and not args.cross_project and not getattr(args, "no_auto_project", False):
        from . import memory_projects

        # cwd -> Git top level -> origin -> registry. Unresolved, colliding, or
        # outside a repository: global and shared only, never a guess.
        resolution = memory_projects.resolve(root, caller_path("."), context.paths.config_home)
        if resolution.state == "resolved" and resolution.entry is not None:
            project, source = resolution.entry["folder"], "auto"
    scope = {"project": project, "project_source": source}
    request = retrieve.Request(
        project=project,
        cross_project=args.cross_project,
        history=bool(getattr(args, "history", False)),
        kind=getattr(args, "memory_type", None),
        tag=getattr(args, "tag", None),
    )
    try:
        if args.memory_command == "search":
            default = retrieve.SEARCH_DEFAULT if args.query.strip() else retrieve.INDEX_DEFAULT
            result = retrieve.search(root, args.query, request, limit=args.limit or default)
            if as_json:
                print(json.dumps({**retrieve.search_json(result), **scope}, indent=2))
            else:
                print_memory_search(result)
        elif args.memory_command == "show":
            hit, body = retrieve.show(root, args.record_path, request, max_bytes=args.max_bytes)
            if as_json:
                print(json.dumps({**retrieve.show_json(hit, body), **scope}, indent=2))
            else:
                print_memory_record(hit, body)
        else:
            summary = retrieve.brief(root, request, tokens=args.tokens)
            if as_json:
                print(json.dumps({**retrieve.brief_json(summary), **scope}, indent=2))
            else:
                print(summary.text, end="")
    except memory.MemoryRequestError as error:
        # The vault is fine; this request is not. Never report it as broken.
        if as_json:
            print(json.dumps({"state": "refused", "problem": str(error), **scope}, indent=2))
        else:
            print(f"  {error}", file=sys.stderr)
        return 1
    except memory.MemoryVaultError as error:
        return _print_memory_state(
            memory.VaultStatus(state="broken", looked_in=looked, root=root, problem=str(error)),
            as_json=as_json,
        )
    return 0


def _handle_memory_backup(context: CommandContext) -> int:
    from . import memory
    from . import memory_backup as backups

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    try:
        if args.memory_command == "backup":
            if root is None:
                return _print_memory_state(
                    memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
                )
            from . import memory_setup

            recorded = memory_setup.recorded_backup(context.paths.config_home)
            if args.destination:
                destination = caller_path(args.destination)
            elif recorded is not None:
                destination = Path(recorded["destination"])
            else:
                raise ValueError("no backup is recorded yet; name one with --destination PATH")
            assurance = args.assurance or (recorded or {}).get("assurance", "unknown")
            result = backups.backup(root, destination, apply=args.confirm, assurance=assurance)
            if result.state != "preview":
                memory_setup.record_backup(context.paths.config_home, destination, assurance)
            if as_json:
                print(json.dumps(backups.backup_json(result), indent=2))
            else:
                print_memory_backup(result)
            return 0
        restored = backups.restore(
            caller_path(args.source),
            caller_path(args.destination),
            active=root,
            apply=args.confirm,
        )
        if as_json:
            print(json.dumps(backups.restore_json(restored), indent=2))
        else:
            print_memory_restore(restored)
        return 1 if restored.state == "restored-with-findings" else 0
    except memory.MemoryVaultError as error:
        return _print_memory_state(
            memory.VaultStatus(state="broken", looked_in=looked, root=root, problem=str(error)),
            as_json=as_json,
        )


def _add_retrieval_scope(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--project", metavar="SLUG", help="Include this project's records")
    group.add_argument(
        "--no-auto-project",
        action="store_true",
        dest="no_auto_project",
        help="Do not detect the project from the current repository",
    )
    group.add_argument(
        "--cross-project",
        action="store_true",
        dest="cross_project",
        help="Include every project's records, one per project first",
    )
    parser.add_argument("--json", action="store_true", dest="memory_json")


def _handle_memory_setup(context: CommandContext) -> int:
    from . import memory_setup as setup

    args = context.args
    config_home = context.paths.config_home
    if args.setup_path:
        result = setup.setup_path(caller_path(args.setup_path), config_home, apply=args.confirm)
    elif args.setup_clone:
        if not args.dest:
            raise ValueError("--clone needs --dest PATH for the new checkout")
        result = setup.setup_clone(
            args.setup_clone, caller_path(args.dest), config_home, apply=args.confirm
        )
    elif args.setup_new:
        result = setup.setup_new(
            caller_path(args.setup_new), config_home, remote=args.remote, apply=args.confirm
        )
    elif args.setup_remove:
        result = setup.remove(config_home, apply=args.confirm)
    else:
        result = setup.show(config_home)
    if result.state == "configured" and result.vault.get("path"):
        result.notes.insert(0, _install_vault_hooks(context, Path(result.vault["path"])))
    if getattr(args, "memory_json", False):
        print(json.dumps(setup.result_json(result), indent=2))
    else:
        print_memory_setup(result)
    return 0


def _install_vault_hooks(context: CommandContext, vault: Path) -> str:
    """Setup installs the vault's commit and push checks too: one step, not two."""
    from . import memory, memory_hook

    retry = "install them with: agentbot memory hook install --yes"
    try:
        state = memory_hook.install(vault, context.paths.root)
    except memory.MemoryVaultError as error:
        return f"Vault checks not installed ({error}); {retry}"
    if state.problem or "unowned" in state.states.values():
        return f"Vault checks not installed: {state.problem or 'a hook exists that Agentbot does not own'}; {retry}"
    return "Installed the vault's pre-commit and pre-push checks."


def _handle_memory_project(context: CommandContext) -> int:
    from . import memory
    from . import memory_project_writes as writes
    from . import memory_projects as projects
    from . import memory_proposals as proposals

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    cwd = caller_path(".")
    config_home = context.paths.config_home
    action = getattr(args, "project_action", None) or "status"
    if action == "status":
        resolution = projects.resolve(root, cwd, config_home)
        if as_json:
            print(json.dumps(projects.resolution_json(resolution), indent=2))
        else:
            print_memory_project(resolution)
        return 1 if resolution.state == "collision" else 0

    def body() -> str:
        source = caller_path(args.from_file) if args.from_file else None
        return proposals.read_body(source, sys.stdin.buffer)

    result: dict = {}
    if action == "add":
        result = writes.add(
            root,
            cwd,
            config_home,
            kind=args.kind,
            title=args.title,
            body=body(),
            tags=args.tag or [],
            supersedes=args.supersedes,
        )
    elif action == "edit":
        text = body() if (args.stdin or args.from_file) else None
        result = writes.edit(
            root, cwd, config_home, args.path, body=text, title=args.title, tags=args.tag
        )
    elif action == "context":
        result = writes.set_context(root, cwd, config_home, body())
    elif action == "move":
        result = writes.move(root, cwd, config_home, args.path, args.new_path)
    elif action == "delete":
        result = writes.delete(root, cwd, config_home, args.path)
    elif action == "retire":
        result = writes.retire(root, cwd, config_home, args.path)
    elif action == "promote":
        result = writes.promote(root, cwd, config_home, args.path, scope=args.scope)
    elif action == "maintain":
        result = writes.maintain(root, cwd, config_home)
    elif action == "forget":
        if not args.confirm:
            folder = projects.require_project(root, cwd, config_home)["folder"]
            result = {"state": "preview", "path": f"projects/{folder}"}
        else:
            result = writes.forget(root, cwd, config_home)
    elif action == "register":
        result = {"state": "registered", **projects.register(root, cwd, config_home)}
    elif action == "attach":
        result = {"state": "attached", **projects.attach(root, cwd, config_home, args.project)}
    elif action == "link":
        if not args.confirm:
            result = {"state": "preview", "origin": args.origin, "project": args.project}
        else:
            result = {"state": "linked", **projects.link(root, args.origin, args.project)}
    if result.get("state") not in {"preview", "attached", "report"}:
        from . import memory_autosync

        result["sync"] = memory_autosync.after_write(root, config_home)
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print_memory_project_write(result, action=action)
    return 0


def _handle_memory_proposals(context: CommandContext, root: Path) -> int:
    """Tracked core proposals. Approve and reject are the user's."""
    from . import memory, memory_autosync
    from . import memory_proposals as proposals
    from .memory_retrieve import DATA_NOTICE

    memory.read_marker(root)

    args = context.args
    command = args.memory_command
    result: dict[str, Any]
    if command == "propose":
        source = caller_path(args.from_file) if args.from_file else None
        result = proposals.propose(
            root,
            kind=args.memory_type,
            title=args.title,
            body=proposals.read_body(source, sys.stdin.buffer),
            scope=args.scope,
            projects=list(args.project or []),
            tags=list(args.tag or []),
            supersedes=list(args.supersedes or []) or None,
        )
    elif command == "review":
        items = proposals.queue(root)
        as_json = getattr(args, "memory_json", False)
        if not args.draft_path and items and not as_json and _at_a_terminal():
            return _walk_proposals(context, root, items)
        if args.draft_path:
            match = [item for item in items if item["path"] == args.draft_path]
            if not match:
                raise ValueError(f"no open proposal at {args.draft_path}")
            # The text too: reading it is how the user decides to approve.
            result = {
                "state": "open",
                **match[0],
                "notice": DATA_NOTICE,
                "body": proposals.body(root, args.draft_path),
            }
        else:
            result = {
                "state": "queue",
                "open": len(items),
                "notice": DATA_NOTICE,
                "proposals": items,
            }
    else:
        waiting = {item["path"]: item for item in proposals.queue(root)}
        if args.draft_path not in waiting:
            raise ValueError(f"no open proposal at {args.draft_path}")
        if not args.confirm:
            result = {"state": "preview", "notice": DATA_NOTICE, **waiting[args.draft_path]}
        else:
            _confirm_by_hand(
                str(waiting[args.draft_path]["id"])[:8],
                f"{command} {args.draft_path}",
                f"agentbot memory {command} {args.draft_path} --yes",
            )
            if command == "approve":
                result = proposals.approve(root, args.draft_path)
            else:
                result = proposals.reject(root, args.draft_path)
    if result["state"] in {"proposed", "approved", "rejected"}:
        result["sync"] = memory_autosync.after_write(root, context.paths.config_home)
    if getattr(args, "memory_json", False):
        print(json.dumps(result, indent=2))
    else:
        print_memory_result(result, title=command)
    return 0


def _at_a_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_review_choice() -> str:
    """The keys lit the way the menus light theirs, on a line of their own."""
    from .ui.table import BOLD, CYAN, DIM, RESET, use_color

    lit, dim, bold, reset = (CYAN, DIM, BOLD, RESET) if use_color() else ("", "", "", "")
    keys = "   ".join(
        f"{reset}{lit}{key}{reset}{dim} {word}"
        for key, word in (("a", "approve"), ("r", "reject"), ("s", "skip"), ("q", "stop"))
    )
    print(f"\n  {dim}{keys}{reset}", file=sys.stderr)
    print(f"  {bold}Choice:{reset} ", end="", file=sys.stderr, flush=True)
    return input().strip().lower()


def _walk_proposals(context: CommandContext, root: Path, items: list[dict[str, Any]]) -> int:
    """Each open proposal in turn: its text, then approve, reject, skip or stop.

    A person at a terminal reviews the queue here without copying any path.
    Approve and reject still ask for the short code, as their own commands do.
    """
    from . import memory_autosync
    from . import memory_proposals as proposals
    from .memory_retrieve import DATA_NOTICE

    rc = 0
    for number, item in enumerate(items, 1):
        path = item["path"]
        print_memory_result(
            {"state": "open", **item, "notice": DATA_NOTICE, "body": proposals.body(root, path)},
            title=f"review {number} of {len(items)}",
        )
        choice = _ask_review_choice()
        if choice == "q":
            print("  Stopped; the rest are left for later.")
            break
        if choice not in {"a", "r"}:
            print("  Left for later.")
            continue
        command = "approve" if choice == "a" else "reject"
        print(file=sys.stderr)
        try:
            _confirm_by_hand(
                str(item["id"])[:8], f"{command} {path}", f"agentbot memory {command} {path} --yes"
            )
        except ValueError as error:
            print(f"  {error}; left for later.")
            rc = 1
            continue
        result = (
            proposals.approve(root, path) if command == "approve" else proposals.reject(root, path)
        )
        result["sync"] = memory_autosync.after_write(root, context.paths.config_home)
        print_memory_result(result, title=command)
    return rc


# Retrieval refreshes first. Not validate: hooks run it mid-commit and mid-push.
READ_COMMANDS = frozenset({"status", "search", "show", "brief", "due", "project"})


def _refresh_before_read(context: CommandContext) -> None:
    """Fetch the shared vault first, at most once per interval; never block the read."""
    from . import memory, memory_autosync

    if context.args.memory_command == "project" and getattr(context.args, "project_action", None):
        return
    try:
        root, _ = memory.find_vault(context.paths.root)
        if root is not None:
            memory_autosync.before_read(root, context.paths.config_home)
    except memory.MemoryVaultError:
        return


def _handle_memory_sync(context: CommandContext) -> int:
    from . import memory, memory_autosync

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    config_home = context.paths.config_home
    payload: Any
    if args.memory_command == "sync":
        if args.mode or args.interval is not None:
            memory_autosync.set_settings(config_home, mode=args.mode, fetch_interval=args.interval)
            payload = memory_autosync.status(root, config_home)
            title = "settings"
        elif args.status_only:
            payload = memory_autosync.status(root, config_home)
            title = "status"
        else:
            payload = {
                **memory_autosync.run(root),
                "settings": memory_autosync.status(root, config_home),
            }
            title = "run"
        code = 0 if payload.get("state", "synced") in {"synced", "offline"} or title != "run" else 1
    else:
        action = args.conflict_action or "list"
        if action == "list":
            payload = {"conflicts": memory_autosync.open_conflicts(root)}
        elif action == "show":
            payload = memory_autosync.show(root, args.op)
        else:
            conflict = next(
                (c for c in memory_autosync.open_conflicts(root) if c["op"] == args.op), None
            )
            if conflict is not None and conflict.get("resolver") == "human":
                _confirm_by_hand(
                    args.op[:8],
                    f"resolve core conflict {args.op} keeping {args.keep}",
                    f"agentbot memory conflict resolve {args.op} --keep {args.keep}",
                )
            payload = memory_autosync.resolve(
                root, config_home, args.op, args.keep, cwd=caller_path(".")
            )
        title = f"conflict {action}"
        code = 0
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print_memory_sync(payload, title=title)
    return code


def _handle_memory(context: CommandContext) -> int:
    from . import memory

    as_json = bool(getattr(context.args, "memory_json", False))
    try:
        return _dispatch_memory(context)
    except memory.MemoryVaultError as error:
        if _vault_usable(context):
            # A sound vault refused this one request (not a registered project,
            # a write that would break the vault, ...): say so as this command,
            # not as a broken-vault status screen.
            result = {"state": "refused", "detail": str(error)}
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                print_memory_result(result, title=context.args.memory_command)
            return 1
        # One place for a configured vault that is missing or not the one
        # recorded: every memory command reports it the same way.
        return _print_memory_state(
            memory.VaultStatus(state="broken", looked_in=(), problem=str(error)),
            as_json=as_json,
        )


def _vault_usable(context: CommandContext) -> bool:
    from . import memory

    try:
        root, _ = memory.find_vault(context.paths.root)
        if root is None:
            return False
        memory.read_marker(root)
    except memory.MemoryVaultError:
        return False
    return True


def _dispatch_memory(context: CommandContext) -> int:
    from . import memory

    if context.args.memory_command == "setup":
        return _handle_memory_setup(context)
    if context.args.memory_command in {"sync", "conflict"}:
        return _handle_memory_sync(context)
    if context.args.memory_command in {"propose", "review", "approve", "reject"}:
        root, looked = memory.find_vault(context.paths.root)
        if root is None:
            return _print_memory_state(
                memory.VaultStatus(state="unconfigured", looked_in=looked),
                as_json=bool(getattr(context.args, "memory_json", False)),
            )
        return _handle_memory_proposals(context, root)
    if context.args.memory_command in READ_COMMANDS:
        _refresh_before_read(context)
    if context.args.memory_command == "project":
        return _handle_memory_project(context)
    if context.args.memory_command == "hook":
        return _handle_memory_hook(context)
    if context.args.memory_command == "due":
        return _handle_memory_due(context)
    if context.args.memory_command in {"search", "show", "brief"}:
        return _handle_memory_retrieve(context)
    if context.args.memory_command in {"backup", "restore"}:
        return _handle_memory_backup(context)
    as_json = bool(getattr(context.args, "memory_json", False))
    if context.args.memory_command == "status":
        status = memory.status(context.paths.root)
    else:
        root, looked = memory.find_vault(context.paths.root)
        if root is None:
            status = memory.VaultStatus(state="unconfigured", looked_in=looked)
        else:
            try:
                report = memory.validate(root, acknowledge=context.args.acknowledge_warning or ())
            except memory.MemoryVaultError as error:
                status = memory.VaultStatus(
                    state="broken", looked_in=looked, root=root, problem=str(error)
                )
            else:
                if as_json:
                    print(json.dumps(memory.report_json(report), indent=2))
                else:
                    print_memory_validation(report)
                return 0 if report.valid else 1
    return _print_memory_state(status, as_json=as_json)


def _gitlab_token_verify_only(store: ModuleType) -> int:
    """Check a candidate token without storing it.

    The menu calls this so the verdict is in front of the operator before it
    asks whether to save, which is the order the GitHub screen has always used.
    Same stdin discipline and the same shape rule as `set`, so a token this
    accepts is not one `set` then rejects for a different reason.

        0  accepted, and not proven able to write
        1  refused, or the wrong shape
        2  could not ask
        3  accepted, but carries a scope that can write
    """
    token = sys.stdin.readline().strip()
    if not token:
        print("  No token was supplied.", file=sys.stderr)
        return 1
    if not store.is_valid(token):
        print(f"  token value is invalid: {store.SHAPE_RULE}", file=sys.stderr)
        return 1
    print(f"  Proposed: {store.fingerprint(token)}")
    result = store.verify(token)
    if result.accepted is False:
        print(f"  {result.detail}", file=sys.stderr)
        return 1
    if result.accepted is None:
        print(f"  Could not check it: {result.detail}")
        return 2
    if result.write_capable:
        print(
            f"  This token can write ({', '.join(result.scopes)}); read_api is the contract.",
            file=sys.stderr,
        )
        return 3
    scopes = ", ".join(result.scopes) if result.scopes else "not published"
    print(f"  GitLab {result.detail}; scopes: {scopes}")
    return 0


def _handle_gitlab_token(context: CommandContext) -> int:
    from . import gitlab_token as store

    command = context.args.gitlab_token_command
    config_home = context.paths.config_home
    saved = store.read(config_home)

    if command == "status":
        problem = store.inspect(config_home)
        if problem is not None:
            print(f"  Saved state is unusable: {problem}")
            return 1
        if not saved:
            print("  not configured")
            return 1
        print(f"  {store.fingerprint(saved)}")
        return 0

    if command == "verify":
        return _gitlab_token_verify_only(store)

    if command == "set":
        # One line, from stdin. Whitespace is stripped because a paste through
        # a terminal carries a newline and sometimes a trailing space, and a
        # token that differs only by those is refused by the shape check with
        # nothing to show the operator why.
        token = sys.stdin.readline().strip()
        if not token:
            print("  No token was supplied; nothing was saved.", file=sys.stderr)
            return 1
        if not store.is_valid(token):
            print(f"  token value is invalid: {store.SHAPE_RULE}", file=sys.stderr)
            return 1
        # Checked before it is written, the way the GitHub screen checks: a
        # credential GitLab refuses is saved by nobody, and the operator finds
        # out now rather than the first time the facade is asked for something.
        # Being unable to ask is not a refusal, so it saves and says so.
        result = store.verify(token)
        if result.accepted is False:
            print(f"  {result.detail}; nothing was saved.", file=sys.stderr)
            return 1
        # Decided before the write, not reported after it. A token that can
        # write used to be saved with a warning, which meant the operator was
        # told about it while it was already on disk and already exported to
        # every client -- the GitHub screen has always checked first, and this
        # now matches it.
        if result.write_capable:
            print(
                f"  This token can write ({', '.join(result.scopes)}); "
                "read_api is the contract. Nothing was saved.",
                file=sys.stderr,
            )
            return 1
        try:
            store.write(config_home, token)
        except store.TokenError as error:
            print(f"  {error}", file=sys.stderr)
            return 1
        print(f"  Saved {store.fingerprint(token)}")
        if result.accepted is None:
            print(f"  Saved without a check: {result.detail}")
        return 0

    if command == "check":
        if not saved:
            print("  No saved token to check.", file=sys.stderr)
            return 1
        result = store.verify(saved)
        if result.accepted is None:
            print(f"  Could not check it: {result.detail}")
            return 2
        if not result.accepted:
            print(f"  {result.detail}")
            return 1
        scopes = ", ".join(result.scopes) if result.scopes else "not published"
        # A token that can write is accepted by GitLab and still wrong for a
        # read-only facade. Checking the saved credential is the only place a
        # scope that was widened after it was stored can be caught, so this
        # fails rather than warns.
        if result.write_capable:
            print(f"  GitLab {result.detail}, but this token can write: {scopes}", file=sys.stderr)
            print("  read_api is the contract. Replace it with a read-only token.", file=sys.stderr)
            return 1
        print(f"  GitLab {result.detail}; scopes: {scopes}")
        return 0

    if command == "reveal":
        if not saved:
            print("  No saved token to reveal.", file=sys.stderr)
            return 1
        print(saved)
        return 0

    if command == "remove":
        try:
            removed = store.remove(config_home)
        except store.TokenError as error:
            print(f"  {error}", file=sys.stderr)
            return 1
        print("  Saved token removed." if removed else "  No saved token file exists.")
        return 0

    raise SystemExit(f"unknown gitlab-token command: {command}")


def _handle_mcp(context: CommandContext) -> int:
    command = context.args.mcp_command
    if command == "serve":
        if context.args.catalog_id == "gitlab_read":
            from .gitlab_read_mcp import main as run_gitlab

            return run_gitlab([])
        from .mcp_filter import main as run_filter

        return run_filter(
            ["--catalog-id", context.args.catalog_id],
            root=context.paths.root,
        )

    from .mcp_service import McpService

    service = McpService(context.paths)
    json_output = bool(getattr(context.args, "mcp_json", False))
    if command == "catalog":
        print_mcp_catalog(service.catalog(), json_output=json_output)
        return 0
    if command == "status":
        report = service.status(live=bool(getattr(context.args, "live", False)))
        print_mcp_status(report, json_output=json_output)
        return 1 if any(item.severity == "error" for item in report.items) else 0
    if command == "restore":
        service.restore(context.args.operation_id, confirmed=bool(context.args.confirm))
        print(f"  Restored MCP operation {context.args.operation_id}")
        return 0

    selection = tuple(context.args.mcp_select)
    targets = tuple(context.args.mcp_targets)
    plan = (
        service.plan(selection, targets)
        if command != "off"
        else service.plan_off(selection, targets)
    )
    print_mcp_plan(plan, json_output=json_output)
    if not plan.can_apply:
        return 1
    if command == "plan":
        return 0
    operation_id = (
        service.setup(selection, targets, confirmed=bool(context.args.confirm))
        if command == "setup"
        else service.off(selection, targets, confirmed=bool(context.args.confirm))
    )
    print(f"  MCP operation: {operation_id}")
    return 0


def _handle_update(context: CommandContext) -> int:
    args = context.args
    command = args.command
    # No planning phase on screen. It had a heading, a legend and three
    # [STEP]/[OK] pairs before the report, which is four screens of scaffolding
    # in front of one table -- and the sibling product's update goes straight to
    # its report. The phases are still timed, because the closing summary still
    # reports them; they just no longer narrate themselves.
    planning = InstallLog(sentinel="Plan complete", quiet=True)
    plan = context.lifecycle.plan_update(progress=planning.stage)
    print_update_plan(plan, command=command)
    if bool(getattr(args, "dry_run", False)):
        # A dry run has no prompt to close it, so it ended on the plan table.
        # The sibling product closes its own with a sentence for the same
        # reason; a rollup would be the wrong shape, since a plan is not a
        # health check and "All 3 component(s) look good" says nothing true
        # about work that has not happened.
        print()
        print("  Dry run: nothing was changed.")
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

    # The work phase opens with its own heading and legend, and is named the
    # same thing the sibling product names it. The [STEP] lines used to start
    # directly under the plan table, so the table the operator had just
    # approved and the run that followed it read as one block. A literal
    # title, so the lexicon check can read it.
    print_header("Updating", "Agentbot › Update › Updating")
    log_legend()
    print()
    applying = InstallLog(sentinel="Update complete")
    outcome = context.lifecycle.apply_update(plan, progress=applying.stage)
    # One result block, closing on one rollup. The outcome, the reconciliation
    # and the resync each used to print their own, and the resync brought a
    # second `=== Workspace resync ===` heading with it -- so one update ended
    # on three unrelated summaries, the last of which counted only the resync
    # while reading as the verdict on the whole run.
    print_header("Update result", f"Agentbot › {command.capitalize()} › Result")
    ok, check, miss = print_update_result(outcome)
    workspace_report = outcome.workspace_report
    print_rollup(ok=ok, check=check, miss=miss)
    # Last, after the result it describes. Planning and applying are one run to
    # the operator, so they are one summary: the ten seconds spent reading
    # twelve sources belongs in the total beside the work it was deciding
    # about.
    print_timing(
        command.capitalize(),
        {**planning.seconds, **applying.seconds},
        planning.total + applying.total,
    )
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
        result = context.lifecycle.preview_workspace(path, profile=args.profile, targets=targets)
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
    failed = result.status in {"conflict", "failed"}
    rows = [] if failed or args.no_memory else [_boot_project_memory(context, target)]
    print_workspace_report(result, extra_rows=rows)
    return 1 if failed else 0


def _boot_project_memory(context: CommandContext, target: Path) -> tuple[str, str, str]:
    """Give the booted repository project memory, so one command sets it all up.

    Memory is optional: no vault, an older schema, or a directory that is not a
    repository is a skipped row, and nothing here can fail the boot.
    """
    from . import memory, memory_autosync, memory_projects

    label = "Project memory"
    config_home = context.paths.config_home
    if memory_projects.repo_identity(target) is None:
        return (label, "not a Git repository", "skipped")
    try:
        root, _ = memory.find_vault(context.paths.root)
        if root is None:
            return (label, "no memory vault configured", "skipped")
        memory.read_marker(root)
        memory_autosync.before_read(root, config_home)
        found = memory_projects.resolve(root, target, config_home)
        if found.state == "resolved" and found.entry is not None:
            return (label, f"{found.entry['folder']} (already registered)", "ok")
        if found.state == "collision":
            return (label, "; ".join(found.problems) or "registry collision", "conflict")
        entry = memory_projects.register(root, target, config_home)
        outcome = memory_autosync.after_write(root, config_home)
        return (label, f"{entry['folder']} registered; sync {outcome['state']}", "applied")
    except (memory.MemoryVaultError, OSError) as error:
        return (label, str(error), "warn")


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
    if bool(getattr(context.args, "plan_only", False)):
        return show_install_plan(context.lifecycle, components)
    return run_agentbot_install(context.lifecycle, context.paths, components=components)


def _handle_skills(context: CommandContext) -> int:
    if context.args.skills_command == "update":
        plan = plan_skill_update(context.paths)
        previous = dict(plan.previous)
        print_header("Skills update", "Agentbot › Skills update")
        for catalog in plan.catalogs:
            old = previous.get(catalog.source_id)
            old_label = old[:12] if old else "unrecorded"
            print(
                f"  {catalog.source_id}: {old_label} -> {catalog.revision[:12]} "
                f"({len(catalog.skills)} candidate skill(s))"
            )
        print(f"  Review ID: {plan.review_id}")
        if context.args.confirm:
            if context.args.plan_sha256 != plan.review_id:
                raise SkillsInstallError(
                    "skills update requires the exact --plan-sha256 from a reviewed preview"
                )
            apply_skill_update(context.paths, plan)
            context.lifecycle.refresh_outputs()
            print("  Installed the reviewed source revisions.")
        else:
            print("  Preview only; use --yes --plan-sha256 REVIEW_ID to apply.")
        return 0
    if context.args.skills_command == "restore":
        snapshots = restore_skills(
            context.paths,
            apply=bool(getattr(context.args, "confirm", False)),
            previous=bool(getattr(context.args, "previous", False)),
        )
        if context.args.confirm:
            context.lifecycle.refresh_outputs()
        print_header("Skills restore", "Agentbot › Skills restore")
        for snapshot in snapshots:
            print(
                f"  {snapshot.source_id}: {snapshot.revision[:12]} "
                f"({len(snapshot.installed)} skill(s))"
            )
        print(
            "  Restored reviewed revisions."
            if context.args.confirm
            else "  Preview only; use --yes to apply."
        )
        return 0
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
    "mcp": _handle_mcp,
    "gitlab-token": _handle_gitlab_token,
    "memory": _handle_memory,
    "cli-config": _handle_cli_config,
    "cursor": _handle_cursor,
    "vscode": _handle_vscode,
    "update": _handle_update,
    "workspace": _handle_workspace,
    "boot": _handle_boot,
    "workspaces": _handle_workspaces,
    "resync": _handle_resync,
    "install": _handle_install,
    "skills": _handle_skills,
}


def _add_gitlab_token_parser(subparsers: argparse._SubParsersAction) -> None:
    """The GitLab read credential's subcommands."""
    gitlab_token = subparsers.add_parser(
        "gitlab-token", help="Inspect or manage the saved GitLab read token"
    )
    gitlab_token_sub = gitlab_token.add_subparsers(dest="gitlab_token_command", required=True)
    gitlab_token_sub.add_parser("status", help="Show whether a token is saved, by fingerprint")
    # Read from stdin, never an argument: an argv is world-readable in /proc.
    gitlab_token_sub.add_parser("set", help="Save a token read from standard input")
    # Check a candidate without storing it, so the menu can put the verdict in
    # front of the operator before it asks whether to save -- the order the
    # GitHub screen has always used.
    gitlab_token_sub.add_parser(
        "verify", help="Check a token read from standard input without saving it"
    )
    gitlab_token_sub.add_parser("check", help="Ask GitLab whether the saved token is accepted")
    gitlab_token_sub.add_parser("reveal", help="Print the saved token once")
    gitlab_token_sub.add_parser("remove", help="Delete the saved token")


def _add_memory_setup_parser(memory_sub: argparse._SubParsersAction) -> None:
    setup = memory_sub.add_parser("setup", help="Show, choose, or remove this machine's vault")
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument("--path", dest="setup_path", metavar="PATH", help="Use an existing checkout")
    mode.add_argument(
        "--clone", dest="setup_clone", metavar="URL", help="Clone a vault (with --dest)"
    )
    mode.add_argument("--new", dest="setup_new", metavar="PATH", help="Create a new empty vault")
    mode.add_argument(
        "--remove", dest="setup_remove", action="store_true", help="Forget the configured vault"
    )
    setup.add_argument("--dest", metavar="PATH", help="Destination for --clone")
    setup.add_argument("--remote", metavar="URL", help="Origin to record for --new")
    setup.add_argument("--yes", action="store_true", dest="confirm")
    setup.add_argument("--json", action="store_true", dest="memory_json")


def _add_memory_project_parser(memory_sub: argparse._SubParsersAction) -> None:
    project = memory_sub.add_parser(
        "project", help="Show or write the current repo's project memory"
    )
    project.add_argument("--json", action="store_true", dest="memory_json")
    actions = project.add_subparsers(dest="project_action")

    def with_body(parser: argparse.ArgumentParser, *, required: bool) -> None:
        source = parser.add_mutually_exclusive_group(required=required)
        source.add_argument("--stdin", action="store_true")
        source.add_argument("--from-file", dest="from_file", metavar="PATH")

    status = actions.add_parser("status", help="Show which project memory this repo resolves to")
    add = actions.add_parser("add", help="Add a decision, lesson, or note to this project")
    add.add_argument("--kind", required=True, choices=("decision", "lesson", "note"))
    add.add_argument("--title", required=True)
    add.add_argument("--tag", action="append", metavar="TAG")
    add.add_argument("--supersedes", metavar="ID", help="A record in this project it replaces")
    with_body(add, required=True)
    edit = actions.add_parser("edit", help="Change a record's body, title, or tags")
    edit.add_argument("path", metavar="PATH")
    edit.add_argument("--title")
    edit.add_argument("--tag", action="append", metavar="TAG")
    with_body(edit, required=False)
    context = actions.add_parser("context", help="Replace this project's active context")
    with_body(context, required=True)
    move = actions.add_parser("move", help="Move a record within this project")
    move.add_argument("path", metavar="PATH")
    move.add_argument("new_path", metavar="NEW_PATH")
    delete = actions.add_parser("delete", help="Delete a record from this project")
    delete.add_argument("path", metavar="PATH")
    retire = actions.add_parser("retire", help="Take a record out of retrieval without deleting it")
    retire.add_argument("path", metavar="PATH")
    promote = actions.add_parser("promote", help="Propose a project lesson or decision for core")
    promote.add_argument("path", metavar="PATH")
    promote.add_argument("--scope", choices=("global", "shared"), default="shared")
    maintain = actions.add_parser("maintain", help="Report what this project's memory needs")
    forget = actions.add_parser("forget", help="Remove this project's memory folder")
    forget.add_argument("--yes", action="store_true", dest="confirm")
    actions.add_parser("register", help="Give this repository project memory")
    attach = actions.add_parser("attach", help="Bind this no-remote checkout to a project")
    attach.add_argument("project", metavar="PROJECT")
    link = actions.add_parser("link", help="Human-only: add an origin as a project alias")
    link.add_argument("origin", metavar="ORIGIN_URL")
    link.add_argument("project", metavar="PROJECT")
    link.add_argument("--yes", action="store_true", dest="confirm")
    for parser in (
        status,
        add,
        edit,
        context,
        move,
        delete,
        retire,
        promote,
        maintain,
        forget,
        attach,
        link,
    ):
        parser.add_argument("--json", action="store_true", dest="memory_json")


def _add_memory_parser(subparsers: argparse._SubParsersAction) -> None:
    memory = subparsers.add_parser("memory", help="Inspect and validate the private memory vault")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    _add_memory_setup_parser(memory_sub)
    _add_memory_project_parser(memory_sub)
    sync = memory_sub.add_parser("sync", help="Sync the vault now, or show or set sync settings")
    sync.add_argument("--mode", choices=("auto", "manual"))
    sync.add_argument(
        "--interval", type=int, metavar="SECONDS", help="Minimum time between fetches"
    )
    sync.add_argument("--status", action="store_true", dest="status_only")
    sync.add_argument("--json", action="store_true", dest="memory_json")
    conflict = memory_sub.add_parser("conflict", help="List, show, or resolve sync conflicts")
    conflict.add_argument("--json", action="store_true", dest="memory_json")
    conflict_actions = conflict.add_subparsers(dest="conflict_action")
    conflict_list = conflict_actions.add_parser("list", help="List open conflicts")
    conflict_show = conflict_actions.add_parser("show", help="Show both versions of one conflict")
    conflict_show.add_argument("op", metavar="OP")
    conflict_resolve = conflict_actions.add_parser("resolve", help="Keep mine or theirs")
    conflict_resolve.add_argument("op", metavar="OP")
    conflict_resolve.add_argument("--keep", required=True, choices=("mine", "theirs"))
    for parser in (conflict_list, conflict_show, conflict_resolve):
        parser.add_argument("--json", action="store_true", dest="memory_json")
    memory_status = memory_sub.add_parser("status", help="Show vault location, schema, and state")
    memory_status.add_argument("--json", action="store_true", dest="memory_json")
    memory_validate = memory_sub.add_parser(
        "validate", help="Validate every vault file against its schema version"
    )
    memory_validate.add_argument("--json", action="store_true", dest="memory_json")
    memory_validate.add_argument(
        "--acknowledge-warning",
        action="append",
        choices=sorted(WARNING_RULES),
        dest="acknowledge_warning",
        metavar="RULE_ID",
        help="Accept one warning rule for this run only; blocking rules cannot be acknowledged",
    )
    propose = memory_sub.add_parser(
        "propose", help="Propose one core record for the user to review"
    )
    propose.add_argument(
        "--type",
        required=True,
        choices=("decision", "lesson", "preference", "profile"),
        dest="memory_type",
    )
    propose.add_argument(
        "--supersedes", action="append", metavar="ID", help="Core records it replaces"
    )
    propose.add_argument("--title", required=True)
    propose.add_argument("--scope", required=True, choices=("global", "shared", "project"))
    propose.add_argument("--project", action="append", metavar="SLUG")
    propose.add_argument("--tag", action="append", metavar="TAG")
    source = propose.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-file", dest="from_file", metavar="PATH")
    source.add_argument("--stdin", action="store_true", help="Read the body from standard input")
    propose.add_argument("--json", action="store_true", dest="memory_json")
    review = memory_sub.add_parser("review", help="List open proposals, or show one")
    review.add_argument("draft_path", nargs="?", metavar="proposals/core/FILE.md")
    review.add_argument("--json", action="store_true", dest="memory_json")
    approve = memory_sub.add_parser("approve", help="Human-only: promote one proposal into core")
    approve.add_argument("draft_path", metavar="proposals/core/FILE.md")
    approve.add_argument("--yes", action="store_true", dest="confirm")
    approve.add_argument("--json", action="store_true", dest="memory_json")
    reject = memory_sub.add_parser("reject", help="Human-only: remove a proposal")
    reject.add_argument("draft_path", metavar="proposals/core/FILE.md")
    reject.add_argument("--yes", action="store_true", dest="confirm")
    reject.add_argument("--json", action="store_true", dest="memory_json")
    search = memory_sub.add_parser("search", help="Search accepted records within scope")
    search.add_argument(
        "query",
        nargs="?",
        default="",
        help="Words to find; omit to list record headers (the index)",
    )
    search.add_argument(
        "--type",
        choices=("context", "preference", "decision", "lesson", "project"),
        dest="memory_type",
    )
    search.add_argument("--tag", metavar="TAG")
    search.add_argument("--limit", type=int, default=None, metavar="N")
    search.add_argument(
        "--history",
        action="store_true",
        help="Include superseded, retired, expired, and cold records, labelled",
    )
    _add_retrieval_scope(search)
    show = memory_sub.add_parser("show", help="Show one accepted record within scope")
    show.add_argument("record_path", metavar="PATH")
    show.add_argument("--max-bytes", type=int, default=65536, dest="max_bytes", metavar="N")
    _add_retrieval_scope(show)
    brief = memory_sub.add_parser("brief", help="Print a bounded, disposable session brief")
    brief.add_argument("--tokens", type=int, default=800, metavar="N")
    _add_retrieval_scope(brief)
    backup = memory_sub.add_parser("backup", help="Preview or refresh a verified local mirror")
    backup.add_argument(
        "--destination", metavar="PATH", help="Default: the backup this machine recorded"
    )
    backup.add_argument(
        "--encryption-assurance",
        choices=("unknown", "user-attested"),
        dest="assurance",
    )
    backup.add_argument("--yes", action="store_true", dest="confirm")
    backup.add_argument("--json", action="store_true", dest="memory_json")
    restore = memory_sub.add_parser("restore", help="Preview or restore into a new directory")
    restore.add_argument("--source", required=True, metavar="BACKUP")
    restore.add_argument("--destination", required=True, metavar="PATH")
    restore.add_argument("--yes", action="store_true", dest="confirm")
    restore.add_argument("--json", action="store_true", dest="memory_json")
    due = memory_sub.add_parser("due", help="List accepted records due for review or expired")
    due.add_argument("--limit", type=int, default=20, metavar="N")
    due.add_argument("--json", action="store_true", dest="memory_json")
    hook = memory_sub.add_parser("hook", help="Inspect or manage the vault's owned Git hooks")
    hook.add_argument(
        "hook_action", nargs="?", default="status", choices=("status", "install", "remove")
    )
    hook.add_argument("--yes", action="store_true", dest="confirm")
    hook.add_argument("--json", action="store_true", dest="memory_json")


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

    install_parser = subparsers.add_parser(
        "install", help="Install Agentbot into this machine's agent homes"
    )
    install_parser.add_argument(
        "--components",
        default="",
        help="Comma-separated subset to set up: skills, graphify, boost, "
        "vscode, cursor, cli-config. Omit for all of them.",
    )
    install_parser.add_argument(
        "--plan-only",
        action="store_true",
        dest="plan_only",
        # The menu's screen between the selector and the run. Prints what each
        # chosen component would do and asks; the exit status is the answer.
        help=argparse.SUPPRESS,
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
    # The GitLab read credential. GitHub's token stays in the Bash helper both
    # products share; this one is Agentbot's, and the menu drives it through
    # here so the secret never crosses a shell argument vector.
    _add_gitlab_token_parser(subparsers)

    _add_memory_parser(subparsers)

    mcp = subparsers.add_parser("mcp", help="Manage explicitly selected MCP servers")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_catalog = mcp_sub.add_parser("catalog", help="List validated MCP candidates")
    mcp_catalog.add_argument("--json", action="store_true", dest="mcp_json")
    mcp_status = mcp_sub.add_parser("status", help="Inspect managed MCP configuration")
    mcp_status.add_argument("--json", action="store_true", dest="mcp_json")
    mcp_status.add_argument("--live", action="store_true", help="Run admitted live checks")
    for action in ("plan", "setup", "off"):
        action_parser = mcp_sub.add_parser(action, help=f"{action.capitalize()} MCP configuration")
        action_parser.add_argument("--select", nargs="+", required=True, dest="mcp_select")
        action_parser.add_argument(
            "--targets",
            nargs="+",
            required=True,
            choices=("claude", "codex", "cursor"),
            dest="mcp_targets",
        )
        action_parser.add_argument("--json", action="store_true", dest="mcp_json")
        if action in {"setup", "off"}:
            action_parser.add_argument("--yes", action="store_true", dest="confirm")
    mcp_restore = mcp_sub.add_parser("restore", help="Restore a complete MCP operation backup")
    mcp_restore.add_argument("operation_id")
    mcp_restore.add_argument("--yes", action="store_true", dest="confirm")
    mcp_serve = mcp_sub.add_parser("serve", help=argparse.SUPPRESS)
    mcp_serve.add_argument("--catalog-id", required=True)
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
    update = subparsers.add_parser(
        "update",
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
    boot.add_argument(
        "--no-memory",
        action="store_true",
        dest="no_memory",
        help="Do not register this repository's project memory",
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
    for name, help_text in (("update", "Preview or install current reviewed source revisions"),):
        update_parser = skills_sub.add_parser(name, help=help_text)
        update_parser.add_argument(
            "--yes", dest="confirm", action="store_true", help="Apply the reviewed update"
        )
        update_parser.add_argument(
            "--plan-sha256", help="Exact review ID printed by a prior preview"
        )
    restore = skills_sub.add_parser(
        "restore", help="Preview or restore exact reviewed source revisions"
    )
    restore.add_argument("--yes", dest="confirm", action="store_true", help="Apply the restore")
    restore.add_argument(
        "--previous", action="store_true", help="Select the prior reviewed source snapshot"
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
        f"  Error: '{command}' is archived. MCP is managed by 'agentbot mcp' now; "
        "the retired catalog and interactive control-plane code is in Git history.",
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


#: Marks a line the menu's relay must print without its trailing newline. See
#: _TUI_KEEP_CURSOR in scripts/lib/tui.sh.
_KEEP_CURSOR = "\x17"


def _ask(question: str) -> None:
    """Write a question at the left edge and leave the cursor beside it.

    Under the menu, stdout is piped through a Bash relay that reads whole
    lines, so a question with no trailing newline would sit unread in it until
    the process exited. The line is terminated for the relay and marked, and
    the relay drops the newline again. Without the relay stdout is the
    terminal, where simply not writing a newline does the same thing.

    The indent belongs here rather than to each caller: passed in, it is inside
    a variable, and the sweep in tests/test_left_edge.sh reads format strings.
    It cannot see an indent it is not shown, and was right not to trust one.
    """
    if os.environ.get("AGENTBOT_TUI"):
        print(f"  {question}{_KEEP_CURSOR}")
    else:
        print(f"  {question}", end="")
    sys.stdout.flush()


def confirm_update_plan() -> bool:
    """Ask on the same stream the report was printed to, and read the terminal.

    The question used to be written straight to the terminal descriptor while
    the report went to stdout. Under the menu those are not the same path:
    tui_run_to_output pipes stdout through a Bash relay that re-reads it line by
    line, and the relay lags. The question overtook the report it was asking
    about and landed inside it, between a heading and its breadcrumb.

    Printed to stdout it cannot overtake anything, because one stream carries
    both in order. It ends with a newline for the same reason the relay needs
    one: `while IFS= read -r line` holds a partial line until end of input, so
    a question with the cursor left on its own line would not appear until the
    process had already exited.

    The answer still comes from the terminal, which is the point of the
    descriptor: stdin belongs to the pipeline, not to the operator.
    """
    print()
    _ask("Apply this Agentbot update plan? [y/N]: ")
    with _open_update_tty_stream(
        descriptor_name="AGENTBOT_UPDATE_TTY_IN_FD",
        path_name="AGENTBOT_UPDATE_TTY_INPUT",
        mode="r",
    ) as input_stream:
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
        selected_report = PruneReport(candidates=tuple(candidates_by_name[name] for name in names))
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
        # The heading and legend go first, so the per-source [STEP] lines have
        # something above them. They used to arrive bare, under whatever screen
        # happened to be there -- after a prune, under the prune's own table.
        print_header("Skills install", "Agentbot › Skills install")
        log_legend()
        print()
        results = lifecycle.install_skills()
        outputs = lifecycle.refresh_outputs()
        install_rc, (ok, check, miss) = print_skills_report(
            results, title="Skills install", include_header=False, include_rollup=False
        )
        refreshed = print_output_refresh_report(
            linked=outputs.claude_linked,
            updated=outputs.claude_updated,
            skipped=outputs.claude_skipped,
        )
        # One rollup for both sections, at the end. The sources printed one
        # mid-surface and the output refresh printed none, so the screen closed
        # on a table.
        print_rollup(ok=ok + refreshed[0], check=check + refreshed[1], miss=miss + refreshed[2])
        return install_rc
    if skills_command == "update":
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
            print_header("Installed skills", "Agentbot › Installed skills")
            print("  No installed skills found.")
            return 0
        print_header("Installed skills", "Agentbot › Installed skills")
        for skill in skills:
            print(f"  {skill}")
        return 0
    if skills_command == "doctor":
        return print_skills_doctor(lifecycle.diagnostics)
    raise SystemExit(f"unknown skills command: {skills_command}")


def print_skills_error(error: Exception) -> int:
    print(f"  Error: {error}", file=sys.stderr)
    return 1


#: What the operator chose on the plan screen, as an exit status. The menu is
#: Bash and the plan is Python, so the answer has to cross a process boundary;
#: these are the three ways out of that screen.
PLAN_CONFIRM = 0
PLAN_EDIT = 10
PLAN_BACK = 11


def show_install_plan(lifecycle: Lifecycle, components: tuple[str, ...] | None) -> int:
    """What the selected components would do, then confirm, edit or back.

    Between the selector and the run, as the sibling product's execution plan
    is. The selector says what was chosen; this says what choosing it means.
    """
    from .install_plan import build_install_plan

    chosen = components if components is not None else lifecycle.SELECTABLE_COMPONENTS
    rows = build_install_plan(lifecycle, tuple(chosen))
    print_header("Install plan", "Agentbot › Install › Plan")
    print_four_column_table(
        [(row.component, row.installed, row.available, row.action) for row in rows]
    )
    print()
    # The sibling product's execution-plan keys, in its own shortcut format.
    #
    # Its fourth key, `x confirm_forced`, is not here: forcing means bypassing
    # "this is already present" checks, and an Agentbot install has none to
    # bypass -- skills reconcile from the lock every time and the integrations
    # set up idempotently. A key that did what `c` does is the defect the token
    # screen's `[y/N/q]` was.
    _ask(f"{format_shortcuts('c', 'confirm', 'e', 'edit', 'q', 'back_to_menu')} : ")
    try:
        with _open_update_tty_stream(
            descriptor_name="AGENTBOT_UPDATE_TTY_IN_FD",
            path_name="AGENTBOT_UPDATE_TTY_INPUT",
            mode="r",
        ) as stream:
            answer = stream.readline().strip().lower()
    except OSError:
        # No terminal to ask: an unattended run has already chosen by invoking
        # the install, and stopping to wait for an answer nobody can give would
        # hang it. The plan above is still printed, so the log says what ran.
        print()
        return PLAN_CONFIRM
    if answer in {"c", "confirm", ""}:
        return PLAN_CONFIRM
    if answer in {"e", "edit"}:
        return PLAN_EDIT
    return PLAN_BACK


def run_agentbot_install(
    lifecycle: Lifecycle, paths: AgentbotPaths, *, components: tuple[str, ...] | None = None
) -> int:
    print_header("Install Agentbot", "Agentbot › Install Agentbot")
    # No "Selected:" line. The selector said what was chosen a moment ago, the
    # plan screen said it again with what each one would do, and the run below
    # opens every component with a [STEP] naming it -- so this said it a fourth
    # time to an operator who had just pressed confirm.
    # The key to the prefixes below, as the sibling product prints under its own
    # install heading. Without it [STEP] and [OK] are markers the reader is left
    # to infer.
    log_legend()
    print()
    # Emitted whether or not anyone is watching live. The [STEP] and [OK] lines
    # carry no cursor control, so a pipe degrades cleanly rather than losing
    # them -- and the piped case is `dotfiles full-update`, which is where the
    # silence this progress exists to fix was longest. The animation between a
    # step and its completion is written to the terminal instead of stdout for
    # that same reason; see install_log._StepSpinner.
    log = InstallLog()
    outcome = lifecycle.install(progress=log.stage, components=components)
    stop_progress_animation()

    # One table for everything the run did, then the doctor, then the time.
    ok, check, miss = print_install_summary(outcome)
    print_install_closing_line(ok=ok, check=check, miss=miss)
    # The table is skipped inside a full run, where the caller's postflight
    # prints the same one. The result still decides the exit code: this is a
    # quieter surface, not a dropped check.
    if os.environ.get("AGENTBOT_INSTALL_SHOW_DOCTOR", "1") == "0":
        # The same verdict print_doctor_summary gives: errors fail, warnings
        # do not. Failing on any issue here exited 1 on a single warning the
        # operator had no table to see.
        doctor_rc = 1 if any(i.level.lower() == "error" for i in outcome.diagnostics.issues) else 0
    else:
        doctor_rc = print_doctor_summary(list(outcome.diagnostics.issues))
    # Last, after the report it describes -- the same place the sibling
    # product's install puts it.
    print_timing("Install", log.seconds, log.total)
    skills_rc = skills_report_status(list(outcome.skills))
    if skills_rc != 0:
        return skills_rc
    if outcome.graphify.state == "broken":
        return 1
    if outcome.boost.state in {"broken", "forbidden", "unsafe-config"}:
        return 1
    return doctor_rc


def print_skills_doctor(diagnostics: Diagnostics) -> int:
    issues = diagnostics.skills_doctor_issues()
    print_header("Skills doctor", "Agentbot › Skills doctor")
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


def _platform_status_rows(diagnostics: Diagnostics) -> tuple:
    """The editor and CLI surfaces, read but never written.

    Status is read-only, so each is inspected with apply=False -- the same
    call the install makes for a component the operator did not select.
    """
    from .command_runner import CommandRunner
    from .platform_surfaces import surface_outcome

    paths = getattr(diagnostics, "paths", None)
    if paths is None:
        return ()
    runner = CommandRunner()
    return tuple(
        surface_outcome(key, label, paths, runner, apply=False)
        for key, label, _phase in Lifecycle.PLATFORM_COMPONENTS
    )


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
        mcp_catalog_count=snapshot.mcp_catalog_count,
        mcp_managed_count=snapshot.mcp_managed_count,
        # The checkout Diagnostics was built against, so status reports on the
        # repository it actually inspected rather than the process's cwd.
        repo_root=getattr(getattr(diagnostics, "paths", None), "root", None),
        platform=_platform_status_rows(diagnostics),
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


def print_help_command(topic: str | None) -> int:
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
