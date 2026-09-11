"""Report printers for each Agentbot domain object.

Generic terminal primitives live in src/ui/table.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ..commands import CommandSpec, commands_for_surface
from ..models import Table, TableSection
from .table import (
    DIM,
    GREEN,
    RED,
    YELLOW,
    _c,
    highlight_manual_skill_name,
    print_four_column_table,
    print_header,
    print_note,
    print_rollup,
    print_section,
    print_section_block,
    print_table,
    print_table_model,
    shorten_detail,
)


def print_command_help(spec: CommandSpec | None = None) -> None:
    if spec is None:
        print_header("Agentbot Help", "Agentbot › Help")
        print("  Usage: agentbot <command> [options]")
        surfaces: tuple[tuple[Literal["public", "bootstrap"], str], ...] = (
            ("public", "── Commands ──"),
            ("bootstrap", "── Bootstrap commands ──"),
        )
        for surface, label in surfaces:
            print()
            print_section(label)
            # Bootstrap commands are not launcher commands. Their CommandSpec
            # usage says "./install.sh skills install", but this index showed
            # only the bare name, so they read as `agentbot skills install` --
            # which the launcher rejects. Both the operator and a reviewing
            # agent tried exactly that.
            if surface == "bootstrap":
                print(f"  {_c('Run with ./install.sh, not agentbot.', DIM)}")
            print()
            print_table(
                [
                    (item.name, item.summary, item.behavior)
                    for item in commands_for_surface(surface)
                ],
                headers=("command", "description", "behavior"),
                wrap_details=True,
            )
        print()
        print_section("── Environment ──")
        print()
        print_table(
            [
                ("AGENTBOT_HOME", "Owning Agentbot repository root", "info"),
                ("XDG_CONFIG_HOME", "Base for private Agentbot state", "info"),
                ("GITHUB_TOKEN", "Optional GitHub API credential; never rendered", "info"),
                ("NO_COLOR", "Disable ANSI color output", "info"),
            ],
            headers=("variable", "purpose", "behavior"),
            wrap_details=True,
        )
        return

    print_header(spec.name, f"Agentbot › Help › {spec.name}")
    print_table(
        [
            ("Usage", spec.usage, spec.behavior),
            ("Purpose", spec.summary, "info"),
            ("Effects", spec.effects, spec.behavior),
        ],
        show_header=False,
        wrap_details=True,
    )
    if spec.options:
        print_section_block("── Options ──")
        print_table(
            [
                (item.usage, f"{item.description} Default: {item.default}.", "info")
                for item in spec.options
            ],
            show_header=False,
            wrap_details=True,
        )
    if spec.examples:
        print_section_block("── Examples ──")
        print_table(
            [("Example", example, "info") for example in spec.examples],
            show_header=False,
            wrap_details=True,
        )
    if spec.related:
        print()
        print(f"  Related: {', '.join(spec.related)}")


def _repo_row(root: Path | str | None) -> tuple[str, str, str]:
    """The Agentbot checkout's state against its upstream.

    Loaded by path from the shared tree so both repositories answer this the
    same way; a failure to load is reported rather than raised, because a status
    that cannot check its own repository should still print everything else.
    """
    import sys
    from pathlib import Path

    if root is None:
        return ("Agentbot repo", "unchecked (no checkout given)", "skipped")
    shared = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "shared" / "python"
    if str(shared) not in sys.path:
        sys.path.insert(0, str(shared))
    try:
        import repo_status
    except ImportError:
        return ("Agentbot repo", "unchecked (checker unavailable)", "skipped")
    detail, _, result = repo_status.check(Path(root)).rpartition("|")
    return ("Agentbot repo", detail, result)


def print_status_summary(
    *,
    installed_skills: int,
    global_agents_exists: bool,
    skills_sources_exists: bool,
    enabled_sources: int = 0,
    global_lock_exists: bool = False,
    global_lock_skills: int = 0,
    claude_bridge_links: int = 0,
    claude_statusline_state: str = "unknown",
    manual_skill_count: int = 0,
    doctor_issue_count: int = 0,
    repo_root: Path | str | None = None,
    platform: tuple = (),
) -> None:
    manifest_detail = "skills.sources.yaml"
    if enabled_sources >= 0 and skills_sources_exists:
        manifest_detail = f"{enabled_sources} enabled source(s)"
    elif enabled_sources < 0:
        manifest_detail = "skills.sources.yaml (parse error)"

    lock_detail = "~/.agents/.skill-lock.json"
    if global_lock_exists and global_lock_skills >= 0:
        lock_detail = f"~/.agents/.skill-lock.json ({global_lock_skills} pinned)"
    elif global_lock_exists:
        lock_detail = "~/.agents/.skill-lock.json (unreadable)"

    table = Table(
        title="Check Status",
        breadcrumb="Agentbot › Check Status",
        sections=(
            TableSection(
                # No section rule: the L10 contract gives a rule to a surface
                # with two or more groups, because a rule earns its place by
                # separating things. This surface has one table, and the header
                # above it already says "Check Status".
                label="",
                rows=(
                    (
                        "Installed skills",
                        str(installed_skills),
                        "ok" if installed_skills else "check",
                    ),
                    (
                        "Global AGENTS.md",
                        "global/AGENTS.md",
                        "ok" if global_agents_exists else "missing",
                    ),
                    (
                        "Skills manifest",
                        manifest_detail,
                        "ok" if skills_sources_exists else "missing",
                    ),
                    (
                        "Global skill lock",
                        lock_detail,
                        "ok" if global_lock_exists and global_lock_skills != 0 else "check",
                    ),
                    (
                        "Claude bridge",
                        f"{claude_bridge_links} symlink(s)" if claude_bridge_links else "none",
                        "ok" if claude_bridge_links else "check",
                    ),
                    (
                        "Claude statusline",
                        claude_statusline_state,
                        "ok" if claude_statusline_state == "ok" else "check",
                    ),
                    (
                        "Prunable skills",
                        f"{manual_skill_count} outside managed sources"
                        if manual_skill_count
                        else "none",
                        "info" if manual_skill_count else "ok",
                    ),
                    (
                        "Doctor",
                        "no issues"
                        if doctor_issue_count == 0
                        else f"{doctor_issue_count} issue(s)",
                        "ok" if doctor_issue_count == 0 else "check",
                    ),
                    # The checkout is part of this machine's setup too, and
                    # status never said whether it had updates waiting. The
                    # check is bounded and degrades to "unchecked" rather than
                    # hanging on a slow or absent network.
                    _repo_row(repo_root),
                    # The editor and CLI surfaces, which had a Platform submenu
                    # of their own -- so the one screen that answers "is this
                    # machine set up" was silent about three parts of it.
                    *((row.label, row.detail, row.result) for row in platform),
                ),
            ),
        ),
    )
    print_table_model(table)


def print_doctor_summary(issues: list, *, include_header: bool = True) -> int:
    if include_header:
        print_header("Doctor", "Agentbot › Doctor")
    else:
        # A frame of its own, not a `── rule ──` inside the status table's.
        # The rule form belongs to groups of one table; this is a second
        # surface with its own subject, and every other one on screen opens
        # with a title and a breadcrumb.
        #
        # Nothing at all when the machine is healthy: the status table above
        # already carries a Doctor row saying "no issues", and this repeated it
        # as a heading, a fabricated "Health check" row and a rollup over the
        # one row it had just invented. The section exists to expand on a
        # problem, so it appears when there is one.
        if not issues:
            return 0
        print_header("Doctor issues", "Agentbot › Check Status › Doctor issues")
    # print_header does not emit column names; print_table must.
    show_columns = True
    if not issues:
        print_table(
            [("Health check", "skills + global baseline", "ok")],
            show_header=show_columns,
        )
        print_rollup(ok=1, check=0, miss=0)
        return 0

    rows: list[tuple[str, str, str]] = []
    errors = 0
    warnings = 0
    for issue in issues:
        level = issue.level.lower()
        if level == "error":
            errors += 1
        else:
            warnings += 1
        result = "error" if level == "error" else "check"
        rows.append((issue.scope, issue.message, result))
    _ok, check, miss = print_table(
        rows,
        show_header=show_columns,
        wrap_details=True,
        detail_highlighter=highlight_manual_skill_name,
    )
    # One rollup, not two. This printed "0 error(s), 8 warning(s)." and then
    # "0 ok, 8 need attention." -- the same rows counted twice in two
    # vocabularies, when the point of the rollup is one place to look. The
    # split survives: an error is red and counts as missing, a warning yellow.
    print_rollup(ok=0, check=check, miss=miss)
    return 1 if errors else 0


# Integration states, mapped to the result vocabulary.
#
# These rows were written as ("State", status.state, status.state) -- the same
# string in the detail cell and the result cell. The detail column is forty
# wide and the result column is ten, so `unsafe-config`, `not-installed` and
# `skill-without-cli` were cut to `unsafe-co…`, `not-insta…` and `skill-wit…`,
# and five of the ten states were outside the vocabulary entirely, rendering
# uncoloured and counting as attention items.
#
# The detail cell still carries the state in full. The result cell carries the
# verdict, which is what the column is for.
_INTEGRATION_RESULTS = {
    "ready": "ok",
    "stale": "stale",
    "partial": "partial",
    "conflict": "conflict",
    "broken": "error",
    "absent": "missing",
    "not-installed": "missing",
    # Forbidden BoostGraph or MCP configuration is present: something is there
    # that must not be, which is a state to fix rather than to note.
    "unsafe-config": "check",
    "forbidden": "error",
    # Half-installed: one side of the integration exists and the other does not.
    "cli-only": "partial",
    "skill-without-cli": "partial",
}


def integration_result(state: str) -> str:
    """The result word for an integration state.

    An unmapped state falls through to `check` rather than to the raw string:
    an unknown word in the result column is uncoloured and silently counted as
    attention, and `check` at least says the right thing while it is unmapped.
    """
    return _INTEGRATION_RESULTS.get(state, "check")


# Workspace render actions, mapped the same way and for the same reason.
#
# The kind went straight into the result cell, and `create` and `update` are in
# no vocabulary: previewing a new workspace showed two uncoloured `create` rows
# and closed "0 ok, 2 need attention." over a plan in which nothing was wrong.
#
# What a kind means depends on whether the run wrote anything. In a preview
# `create` is what would happen; in an applied run it is what did. The detail
# cell names the action either way -- "create managed AGENTS.md" -- so the
# result cell is free to answer the question the rollup asks.
_WORKSPACE_PREVIEW_RESULTS = {
    "create": "preview",
    "update": "preview",
    "unchanged": "unchanged",
    "conflict": "conflict",
}
_WORKSPACE_APPLIED_RESULTS = {
    "create": "applied",
    "update": "applied",
    "unchanged": "unchanged",
    "conflict": "conflict",
}


def workspace_action_result(kind: str, *, applied: bool) -> str:
    """The result word for a render action, given what the run actually did."""
    table = _WORKSPACE_APPLIED_RESULTS if applied else _WORKSPACE_PREVIEW_RESULTS
    return table.get(kind, "check")


#: Integration states an install should expand rather than summarise. Anything
#: else is one row saying it is fine; these are the ones worth the detail.
_EXPAND_STATES = {"broken", "forbidden", "unsafe-config", "conflict", "partial"}


def _integration_rows(label: str, status, rows) -> list[tuple[str, str, str]]:
    """One row, plus the detail rows indented under it when it needs looking at.

    An install printed the whole Graphify table and the whole Boost table every
    time, healthy or not -- forty lines saying nothing was wrong. Reduced to a
    line each, and expanded only when the state is one somebody has to act on.
    """
    summary = (label, status.state, integration_result(status.state))
    if status.state not in _EXPAND_STATES:
        return [summary]
    return [summary, *((f"    {name}", detail, result) for name, detail, result in rows)]


def print_install_summary(outcome) -> tuple[int, int, int]:
    """Everything an install did, in one table.

    It used to print five: a skills report with its own rollup, an output
    refresh, a full Graphify table, a full Boost table and the doctor. Four of
    them said "all good" in their own heading with their own rollup, so the one
    thing the operator wanted -- did this work -- was five answers in five
    places.
    """
    print_header("Install summary", "Agentbot › Install › Summary")
    rows: list[tuple[str, str, str]] = [*_skill_source_rows(outcome.skills)]
    rows.append(
        (
            "Claude skill links",
            f"{outcome.outputs.claude_linked} linked, "
            f"{outcome.outputs.claude_updated} updated, "
            f"{outcome.outputs.claude_skipped} skipped",
            "ok",
        )
    )
    rows.extend(_integration_rows("Graphify", outcome.graphify, _graphify_rows(outcome.graphify)))
    rows.extend(_integration_rows("Boost", outcome.boost, _boost_rows(outcome.boost)))
    for surface in outcome.platform:
        rows.append((surface.label, surface.detail, surface.result))
    # Truncated, not wrapped: a summary is one line per thing, and a wrapped
    # repository name turned twenty rows into thirty-odd. The command behind
    # each row is where the untruncated detail lives.
    return print_table(rows)


def _graphify_rows(status) -> list[tuple[str, str, str]]:
    """The detail rows, without the State row the summary supplies itself."""
    cli_detail = str(status.cli_path) if status.cli_path else "not installed"
    return [
        ("CLI", cli_detail, "ok" if status.cli_path else "missing"),
        ("CLI version", status.cli_version or "—", "ok" if status.cli_version else "check"),
        (
            "Agent Skills",
            str(status.skill_path),
            "ok" if status.skill_path.is_file() else "missing",
        ),
        ("Codex", status.codex_state, "ok" if status.codex_state == "linked" else "check"),
        ("Claude", status.claude_state, "ok" if status.claude_state == "linked" else "check"),
    ]


def _skill_source_rows(results) -> list[tuple[str, str, str]]:
    """One row per source: what it is, where it came from, how it went."""
    from ..skills_installer import InstallResult

    rows: list[tuple[str, str, str]] = []
    for result in results:
        if not isinstance(result, InstallResult):
            continue
        if result.skipped:
            rows.append((result.source_id, "—", "skipped"))
            continue
        repo = result.command[4] if len(result.command) > 4 else ""
        if result.returncode == 0:
            rows.append((result.source_id, repo, "ok"))
            continue
        detail = shorten_detail(result.stderr or result.stdout or f"exit {result.returncode}")
        rows.append((result.source_id, detail, "failed"))
    return rows


def print_install_closing_line(*, ok: int, check: int, miss: int) -> None:
    """How the install went, in the sibling product's words and colours.

    "Install finished — N component(s) look good." there, and the same here, so
    a full update that prints both runs into one terminal closes each half the
    same way.
    """
    print()
    if check == 0 and miss == 0:
        print(f"  {_c(f'Install finished — {ok} component(s) look good.', GREEN)}")
    else:
        needing = check + miss
        print(f"  {_c(f'Install finished — {ok} ok, {needing} need attention.', YELLOW)}")
    print()


def skills_report_status(results: list) -> int:
    """The exit status the skills half of an install contributes.

    Was a side effect of printing the skills table. The table is part of the
    summary now, and the status is still the command's, so it is asked for
    rather than produced in passing.
    """
    from ..skills_installer import summarize_install_results

    return 0 if summarize_install_results(results).failed == 0 else 1


def print_graphify_status(status) -> None:
    """Render the Graphify integration state without performing repairs."""
    print_header("Graphify", "Agentbot › Graphify")
    rows = [
        ("State", status.state, integration_result(status.state)),
        *_graphify_rows(status),
    ]
    ok, check, miss = print_table(rows)
    # The message explains; the rollup concludes. Printed after it, the message
    # was the last thing on the screen, so the one line the contract puts at the
    # bottom for "does this need me?" was not at the bottom. On graphify the two
    # even disagreed: "5 ok, 1 need attention." above "integration are ready."
    print()
    print_note(status.message)
    # print_rollup opens and closes with a blank line.
    print_rollup(ok=ok, check=check, miss=miss)


def _boost_rows(status) -> list[tuple[str, str, str]]:
    """The detail rows, without the State row the summary supplies itself."""
    rows = [
        (
            "CLI",
            str(status.cli_path) if status.cli_path else "not installed",
            "ok" if status.cli_path else "missing",
        ),
        ("CLI version", status.cli_version or "—", "ok" if status.cli_version else "check"),
        (
            "Tracing upload",
            "disabled" if status.upload_disabled else "not disabled",
            "ok" if status.upload_disabled else "check",
        ),
        (
            "Auto-update",
            "disabled" if status.auto_update_disabled else "not disabled",
            "ok" if status.auto_update_disabled else "check",
        ),
        (
            "BoostGraph / MCP",
            status.graph_state,
            "ok" if status.graph_state == "absent" else "error",
        ),
        ("Claude", status.claude_state, "ok" if status.claude_state in {"ready", "skipped"} else "check"),
        ("Codex", status.codex_state, "ok" if status.codex_state in {"ready", "skipped"} else "check"),
        ("Cursor", status.cursor_state, "ok" if status.cursor_state in {"ready", "skipped"} else "check"),
    ]
    if status.user_flags:
        rows.append(
            (
                "Feature flags",
                ", ".join(
                    f"{name}={'on' if value else 'off'}" for name, value in status.user_flags
                ),
                "ok" if not status.diverged_flags else "check",
            )
        )
    if status.diverged_flags:
        rows.append(("Flags off policy", ", ".join(status.diverged_flags), "check"))
    if status.stale_artifacts:
        rows.append(
            (
                "Artifact version",
                f"{len(status.stale_artifacts)} file(s) predate {status.cli_version or 'the CLI'}",
                "check",
            )
        )
    if status.shadowing_configs:
        rows.append(
            (
                "Repo config override",
                ", ".join(str(path) for path in status.shadowing_configs),
                "error",
            )
        )
    return rows


def print_boost_status(status) -> None:
    """Render Boost CLI, safety, and Claude/Codex/Cursor integration state."""
    print_header("Boost", "Agentbot › Boost")
    rows = [
        ("State", status.state, integration_result(status.state)),
        *_boost_rows(status),
    ]
    ok, check, miss = print_table(rows)
    # The message explains; the rollup concludes. Printed after it, the message
    # was the last thing on the screen, so the one line the contract puts at the
    # bottom for "does this need me?" was not at the bottom. On graphify the two
    # even disagreed: "5 ok, 1 need attention." above "integration are ready."
    print()
    print_note(status.message)
    # print_rollup opens and closes with a blank line.
    print_rollup(ok=ok, check=check, miss=miss)


def print_skills_report(
    results: list, *, title: str, include_header: bool = True, include_rollup: bool = True
) -> tuple[int, tuple[int, int, int]]:
    """The installed sources. Standalone, or as a section of a larger surface.

    `skills install` prints its heading before the work now, so the [STEP]
    lines have something above them -- which means this must not print a second
    one, and must not close on a rollup the surface has more sections to add
    to.
    """
    from ..skills_installer import summarize_install_results

    summary = summarize_install_results(results)
    if include_header:
        print_header(title, f"Agentbot › {title}")
    print_section_block("── Sources ──")
    rows = _skill_source_rows(results)

    if rows:
        ok, check, miss = print_table(rows, show_header=False)
    else:
        print(f"  {_c('No active skill sources configured.', DIM)}")
        ok = check = miss = 0

    print()
    if summary.failed:
        print(
            f"  {_c(str(summary.ok), GREEN)} ok, "
            f"{_c(str(summary.failed), RED)} failed, "
            f"{_c(str(summary.skipped), YELLOW)} skipped"
        )
        # Any failed source is an error. A partial install leaves the machine
        # in a state the user did not ask for, and silently exiting 0 meant
        # `agentbot install` and `dotfiles full-update` reported success while
        # skills were missing.
        return 1, (ok, check, miss)
    print(f"  {_c(f'{summary.ok} source(s) installed successfully.', GREEN)}")
    if rows and include_rollup:
        print_rollup(ok=ok, check=check, miss=miss)
    # The counts as well as the status: a surface that adds sections after this
    # one closes on a rollup covering all of them.
    return 0, (ok, check, miss)


def print_skills_update_report(
    *,
    linked: int,
    skipped: int = 0,
    updated: int = 0,
    updated_skills: tuple[str, ...] = (),
    upstream_deleted_skills: tuple[str, ...] = (),
) -> int:
    print_section_block("── Refresh ──")
    bridge_detail = f"{linked} linked"
    if updated:
        bridge_detail += f", {updated} updated"
    if skipped:
        bridge_detail += f", {skipped} skipped"
    rows = [
        ("global lock", "~/.agents/.skill-lock.json", "ok"),
        ("claude bridge", bridge_detail, "ok"),
        ("codex sync", "~/.codex/AGENTS.md + skills", "ok"),
    ]
    if updated_skills:
        rows.append(("updated skills", ", ".join(updated_skills), str(len(updated_skills))))
    if upstream_deleted_skills:
        rows.append(
            (
                "upstream deleted",
                ", ".join(upstream_deleted_skills),
                str(len(upstream_deleted_skills)),
            )
        )
    ok, check, miss = print_table(rows)
    print()
    print_rollup(ok=ok, check=check, miss=miss)
    return 0


def print_output_refresh_report(
    *, linked: int, updated: int, skipped: int
) -> tuple[int, int, int]:
    print_section_block("── Output refresh ──")
    return print_table(
        [
            (
                "Claude skill links",
                f"{linked} linked, {updated} updated, {skipped} skipped",
                "ok",
            )
        ],
        show_header=False,
    )


def _counted(items) -> str:
    """`2: a.md, b.md`, or `none`. The count led the result column before.

    A number is not a result: it is uncoloured beside coloured siblings, and any
    rollup counting the table reads it as an attention item. It belongs with the
    thing it counts.
    """
    names = ", ".join(str(item) for item in items)
    return f"{len(items)}: {names}" if names else "none"


def print_reconciliation_report(result) -> tuple[int, int, int]:
    """Render the final source-owned reconciliation outcome as a compact table."""
    print_section_block("── Reconciliation report ──")
    return print_table(
        [
            # `applied-with-local-changes` is twenty-six characters and this
            # column is ten, so the row read `applied-w…`. The status is the
            # detail; the result says whether it worked.
            (
                "status",
                f"repository reconciliation: {result.status}",
                "ok" if result.status.startswith("applied") else result.status,
            ),
            ("changed files", _counted(result.changed_paths), "info"),
            ("updated skills", _counted(result.updated_skills), "info"),
            ("added skills", _counted(result.added_skills), "info"),
            ("removed skills", _counted(result.removed_skills), "info"),
        ],
        show_header=False,
        wrap_details=True,
    )


def print_workspace_report(result) -> None:
    from ..workspace_service import WorkspaceResult

    if not isinstance(result, WorkspaceResult):
        raise TypeError("expected WorkspaceResult")
    print_header("Workspace", "Agentbot › Workspace")
    # One group, so no section rule: a rule that separates nothing is one the
    # contract does not draw, and the table prints its own column header.
    applied = result.status == "applied"
    rows: list[tuple[str, str, str]] = []
    for action in result.actions:
        rows.append(
            (
                action.relative_path,
                action.detail,
                workspace_action_result(action.kind, applied=applied),
            )
        )
    if not rows:
        rows.append((str(result.path), result.message, result.status))
    ok, check, miss = print_table(rows, wrap_details=True)
    print()
    print(f"  {result.message}")
    print_rollup(ok=ok, check=check, miss=miss)


def print_workspace_resync_report(
    report, *, include_header: bool = True, include_rollup: bool = True
) -> tuple[int, int, int]:
    """Render a resync, standalone or as part of a larger surface.

    `agentbot resync` is its own surface and keeps both. The update run embeds
    it, where a second `=== Workspace Resync ===` in the middle of the result
    split one outcome into two screens, and a rollup counting only the resync
    read as the rollup for the whole update.
    """
    if include_header:
        print_header("Workspace Resync", "Agentbot › Workspace Resync")
    print_section_block("── Workspaces ──")
    rows: list[tuple[str, str, str]] = []
    for result in report.results:
        if result.actions:
            applied = result.status == "applied"
            for action in result.actions:
                rows.append(
                    (
                        f"{result.path}:{action.relative_path}",
                        action.detail,
                        workspace_action_result(action.kind, applied=applied),
                    )
                )
        else:
            rows.append((str(result.path), result.message, result.status))
    ok = check = miss = 0
    if rows:
        ok, check, miss = print_table(rows, show_header=False, wrap_details=True)
    else:
        print("  No registered workspaces.")
    print()

    global_actions = getattr(report, "global_actions", ()) or ()
    print_section_block("── Global ──")
    if global_actions:
        global_rows = [
            (
                action.relative_path,
                action.detail,
                workspace_action_result(action.kind, applied=bool(getattr(report, "applied", False))),
            )
            for action in global_actions
        ]
        global_ok, global_check, global_miss = print_table(
            global_rows, show_header=False, wrap_details=True
        )
        ok += global_ok
        check += global_check
        miss += global_miss
    else:
        print("  No global outputs planned.")
    # One rollup for both groups: the reader is asking whether this resync needs
    # them, and that question has one answer per surface, not one per section.
    if include_rollup:
        print_rollup(ok=ok, check=check, miss=miss)
    return ok, check, miss


def print_workspace_list(records) -> None:
    print_header("Workspaces", "Agentbot › Workspaces")
    # No section rule: this surface has one group, and a rule that separates
    # nothing is one the contract does not draw. The table prints its own
    # column header instead, which print_section_block was supplying.
    if not records:
        print("  No registered workspaces.")
        # Closes on the rollup like the populated branch, rather than being the
        # one shape of this surface that stops on prose.
        print_rollup(ok=0, check=0, miss=0)
        return
    rows: list[tuple[str, str, str]] = []
    for record in records:
        exists = Path(record.path).is_dir()
        detail = (
            f"{record.kind}, {record.policy_mode}, profile={record.profile}, "
            f"targets={','.join(record.targets)}"
        )
        rows.append((record.path, detail, "ok" if exists and record.enabled else "missing"))
    ok, check, miss = print_table(rows, wrap_details=True)
    print_rollup(ok=ok, check=check, miss=miss)


def print_workspace_removed(record) -> None:
    print_header("Workspace removed", "Agentbot › Workspaces › Remove")
    print(f"  Stopped managing: {record.path}")
    print("  No workspace files were changed.")


def _planned_workspace_writes(report) -> int:
    """Render actions that would write something, across workspaces and globals."""
    writes = sum(
        1
        for result in getattr(report, "results", ())
        for action in getattr(result, "actions", ())
        if action.kind in {"create", "update"}
    )
    return writes + sum(
        1
        for action in getattr(report, "global_actions", ())
        if action.kind in {"create", "update"}
    )


def _counted_names(items) -> str:
    """`2: alpha, beta`, or `none`."""
    names = ", ".join(str(item) for item in items)
    return f"{len(items)}: {names}" if names else "none"


def print_update_result(outcome) -> tuple[int, int, int]:
    """What the update changed, one row per thing it could have changed.

    This was three tables. The first had one row -- "Update | Update plan
    applied. | ok" -- restating the heading above it and the rollup below it.
    The second was the reconciliation, five rows of which four read "none" on
    a run that changed nothing, under a heading naming an internal step rather
    than anything the operator asked for. The third was the resync.
    """
    reconcile = outcome.reconcile
    rows: list[tuple[str, str, str]] = []

    # A run that did not apply leads with why. On the happy path there is no
    # such row: "Update | Update plan applied. | ok" restated the heading above
    # it and the rollup below it, which is what made this table worth
    # reworking. A failure is the one case where the outcome says something the
    # rows underneath cannot.
    if not outcome.status.startswith("applied"):
        rows.append(
            ("Update", outcome.message or outcome.status, outcome.status)
        )

    if reconcile is not None:
        changed = tuple(reconcile.changed_paths)
        added = tuple(reconcile.added_skills)
        removed = tuple(reconcile.removed_skills)
        updated = tuple(reconcile.updated_skills)
        touched = len(added) + len(removed) + len(updated)
        rows.append(
            (
                "Skills",
                _skill_change_detail(added, removed, updated),
                "applied" if touched else "unchanged",
            )
        )
        rows.append(
            (
                "Repository files",
                _counted_names(changed),
                "applied" if changed else "unchanged",
            )
        )

    report = outcome.workspace_report
    if report is not None:
        rows.append(_workspace_row("Workspaces", getattr(report, "results", ())))
        rows.append(_global_row(getattr(report, "global_actions", ())))

    if not rows:
        rows.append(("Update", outcome.message or outcome.status, "ok"))
    return print_table(rows, wrap_details=True)


def _skill_change_detail(added, removed, updated) -> str:
    """Which skills changed, by name.

    Counts would be shorter and would not answer the question the row exists
    for: an operator reading "1 removed" still has to go and find out which.
    """
    parts = [
        f"{len(added)} added: {', '.join(added)}" if added else "",
        f"{len(removed)} removed: {', '.join(removed)}" if removed else "",
        f"{len(updated)} updated: {', '.join(updated)}" if updated else "",
    ]
    written = "; ".join(part for part in parts if part)
    return written or "no changes"


def _kind_row(label: str, kinds: list[str], total: int) -> tuple[str, str, str]:
    """One row from a bag of render-action kinds.

    A conflict outranks everything else: it is the only one of these that needs
    the operator, and counting it as "rewritten" -- which the first version of
    this did -- hid the one outcome worth reporting.
    """
    conflicts = sum(1 for kind in kinds if kind == "conflict")
    if conflicts:
        return label, f"{conflicts} of {total} in conflict", "conflict"
    written = sum(1 for kind in kinds if kind in {"create", "update"})
    if written:
        return label, f"{written} of {total} rewritten", "applied"
    return label, f"{total} current", "unchanged"


def _workspace_row(label: str, results) -> tuple[str, str, str]:
    results = tuple(results)
    kinds = [
        action.kind for result in results for action in getattr(result, "actions", ())
    ]
    # A result can fail without producing any action at all, so its own status
    # counts too.
    kinds += [
        result.status for result in results if getattr(result, "status", "") == "conflict"
    ]
    return _kind_row(label, kinds, len(results))


def _global_row(actions) -> tuple[str, str, str]:
    actions = tuple(actions)
    return _kind_row("Global outputs", [action.kind for action in actions], len(actions))


def print_update_plan(plan, *, command: str = "update") -> None:
    title = command.capitalize()
    # `=== Update report ===`, as the sibling product names the same screen.
    # This read `=== Agentbot update ===`, which is the name of the whole run
    # rather than of the report inside it -- and the run now has a planning
    # phase above this and an applying phase below.
    print_header(f"{title} report", f"Agentbot › {title} › Report")
    skill_changes = (
        len(plan.reconcile.wildcard_additions)
        + len(plan.reconcile.wildcard_removals)
        + len(plan.reconcile.explicit_missing)
        + len(plan.reconcile.manifest_changes)
    )
    graphify_changes = 1 if plan.graphify_action in {"setup", "refresh"} else 0
    workspace_writes = _planned_workspace_writes(plan.workspace_report)
    # Four columns, as the sibling product's update report has: what is here,
    # what is available, and what approving this will do to it. Three columns
    # could only say "preview" three times, which told the operator the screen
    # they were already looking at was a preview.
    rows = [
        (
            "Skills",
            f"{len(plan.source_catalogs)} source(s) read",
            f"{skill_changes} change(s)" if skill_changes else "none",
            "reconcile" if skill_changes else "up to date",
        ),
        (
            "Graphify",
            plan.graphify_action,
            "—",
            plan.graphify_action if graphify_changes else "skip",
        ),
        (
            "Workspaces",
            f"{len(plan.workspace_report.results)} registered",
            f"{workspace_writes} to write" if workspace_writes else "none",
            "refresh" if workspace_writes else "up to date",
        ),
    ]
    print_four_column_table(rows)
    # A closing line, not a rollup: the sibling's report says "0 verified
    # upgrades; 5 checks or refreshes remain." for the same reason. "All 3
    # component(s) look good" would be a verdict on work that has not happened,
    # and this is the last thing read before approving it.
    # The sibling's report closes on "0 verified upgrades; 5 checks or refreshes
    # remain." -- a count of what approving it will do, not a health verdict.
    changes = skill_changes + graphify_changes + workspace_writes
    verified = 3 - sum(1 for count in (skill_changes, graphify_changes, workspace_writes) if count)
    print()
    print(f"  {verified} already current; {changes} change(s) on apply.")


def print_update_outcome(outcome) -> tuple[int, int, int]:
    result = "ok" if outcome.status in {"applied", "applied-with-local-changes"} else outcome.status
    # Labelled, because an unlabelled table under a heading that already has
    # sections below it reads as output that lost its heading.
    print_section_block("── Outcome ──")
    return print_table(
        [("Update", outcome.message or outcome.status, result)],
        show_header=False,
        wrap_details=True,
    )


def print_skill_prune_report(report, *, include_manual: bool = False) -> int:
    """Render the prune plan or result. Returns the command's exit code."""
    print_header("Skills Prune", "Agentbot › Skills Prune")

    if report.blocked_reason is not None:
        print_table(
            [("Skill lock", report.blocked_reason, "error")],
            show_header=True,
            wrap_details=True,
        )
        print_rollup(ok=0, check=0, miss=1)
        return 1

    rows = [
        (item.name, item.detail, item.reason)
        for item in report.candidates
        if item.removable_by_default or item.reason == "manual"
    ]
    if not rows:
        print_table(
            [("Skill store", "every installed skill has an active source", "ok")],
            show_header=True,
        )
        print_rollup(ok=1, check=0, miss=0)
        return 0

    print_table(rows, show_header=True)
    print()

    if report.applied:
        if report.removed:
            print(f"  Removed {len(report.removed)} skill(s): {', '.join(report.removed)}")
        else:
            print("  Nothing removed.")
        # What is still there, not every manual candidate: the ones named on
        # the command line have just been removed, and counting them here said
        # so on the line directly under the one listing them as removed.
        left = report.manual_left
        if left and not include_manual:
            print(
                f"  {_c(str(len(left)), YELLOW)} manual skill(s) left in place; "
                "rerun with --include-manual to remove them too."
            )
        return 0

    removable = len(report.removable)
    if removable:
        print(
            f"  {_c(str(removable), YELLOW)} skill(s) would be removed. Rerun with --yes to apply."
        )
    if report.manual:
        print(
            f"  {_c(str(len(report.manual)), DIM)} manual skill(s) are user-placed and are "
            "never removed unless --include-manual is given."
        )
    return 0


