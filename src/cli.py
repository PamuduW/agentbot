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
    print_draft_list,
    print_draft_review,
    print_four_column_table,
    print_graphify_status,
    print_header,
    print_install_closing_line,
    print_install_summary,
    print_manual_skill_removal_report,
    print_mcp_catalog,
    print_mcp_plan,
    print_mcp_status,
    print_memory_approval,
    print_memory_due,
    print_memory_hooks,
    print_memory_proposal,
    print_memory_status,
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


def _handle_memory_drafts(context: CommandContext) -> int:
    from . import memory
    from . import memory_drafts as drafts

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    try:
        if args.memory_command == "propose":
            source = caller_path(args.from_file) if args.from_file else None
            body = drafts.read_body(source, sys.stdin.buffer)
            proposal = drafts.Proposal(
                kind=args.memory_type,
                title=args.title,
                scope=args.scope,
                projects=tuple(args.project or ()),
                tags=tuple(args.tag or ()),
            )
            result = drafts.propose(
                root, proposal, body, acknowledge=args.acknowledge_warning or ()
            )
            if as_json:
                print(json.dumps(drafts.proposal_json(result), indent=2))
            else:
                print_memory_proposal(result)
            return 0 if result.created else 1
        if args.draft_path:
            review = drafts.review_draft(root, args.draft_path)
            if as_json:
                print(json.dumps(drafts.draft_review_json(review), indent=2))
            else:
                print_draft_review(review)
            return 1 if any(item.severity == "error" for item in review.findings) else 0
        summaries, total = drafts.list_drafts(root)
        if as_json:
            print(json.dumps(drafts.draft_list_json(summaries, total), indent=2))
        else:
            print_draft_list(summaries, total)
        return 0
    except memory.MemoryVaultError as error:
        return _print_memory_state(
            memory.VaultStatus(state="broken", looked_in=looked, root=root, problem=str(error)),
            as_json=as_json,
        )


def _handle_memory_changes(context: CommandContext) -> int:
    from . import memory, memory_approve, memory_hook

    args = context.args
    as_json = bool(getattr(args, "memory_json", False))
    root, looked = memory.find_vault(context.paths.root)
    if root is None:
        return _print_memory_state(
            memory.VaultStatus(state="unconfigured", looked_in=looked), as_json=as_json
        )
    try:
        if args.memory_command == "approve":
            result = memory_approve.approve(
                root,
                args.draft_path,
                config_home=context.paths.config_home,
                apply=args.confirm,
                acknowledge=args.acknowledge_warning or (),
            )
            if as_json:
                print(json.dumps(memory_approve.approval_json(result), indent=2))
            else:
                print_memory_approval(result)
            return 0 if result.state in {"preview", "applied"} else 1
        action = args.hook_action
        if action == "status" or not args.confirm:
            state = memory_hook.inspect(root, context.paths.root)
        elif action == "install":
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
        print_memory_due(items, total=total, schema=schema, today=today)
    return 0


def _handle_memory(context: CommandContext) -> int:
    from . import memory

    if context.args.memory_command in {"propose", "review"}:
        return _handle_memory_drafts(context)
    if context.args.memory_command in {"approve", "hook"}:
        return _handle_memory_changes(context)
    if context.args.memory_command == "due":
        return _handle_memory_due(context)
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
            f"  This token can write ({', '.join(result.scopes)}); "
            "read_api is the contract.",
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
    # approved and the run that followed it read as one block.
    #
    # Not built from `command`: this handler also serves the `upgrade` alias,
    # which spelled a heading "Applying upgrade" under an "Upgrade" crumb --
    # the one word the lexicon reserves for apt. A built title is also invisible
    # to the lexicon check, which can only read literal ones.
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
    if bool(getattr(context.args, "plan_only", False)):
        return show_install_plan(context.lifecycle, components)
    return run_agentbot_install(context.lifecycle, context.paths, components=components)


def _handle_skills(context: CommandContext) -> int:
    if context.args.skills_command in {"update", "upgrade"}:
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
        print("  Restored reviewed revisions." if context.args.confirm else "  Preview only; use --yes to apply.")
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
    "upgrade": _handle_update,
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


def _add_memory_parser(subparsers: argparse._SubParsersAction) -> None:
    memory = subparsers.add_parser("memory", help="Inspect and validate the private memory vault")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
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
    propose = memory_sub.add_parser("propose", help="Write one local draft for later review")
    propose.add_argument(
        "--type", required=True, choices=("decision", "lesson", "project"), dest="memory_type"
    )
    propose.add_argument("--title", required=True)
    propose.add_argument("--scope", required=True, choices=("global", "shared", "project"))
    propose.add_argument("--project", action="append", metavar="SLUG")
    propose.add_argument("--tag", action="append", metavar="TAG")
    source = propose.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-file", dest="from_file", metavar="PATH")
    source.add_argument("--stdin", action="store_true", help="Read the body from standard input")
    propose.add_argument(
        "--acknowledge-warning",
        action="append",
        choices=sorted(WARNING_RULES),
        dest="acknowledge_warning",
        metavar="RULE_ID",
    )
    propose.add_argument("--json", action="store_true", dest="memory_json")
    review = memory_sub.add_parser("review", help="List pending drafts, or review one")
    review.add_argument("draft_path", nargs="?", metavar="drafts/FILE.md")
    review.add_argument("--json", action="store_true", dest="memory_json")
    approve = memory_sub.add_parser("approve", help="Preview or promote one reviewed draft")
    approve.add_argument("draft_path", metavar="drafts/FILE.md")
    approve.add_argument("--yes", action="store_true", dest="confirm")
    approve.add_argument(
        "--acknowledge-warning",
        action="append",
        choices=sorted(WARNING_RULES),
        dest="acknowledge_warning",
        metavar="RULE_ID",
    )
    approve.add_argument("--json", action="store_true", dest="memory_json")
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
    help_parser.add_argument(
        "--format",
        # `menu` was the tab-separated dump the Command lib used to parse into
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
    for name, help_text in (
        ("update", "Preview or install current reviewed source revisions"),
        ("upgrade", "Alias for previewing or applying skill updates"),
    ):
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
        doctor_rc = 1 if outcome.diagnostics.issues else 0
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
