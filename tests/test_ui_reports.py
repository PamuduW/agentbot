"""The report printers, exercised for content and exit codes.

These produce everything the user actually reads, and each returns the exit
code its command propagates, so a wrong branch here is a wrong exit status.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from src.graphify import GraphifyStatus
from src.models import DoctorIssue
from src.skill_prune import PruneCandidate, PruneReport
from src.skill_reconcile import ReconcileResult
from src.skills_installer import InstallResult
from src.ui import (
    print_command_help,
    print_doctor_summary,
    print_graphify_status,
    print_manual_skill_removal_report,
    print_reconciliation_report,
    print_skill_prune_report,
    print_skills_report,
    print_skills_update_report,
    print_status_summary,
    print_workspace_list,
    print_workspace_removed,
    print_workspace_report,
    print_workspace_resync_report,
)
from src.ui.reports import integration_result, workspace_action_result
from src.ui.table import (
    DIM,
    GREEN,
    YELLOW,
    CollapseBlankLines,
    _table_widths,
    color_action,
    print_four_column_table,
    print_header,
    print_note,
    print_rollup,
    print_section_block,
    print_table,
    result_class,
    strip_ansi,
)
from src.workspace_service import WorkspaceReport, WorkspaceResult
from src.workspace_state import WorkspaceRecord


def _header_lines(text: str) -> int:
    """Count rendered column-header lines, not the word "component".

    The header is column-padded, so match on the parsed first field.
    """
    return sum(
        1
        for line in text.splitlines()
        if "|" in line and line.split("|")[0].strip() == "component"
    )


def _capture(fn, *args, **kwargs) -> tuple[str, object]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = fn(*args, **kwargs)
    return buffer.getvalue(), result


class ResponsiveTableTests(unittest.TestCase):
    def test_long_cells_fit_supported_widths_with_unicode_ellipsis(self):
        row = (
            "Git config (credentials + submodules)",
            "/home/pamudu/a/very/long/path/with/Boost action text and an em dash —",
            "refresh-required",
        )
        for columns in (48, 80, 120):
            with self.subTest(columns=columns), mock.patch.dict(
                "os.environ",
                {"AGENTBOT_MENU_COLS": str(columns), "NO_COLOR": "1"},
                clear=False,
            ):
                text, _ = _capture(print_table, [row])
                for line in text.splitlines():
                    self.assertLessEqual(len(strip_ansi(line)), columns)
                self.assertIn("…", text)
                self.assertNotIn("...", text)

    def test_no_color_wins_over_forced_color(self):
        with mock.patch.dict(
            "os.environ", {"NO_COLOR": "1", "FORCE_COLOR": "1"}, clear=False
        ):
            text, _ = _capture(print_table, [("component", "detail", "warning")])
        self.assertNotIn("\033[", text)


class DoctorSummaryTests(unittest.TestCase):
    def test_no_issues_is_success(self):
        text, rc = _capture(print_doctor_summary, [])
        self.assertEqual(rc, 0)
        self.assertIn("Health check", text)

    def test_warnings_alone_do_not_fail(self):
        issues = [DoctorIssue(level="warning", scope="skills", message="a warning")]
        text, rc = _capture(print_doctor_summary, issues)
        self.assertEqual(rc, 0)
        self.assertIn("a warning", text)

    def test_embedded_doctor_section_does_not_repeat_the_column_header(self):
        # print_section_block already emits the column header; asking
        # print_table for one too printed "component | detail | result" twice
        # under `status --doctor`.
        issues = [DoctorIssue(level="warning", scope="skills", message="a warning")]
        embedded, _ = _capture(print_doctor_summary, issues, include_header=False)
        self.assertEqual(1, _header_lines(embedded))

        standalone, _ = _capture(print_doctor_summary, issues, include_header=True)
        self.assertEqual(1, _header_lines(standalone))

    def test_empty_doctor_section_does_not_repeat_the_column_header(self):
        embedded, _ = _capture(print_doctor_summary, [], include_header=False)
        self.assertEqual(1, _header_lines(embedded))

    def test_any_error_fails(self):
        issues = [
            DoctorIssue(level="warning", scope="skills", message="a warning"),
            DoctorIssue(level="error", scope="global", message="a real problem"),
        ]
        text, rc = _capture(print_doctor_summary, issues)
        self.assertEqual(rc, 1)
        self.assertIn("a real problem", text)


class SkillsReportTests(unittest.TestCase):
    @staticmethod
    def _result(name: str, *, returncode: int = 0, skipped: bool = False) -> InstallResult:
        return InstallResult(
            source_id=name,
            command=["npx", "skills", "add", name],
            returncode=returncode,
            stdout="",
            stderr="",
            skipped=skipped,
        )

    def test_all_installed_is_success(self):
        text, (rc, _counts) = _capture(
            print_skills_report, [self._result("alpha")], title="Skills install"
        )
        self.assertEqual(rc, 0)
        self.assertIn("alpha", text)

    def test_any_failed_source_fails_the_command(self):
        # A partial install is a failure: the machine is left without skills
        # the user asked for, so the command must not exit 0.
        results = [self._result("alpha"), self._result("beta", returncode=1)]
        text, (rc, _counts) = _capture(print_skills_report, results, title="Skills install")
        self.assertEqual(rc, 1)
        self.assertIn("failed", text)

    def test_total_failure_fails_the_command(self):
        results = [self._result("beta", returncode=1)]
        text, (rc, _counts) = _capture(print_skills_report, results, title="Skills install")
        self.assertEqual(rc, 1)
        self.assertIn("failed", text)

    def test_skipped_sources_alone_are_not_a_failure(self):
        results = [self._result("alpha"), self._result("beta", skipped=True)]
        _text, (rc, _counts) = _capture(print_skills_report, results, title="Skills install")
        self.assertEqual(rc, 0)

    def test_empty_report_still_renders_a_header(self):
        text, (rc, _counts) = _capture(print_skills_report, [], title="Skills install")
        self.assertEqual(rc, 0)
        self.assertIn("Skills install", text)

    def test_update_report_lists_updated_and_deleted_skills(self):
        text, rc = _capture(
            print_skills_update_report,
            linked=2,
            skipped=1,
            updated=3,
            updated_skills=("alpha",),
            upstream_deleted_skills=("beta",),
        )
        self.assertEqual(rc, 0)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)


class ManualSkillRemovalReportTests(unittest.TestCase):
    @staticmethod
    def _report(*, applied: bool = False) -> PruneReport:
        candidate = PruneCandidate(
            name="gpt-taste",
            reason="manual",
            detail="on disk, not in the lock; user-placed",
            directory=Path("/tmp/gpt-taste"),
            locked=False,
        )
        return PruneReport(
            candidates=(candidate,),
            removed=("gpt-taste",) if applied else (),
            applied=applied,
        )

    def test_preview_explains_selective_removal_without_broad_prune_flag(self):
        """Break caught: the safer command tells users to run broad --include-manual."""
        text, rc = _capture(print_manual_skill_removal_report, self._report())

        self.assertEqual(0, rc)
        self.assertIn("Remove Manual Skills", text)
        self.assertIn("select exact skill names and rerun with --yes", text)
        self.assertNotIn("--include-manual", text)

    def test_applied_report_names_exactly_what_was_removed(self):
        text, rc = _capture(
            print_manual_skill_removal_report,
            self._report(applied=True),
        )

        self.assertEqual(0, rc)
        self.assertIn("Removed 1 skill(s): gpt-taste", text)


class SkillPruneReportTests(unittest.TestCase):
    @staticmethod
    def _manual(*names) -> tuple:
        return tuple(
            PruneCandidate(
                name=name,
                reason="manual",
                detail="on disk, not in the lock; user-placed",
                directory=Path("/x") / name,
                locked=False,
            )
            for name in names
        )

    def test_removed_manual_skills_are_not_also_reported_as_left_in_place(self):
        """The menu prunes manual skills by naming them, and they go.

        `manual` is every manual candidate, removed or not, so reporting it
        after applying printed

            Removed 6 skill(s): docker-patterns, ...
            6 manual skill(s) left in place; rerun with --include-manual ...

        naming the same six on both lines.
        """
        report = PruneReport(
            candidates=self._manual("alpha", "beta"),
            removed=("alpha", "beta"),
            applied=True,
        )

        text, _rc = _capture(print_skill_prune_report, report)

        self.assertIn("Removed 2 skill(s): alpha, beta", text)
        self.assertNotIn("left in place", text)

    def test_manual_skills_that_really_were_left_are_still_reported(self):
        """The message earns its place when something actually stayed."""
        report = PruneReport(
            candidates=self._manual("alpha", "beta"),
            removed=("alpha",),
            applied=True,
        )

        text, _rc = _capture(print_skill_prune_report, report)

        self.assertIn("Removed 1 skill(s): alpha", text)
        self.assertIn("1 manual skill(s) left in place", text)

    def test_blocked_plan_reports_the_invalid_lock_as_an_error(self):
        """Break caught: blocked pruning is displayed as a healthy empty plan."""
        report = PruneReport(
            candidates=(), blocked_reason="invalid global skill lock: malformed JSON"
        )

        text, rc = _capture(print_skill_prune_report, report)

        self.assertEqual(1, rc)
        self.assertIn("invalid global skill lock", text)
        self.assertNotIn("every installed skill has an active source", text)


class GraphifyStatusTests(unittest.TestCase):
    @staticmethod
    def _status(state: str, message: str = "") -> GraphifyStatus:
        return GraphifyStatus(
            state=state,
            cli_path=Path("/usr/bin/graphify"),
            cli_version="1.2.3",
            skill_path=Path("/tmp/skill"),
            skill_version="1.0.0",
            codex_state="ready",
            claude_state="ready",
            message=message,
        )

    def test_each_state_renders_without_error(self):
        for state in ("ready", "stale", "conflict", "broken", "absent"):
            text, _ = _capture(print_graphify_status, self._status(state))
            self.assertIn("Graphify", text)

    def test_a_message_is_shown(self):
        text, _ = _capture(print_graphify_status, self._status("broken", "cli missing"))
        self.assertIn("cli missing", text)


class IntegrationStateTests(unittest.TestCase):
    """Every state boost and graphify can report, not just today's.

    The surface sweep in tests/test_report_contract.sh reads rendered output, so
    it only ever sees the state the machine running it happens to be in. These
    states are read out of the source instead, so `forbidden` is covered on a
    machine that has never been forbidden.
    """

    @staticmethod
    def _states_assigned_in(module: str) -> set[str]:
        import ast

        source = Path(module).read_text(encoding="utf-8")
        found: set[str] = set()
        for node in ast.walk(ast.parse(source, filename=module)):
            if isinstance(node, ast.keyword) and node.arg == "state":
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if not (isinstance(target, ast.Name) and target.id == "state"):
                        continue
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.add(value.value)
        return found

    def _states(self) -> set[str]:
        states = self._states_assigned_in("src/boost.py") | self._states_assigned_in(
            "src/graphify.py"
        )
        self.assertIn("unsafe-config", states, "the reader stopped finding states")
        return states

    def test_every_state_has_a_considered_verdict(self):
        """Membership, not just a valid answer.

        integration_result falls back to `check` so an unmapped state cannot
        break a report at runtime. That makes asserting on its return value
        toothless: every state passes whether or not anyone thought about it.
        The map is what has to be complete.
        """
        from src.ui.reports import _INTEGRATION_RESULTS

        for state in sorted(self._states()):
            with self.subTest(state=state):
                self.assertIn(state, _INTEGRATION_RESULTS)

    def test_every_verdict_is_a_word_the_vocabulary_knows(self):
        for state in sorted(self._states()):
            with self.subTest(state=state):
                self.assertNotEqual(
                    "unknown",
                    result_class(integration_result(state)),
                    f"{state!r} produces a result the vocabulary cannot colour or count",
                )

    def test_every_state_maps_to_a_word_that_fits_the_column(self):
        """`unsafe-config` reached the result cell as `unsafe-co…`.

        The detail column is wide and the result column is not, and these rows
        were putting the same string in both.
        """
        width = _table_widths()[2]
        for state in sorted(self._states()):
            with self.subTest(state=state):
                self.assertLessEqual(len(integration_result(state)), width)


class NoteTests(unittest.TestCase):
    def test_a_long_note_keeps_the_margin_on_every_line(self):
        """Printed as one line it soft-wraps to column zero.

        The closing note on `boost status` lists twelve paths, and was the one
        block on the screen whose continuation lines did not start where the
        table does.
        """
        sentence = " ".join(f"/a/long/path/number-{index}.sh," for index in range(20))
        text, _ = _capture(print_note, sentence)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertGreater(len(lines), 1, "the sample was not long enough to wrap")
        for line in lines:
            self.assertTrue(line.startswith("  "), line)
            self.assertFalse(line.startswith("   "), line)

    def test_a_short_note_is_one_line(self):
        text, _ = _capture(print_note, "all good")
        self.assertEqual(["  all good"], [x for x in text.splitlines() if x.strip()])


class BlankLineTests(unittest.TestCase):
    """No two blank lines in a row, anywhere a command prints."""

    def _collapsed(self, chunks) -> str:
        buffer = io.StringIO()
        writer = CollapseBlankLines(buffer)
        for chunk in chunks:
            writer.write(chunk)
        writer.flush()
        return buffer.getvalue()

    def test_a_run_of_blanks_becomes_one(self):
        self.assertEqual(
            "one\n\ntwo\n", self._collapsed(["one\n", "\n", "\n", "   \n", "two\n"])
        )

    def test_an_opening_blank_is_kept(self):
        """The header's leading blank is the gap under the frame above it."""
        self.assertEqual("\nheader\n", self._collapsed(["\n", "header\n"]))

    def test_a_trailing_blank_is_dropped(self):
        """The menu's "Press Enter to continue:" prints its own leading blank.

        That prompt comes from Bash, after this process has exited, so no
        wrapper can see both sides. Python ended its output on a blank too and
        the screen showed two. ui_pause is shared byte-for-byte with the sibling
        repository, so this side stops contributing one.
        """
        self.assertEqual("last\n", self._collapsed(["last\n", "\n"]))
        self.assertEqual("last\n", self._collapsed(["last\n", "\n", "\n", "  \n"]))

    def test_a_blank_between_content_survives(self):
        """Dropping trailing blanks must not drop the gaps inside."""
        self.assertEqual(
            "one\n\ntwo\n", self._collapsed(["one\n", "\n", "two\n", "\n"])
        )

    def test_a_row_written_in_two_calls_stays_one_line(self):
        """print_table writes a row as a cell with end="" and then the rest."""
        self.assertEqual("  a | b | ok\n", self._collapsed(["  a | b | ", "ok\n"]))

    def test_a_partial_line_survives_a_flush(self):
        buffer = io.StringIO()
        writer = CollapseBlankLines(buffer)
        writer.write("no newline yet")
        writer.flush()
        self.assertEqual("no newline yet", buffer.getvalue())

    def test_the_wrapper_answers_isatty_for_the_stream_it_wraps(self):
        """use_color() and the installer's progress gate both ask."""
        buffer = io.StringIO()
        self.assertEqual(buffer.isatty(), CollapseBlankLines(buffer).isatty())

    def test_stacked_report_blocks_leave_one_gap(self):
        """Two blocks meeting is where the doubles came from.

        Each block opens with a blank so it separates itself from what came
        before, and several close with one too. An install printed five double
        gaps: after the last progress line, between a heading and its first
        section, and after every rollup a heading or section followed.
        """
        buffer = io.StringIO()
        writer = CollapseBlankLines(buffer)
        with redirect_stdout(writer):
            print_rollup(ok=1, check=0, miss=0)
            print_header("Graphify", "Agentbot › Graphify")
            print_section_block("── Sources ──")
        writer.flush()
        rendered = strip_ansi(buffer.getvalue())
        self.assertNotIn("\n\n\n", rendered)