def print_manual_skill_removal_report(report) -> int:
    """Render a selective manual-skill preview or applied result."""
    print_header("Remove Manual Skills", "Agentbot › Remove Manual Skills")
    if not report.candidates:
        print_table(
            [("Skill store", "no removable manual skills found", "ok")],
            show_header=True,
        )
        print_rollup(ok=1, check=0, miss=0)
        return 0

    print_table(
        [(item.name, item.detail, item.reason) for item in report.candidates],
        show_header=True,
    )
    print()
    if report.applied:
        print(f"  Removed {len(report.removed)} skill(s): {', '.join(report.removed)}")
    else:
        print(
            f"  {_c(str(len(report.manual)), DIM)} removable manual skill(s); "
            "select exact skill names and rerun with --yes."
        )
    return 0


def print_vscode_report(report) -> None:
    """Render a VS Code preview or applied run.

    Hosts that could not be resolved are shown rather than omitted: "no Windows
    profile" is the answer to "why did nothing happen there", and hiding it
    turns a reported skip into an apparent no-op.
    """
    print_header("VS Code", "Agentbot \u203a VS Code")
    if report.manifest_error:
        print_table([("Manifest", report.manifest_error, "check")])
        print()
        return

    # One rollup for the whole surface, not one per table: the operator asks
    # "does VS Code need me?", and three separate answers do not add up to that.
    totals = [0, 0, 0]

    def _tally(counts: tuple[int, int, int]) -> None:
        for index, value in enumerate(counts):
            totals[index] += value

    _tally(
        print_table(
            [
                (
                    f"{name} host",
                    host.detail if host.available else f"unavailable - {host.detail}",
                    "ok" if host.available else "skipped",
                )
                for name, host in sorted(report.hosts.items())
            ]
        )
    )

    extension_rows: list[tuple[str, str, str]] = []
    for name, plan in sorted(report.extensions.items()):
        if plan.skipped:
            extension_rows.append((name, plan.skipped, "skipped"))
            continue
        outcomes = report.installed.get(name, {})
        if outcomes:
            extension_rows.extend(
                (name, f"{identifier}: {detail}", "ok" if detail == "installed" else "check")
                for identifier, detail in sorted(outcomes.items())
            )
        elif plan.is_noop:
            extension_rows.append(
                (name, f"nothing to install; {len(plan.unmanaged)} unmanaged left alone", "ok")
            )
        else:
            extension_rows.append((name, f"{len(plan.missing)} to install", "check"))
    if extension_rows:
        print()
        print_section("\u2500\u2500 Extensions \u2500\u2500")
        _tally(print_table(extension_rows))

    settings_rows: list[tuple[str, str, str]] = []
    for scope, plan in sorted(report.settings.items()):
        if plan.skipped:
            # The same verdict the Extensions table gives the same host.
            settings_rows.append((scope, plan.skipped, "skipped"))
        elif plan.unreadable:
            settings_rows.append((scope, plan.unreadable, "check"))
        elif plan.is_noop:
            settings_rows.append((scope, "current", "ok"))
        else:
            summary = f"{len(plan.additions)} to add, {len(plan.changes)} to change"
            settings_rows.append((scope, f"{summary} in {plan.path}", "check"))
    if settings_rows:
        print()
        print_section("\u2500\u2500 Settings \u2500\u2500")
        _tally(print_table(settings_rows))

    print_rollup(ok=totals[0], check=totals[1], miss=totals[2])