class FourColumnTests(unittest.TestCase):
    """The plan and report layout, shared with the Bash renderer's widths."""

    def _render(self, rows) -> list[str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            counts = print_four_column_table(rows)
        return [strip_ansi(line) for line in buffer.getvalue().splitlines()], counts

    def test_every_line_is_the_terminal_width(self):
        """Header, rule and rows, as the three-column table already guarantees."""
        lines, _ = self._render([("Skills", "62", "12 source(s)", "reconcile")])
        self.assertEqual(1, len({len(line) for line in lines}), lines)

    def test_the_action_column_is_counted_not_the_detail(self):
        """The last column says what will happen, and that is what a rollup of
        this table would be counting."""
        _, counts = self._render(
            [
                ("Skills", "62", "none", "up to date"),
                ("Graphify", "0.9.58", "—", "refresh"),
            ]
        )
        self.assertEqual((1, 1, 0), counts)

    def test_actions_use_the_action_vocabulary_not_the_result_one(self):
        """`current` is green as an action and yellow as a result.

        Two vocabularies on purpose: one says what something *is*, the other
        what will *happen* to it.
        """
        # Forced here rather than left to the environment: the rest of this
        # file asserts on plain text, and a suite-wide FORCE_COLOR would break
        # every one of those instead.
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}):
            self.assertIn(GREEN, color_action("up to date"))
            self.assertIn(YELLOW, color_action("refresh"))
            self.assertIn(DIM, color_action("latest unchecked"))
            # And a word neither vocabulary knows is left alone rather than
            # guessed at.
            self.assertEqual("invented", color_action("invented"))


class RollupTests(unittest.TestCase):
    def test_an_empty_surface_says_nothing_was_inspected(self):
        """Not the same as everything being fine.

        `cli-config status` on a machine with none of the three CLIs counted
        three skips, tallied nothing, and closed "All 0 component(s) look
        good." -- a verdict on work that never happened.
        """
        text, _ = _capture(print_rollup, ok=0, check=0, miss=0)
        self.assertIn("Nothing to report.", strip_ansi(text))

    def test_a_healthy_surface_still_says_so(self):
        text, _ = _capture(print_rollup, ok=3, check=0, miss=0)
        self.assertIn("All 3 component(s) look good.", strip_ansi(text))

    def test_empty_workspaces_still_closes_on_a_rollup(self):
        text, _ = _capture(print_workspace_list, [])
        last = [line for line in strip_ansi(text).splitlines() if line.strip()][-1]
        self.assertEqual("  Nothing to report.", last)


class EmbeddedResyncTests(unittest.TestCase):
    """The resync renders standalone or inside a larger surface.

    `agentbot resync` is its own screen and keeps its heading and its rollup.
    The update run embeds it, where a second `=== Workspace Resync ===` in the
    middle of the result split one outcome across two screens, and a rollup
    counting only the resync read as the verdict on the whole update.
    """

    @staticmethod
    def _report() -> WorkspaceReport:
        return WorkspaceReport(
            results=(
                WorkspaceResult(
                    path=Path("/tmp/ws"), status="applied", actions=(), message="done"
                ),
            ),
            global_actions=(),
        )

    def test_standalone_keeps_its_heading_and_rollup(self):
        text, _ = _capture(print_workspace_resync_report, self._report())
        rendered = strip_ansi(text)
        self.assertIn("=== Workspace Resync ===", rendered)
        self.assertRegex(rendered, r"(\d+ ok|All \d+ component)")

    def test_embedded_drops_both_and_returns_its_counts(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            counts = print_workspace_resync_report(
                self._report(), include_header=False, include_rollup=False
            )
        rendered = strip_ansi(buffer.getvalue())
        self.assertNotIn("=== Workspace Resync ===", rendered)
        self.assertNotRegex(rendered, r"(\d+ ok|All \d+ component)")
        # Handed back so the surface that embedded it can close on one rollup.
        self.assertEqual(3, len(counts))
        self.assertEqual(1, sum(counts))


class WorkspaceActionResultTests(unittest.TestCase):
    """Every render-action kind the type declares, in both run modes.

    `create` and `update` went into the result cell raw, and neither is in the
    vocabulary: previewing a new workspace showed two uncoloured `create` rows
    and closed "0 ok, 2 need attention." over a plan in which nothing was wrong.
    """

    @staticmethod
    def _kinds() -> tuple[str, ...]:
        """Read off RenderAction.kind, so a new kind is covered when it is added."""
        import ast

        source = Path("src/workspace_render.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source, filename="src/workspace_render.py")):
            if not isinstance(node, ast.AnnAssign):
                continue
            if not (isinstance(node.target, ast.Name) and node.target.id == "kind"):
                continue
            annotation = node.annotation
            if isinstance(annotation, ast.Subscript):
                return tuple(
                    element.value
                    for element in ast.walk(annotation.slice)
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
        raise AssertionError("RenderAction.kind is no longer a Literal of strings")

    def test_the_declared_kinds_are_still_the_four_expected(self):
        self.assertEqual(
            {"create", "update", "unchanged", "conflict"}, set(self._kinds())
        )

    def test_every_kind_maps_into_the_vocabulary_in_both_modes(self):
        width = _table_widths()[2]
        for kind in self._kinds():
            for applied in (False, True):
                with self.subTest(kind=kind, applied=applied):
                    word = workspace_action_result(kind, applied=applied)
                    self.assertNotEqual("unknown", result_class(word))
                    self.assertLessEqual(len(word), width)

    def test_a_write_reads_as_a_plan_before_it_happens_and_a_result_after(self):
        self.assertEqual("preview", workspace_action_result("create", applied=False))
        self.assertEqual("applied", workspace_action_result("create", applied=True))
        # A conflict is a conflict either way.
        self.assertEqual("conflict", workspace_action_result("conflict", applied=True))


class WorkspaceReportTests(unittest.TestCase):
    @staticmethod
    def _result(status: str) -> WorkspaceResult:
        return WorkspaceResult(
            path=Path("/tmp/ws"), status=status, actions=(), message="a message"
        )

    def test_single_workspace_report_shows_path_and_message(self):
        text, _ = _capture(print_workspace_report, self._result("preview"))
        self.assertIn("/tmp/ws", text)
        self.assertIn("a message", text)

    def test_workspace_surfaces_close_on_the_rollup(self):
        """The rollup is the last line, and the message explains above it.

        These three printed a table and a sentence and stopped. The contract
        puts one line at the bottom for "does this need me?", and a surface
        that ends on prose does not have one.
        """
        report = WorkspaceReport(results=(self._result("preview"),), global_actions=())
        for printer, argument in (
            (print_workspace_report, self._result("preview")),
            (print_workspace_resync_report, report),
        ):
            with self.subTest(printer=printer.__name__):
                text, _ = _capture(printer, argument)
                last = [line for line in text.splitlines() if line.strip()][-1]
                self.assertRegex(last, r"^  (\d+ ok|All \d+ component)")

    def test_a_single_group_draws_no_section_rule(self):
        """A rule earns its place by separating things."""
        text, _ = _capture(print_workspace_report, self._result("preview"))
        self.assertNotIn("──", text)

    def test_resync_report_covers_each_status(self):
        report = WorkspaceReport(
            results=tuple(
                self._result(status) for status in ("applied", "preview", "conflict", "failed")
            ),
            global_actions=(),
        )
        text, _ = _capture(print_workspace_resync_report, report)
        self.assertIn("/tmp/ws", text)

    def test_empty_resync_report_renders(self):
        text, _ = _capture(
            print_workspace_resync_report, WorkspaceReport(results=(), global_actions=())
        )
        self.assertIsInstance(text, str)

    def test_removed_workspace_names_the_path(self):
        record = WorkspaceRecord(
            path="/tmp/ws",
            kind="git",
            policy_mode="managed",
            profile="default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at="2026-01-01T00:00:00Z",
        )
        text, _ = _capture(print_workspace_removed, record)
        self.assertIn("/tmp/ws", text)


class ReconciliationReportTests(unittest.TestCase):
    def _render(self, status: str) -> str:
        text, _ = _capture(
            print_reconciliation_report,
            ReconcileResult(status, (Path("a.md"), Path("b.md")), (), ("newskill",)),
        )
        return strip_ansi(text)

    def test_counts_move_out_of_the_result_column(self):
        """A number is not a result.

        The counts sat in the result cell, uncoloured beside coloured siblings
        and read as attention items by anything tallying the table. They belong
        with the thing they count.
        """
        text = self._render("applied")
        self.assertIn("2: a.md, b.md", text)
        for line in text.splitlines():
            if "|" not in line:
                continue
            result = line.rsplit(" | ", 1)[-1].strip()
            header_or_rule = result == "result" or set(result) <= set("-+ ")
            if not result or header_or_rule or line.rstrip().endswith("|"):
                continue
            self.assertNotRegex(result, r"^\d+$")
            self.assertNotEqual("unknown", result_class(result))

    def test_a_long_status_stays_out_of_a_short_column(self):
        """`applied-with-local-changes` is 26 characters; the column is 10."""
        text = self._render("applied-with-local-changes")
        self.assertIn("applied-with-local-changes", text)
        self.assertNotIn("applied-w\u2026", text)

    def test_a_failure_still_reads_as_one(self):
        self.assertRegex(self._render("failed"), r"\| failed")

    def test_changes_are_listed(self):
        result = ReconcileResult(
            status="applied",
            changed_paths=(Path("/tmp/alpha"),),
            removed_skills=("beta",),
            added_skills=("alpha",),
            message="done",
            updated_skills=("gamma",),
        )
        text, _ = _capture(print_reconciliation_report, result)
        self.assertIn("alpha", text)


class StatusSummaryTests(unittest.TestCase):
    def test_summary_reports_each_component(self):
        text, _ = _capture(
            print_status_summary,
            installed_skills=5,
            global_agents_exists=True,
            skills_sources_exists=True,
            enabled_sources=3,
            global_lock_exists=True,
            global_lock_skills=4,
            claude_bridge_links=5,
            claude_statusline_state="ok",
            manual_skill_count=0,
            doctor_issue_count=0,
        )
        self.assertIn("Check Status", text)

    def test_missing_pieces_are_visible(self):
        text, _ = _capture(
            print_status_summary,
            installed_skills=0,
            global_agents_exists=False,
            skills_sources_exists=False,
            enabled_sources=0,
            global_lock_exists=False,
            global_lock_skills=0,
            claude_bridge_links=0,
            claude_statusline_state="missing",
            manual_skill_count=2,
            doctor_issue_count=3,
        )
        self.assertIn("Check Status", text)
        self.assertIn("Prunable skills", text)
        self.assertNotIn("Manual skills", text)


class CommandHelpTests(unittest.TestCase):
    def test_full_catalog_lists_both_surfaces(self):
        text, _ = _capture(print_command_help)
        self.assertIn("Commands", text)
        self.assertIn("Bootstrap commands", text)

    def test_a_single_command_renders_its_detail(self):
        from src.commands import command_by_name

        text, _ = _capture(print_command_help, command_by_name("status"))
        self.assertIn("status", text)


if __name__ == "__main__":
    unittest.main()
