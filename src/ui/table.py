"""Terminal primitives: colour, width maths, tables, and rollups.

Domain report printers live in src/ui/reports.py.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

from ..models import Table

# The layout itself is shared with the Bash renderer's repository through
# scripts/lib/shared/, which sync-shared.sh holds byte-identical. Loaded by path
# because that tree is deliberately outside the package: it is vendored code,
# not part of src/.
_SHARED_PY = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "shared" / "python"
if str(_SHARED_PY) not in sys.path:
    sys.path.insert(0, str(_SHARED_PY))
import report_table as _shared  # noqa: E402

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
ORANGE = "\033[38;5;208m"

# Narrower than this and the three columns have nothing left to show; the Bash
# renderer floors here too.
MINIMUM_COLUMNS = 32

LABEL_W = 22
DETAIL_W = 40
RESULT_W = 10

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MANUAL_SKILL_NAME = re.compile(r"(?<=Manual skill ')[^']+(?=')")


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("AGENTBOT_TUI"):
        return True
    # Match dotfiles report_table: stdin may still be a TTY when stdout is piped (e.g. tee).
    return sys.stdout.isatty() or sys.stdin.isatty()


def _c(text: str, code: str) -> str:
    if not use_color():
        return text
    return f"{code}{text}{RESET}"


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def terminal_columns() -> int:
    """The same width rt_report_columns settles on, by the same steps.

    It used to consult the terminal only when AGENTBOT_TUI was set and to sit at
    80 otherwise, while the Bash renderer honoured COLUMNS and the terminal
    whatever it was called from. A `dotfiles full-update` prints tables from
    both, so on a machine that exports COLUMNS the two came out different widths
    in one session -- which is the drift the parity suite exists to catch, and
    it only caught it once the suite was run with COLUMNS set.

    `shutil.get_terminal_size` reads COLUMNS first and then the terminal, which
    is the Bash order; the floor matches too.
    """
    configured = os.environ.get("AGENTBOT_MENU_COLS", "")
    if configured.isdigit():
        return max(MINIMUM_COLUMNS, int(configured))
    return max(MINIMUM_COLUMNS, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _fit_line(text: str, width: int) -> str:
    return _shared.fit_line(text, width)


def shorten_path(text: str, max_len: int) -> str:
    return _shared.shorten_path(text, max_len, os.path.expanduser("~"))


def _table_widths() -> tuple[int, int, int]:
    """One formula at every width, in both languages.

    The non-interactive case used to return fixed constants instead, which put
    the column boundary one place left of the Bash renderer in
    scripts/lib/shared/tui/report_table.sh: 22/40/10 against 21/41/10. Both are
    80 columns wide, so nothing overflowed -- the two tools just did not line up
    when a `dotfiles full-update` printed them one after the other.

    Every other case already agreed. Measured across 48, 80 and 120 columns,
    both implementations return 12/21/7, 21/41/10 and 33/64/15 whenever a width
    is known, so deriving the default from the same formula at the same 80
    column fallback removes the only divergence.
    """
    return _shared.three_column_widths(max(32, terminal_columns()))


def shorten_detail(text: str, *, max_len: int | None = None) -> str:
    if max_len is None:
        max_len = _table_widths()[1]
    cleaned = strip_ansi(text).replace("\r", "")
    for line in cleaned.splitlines():
        line = line.strip()
        if not line or line == "skills" or "████" in line:
            continue
        if len(line) > max_len:
            return f"{line[: max_len - 1]}…"
        return line
    compact = " ".join(part for part in cleaned.split() if part)
    if len(compact) > max_len:
        return f"{compact[: max_len - 1]}…"
    return compact


class CollapseBlankLines:
    """A stdout wrapper that never writes two blank lines in a row.

    Every block here opens with a blank line so it separates itself from
    whatever came before, and several close with one as well. That is fine in
    isolation and wrong the moment two blocks meet: an install printed five
    double gaps, between the last progress line and the first report, between a
    heading and its first section, and after every rollup that a heading or a
    section followed.

    Fixing it at the call sites means every printer knowing what the printer
    before it emitted, and a new adjacency is a new chance to get it wrong.
    Doing it here makes the single blank structural.

    Blank lines are held rather than written, and released only when something
    follows them. That collapses a run to one, and drops the run at the end of
    the output entirely -- which is the other half of the problem, because the
    "Press Enter to continue:" prompt is printed by Bash after this process
    exits and prints its own leading blank. Python ended on one too, so the
    menu showed two. ui_pause is shared byte-for-byte with the sibling
    repository and is not ours to change, so this side stops contributing one.

    A blank that opens the output still survives, released by the header that
    follows it: that gap sits under the menu frame above.

    Writes are buffered to line boundaries because print_table writes a row in
    two calls, the first with `end=""`.
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._pending = ""
        self._held = False

    def _release(self) -> None:
        if self._held:
            self._stream.write("\n")
            self._held = False

    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, _, self._pending = self._pending.partition("\n")
            if not line.strip():
                # At most one ever survives, however many arrive.
                self._held = True
                continue
            self._release()
            self._stream.write(line + "\n")
        return len(text)

    def flush(self) -> None:
        # A partial line is content, so anything held before it is a real gap.
        # Held blanks with nothing after them stay unwritten.
        if self._pending:
            self._release()
            self._stream.write(self._pending)
            self._pending = ""
        self._stream.flush()

    # Delegated because callers ask: use_color() and the skills installer's
    # progress gate both test isatty().
    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return self._stream.encoding

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)


def print_header(title: str, breadcrumb: str = "") -> None:
    width = terminal_columns() - 2
    print()
    print(f"  {_c(_fit_line(f'=== {title} ===', width), BOLD + ORANGE)}")
    if breadcrumb:
        print(f"  {_c(_fit_line(breadcrumb, width), DIM)}")
    print()


def print_section(label: str) -> None:
    print(f"  {_c(_fit_line(label, terminal_columns() - 2), BOLD + YELLOW)}")


# The result vocabulary, in one place, because the rollup counts by it as well
# as colours by it. It used to be written twice -- once here and once inline in
# print_table's counter -- and the two drifted: `applied`, `read-only` and
# `conflict` were coloured but missing from the counter, so rows painted green
# and red were tallied as needing attention. `graphify` was the visible end of
# it, reporting "5 ok, 1 need attention" over six healthy rows and then closing
# with "Graphify CLI and Agent Skills integration are ready."
#
# A word absent from all four sets renders uncoloured beside coloured siblings
# and falls to the counter's catch-all, so it silently becomes an attention
# item. `ready`, `stale` and `unchanged` were each doing that on a public
# surface. tests/test_report_contract.sh sweeps for it now.
RESULT_GREEN = {
    "ok",
    "installed",
    "configured",
    "linked",
    "up to date",
    "current",
    "unchanged",
    "ready",
    "applied",
    "read-only",
}
RESULT_RED = {"missing", "failed", "error", "conflict"}
RESULT_YELLOW = {
    "check",
    "warn",
    "warning",
    "partial",
    "drift",
    "extra",
    "stale",
    "applied-with-local-changes",
    "mutating",
}
# Informational, not actionable. The `Prunable skills` row is written as
# `"info" if manual_skill_count else "ok"`, which says outright that the two are
# the same kind of non-problem; the rollup counted the second as fine and the
# first as needing attention.
RESULT_CYAN = {"info", "dry-run", "preview"}


def result_class(result: str) -> str:
    """Which of the five classes a result word belongs to.

    `unknown` is its own answer rather than a silent fallback: the sweep in
    tests/test_report_contract.sh reads it, so a surface inventing a word is
    caught there instead of on the operator's screen.
    """
    key = result.strip().lower()
    if key in RESULT_GREEN:
        return "ok"
    if key in RESULT_RED:
        return "missing"
    # Dim, not yellow: a skip is a deliberate non-event -- "nothing declared",
    # "host unavailable" -- and should recede rather than demand attention the
    # way a warning does. The Bash renderer has always dimmed it; this side
    # diverged, and only uncoloured rows were being compared so nothing said so.
    if key.startswith("skipped"):
        return "skipped"
    if key in RESULT_YELLOW:
        return "check"
    if key in RESULT_CYAN:
        return "info"
    return "unknown"


_RESULT_COLORS = {"ok": GREEN, "missing": RED, "skipped": DIM, "check": YELLOW, "info": CYAN}


def color_result(result: str) -> str:
    code = _RESULT_COLORS.get(result_class(result))
    return result if code is None else _c(result, code)


def highlight_manual_skill_name(detail: str, line: str) -> str:
    match = MANUAL_SKILL_NAME.search(strip_ansi(detail))
    if match is None:
        return line
    skill_name = match.group(0)
    return line.replace(skill_name, _c(skill_name, BOLD + CYAN))


def print_table_columns(*, headers: tuple[str, str, str] = ("component", "detail", "result")) -> None:
    h0, h1, h2 = headers
    label_width, detail_width, result_width = _table_widths()
    h0 = _fit_line(h0, label_width)
    h1 = _fit_line(h1, detail_width)
    h2 = _fit_line(h2, result_width)
    print(f"  {_c(f'{h0:<{label_width}} | {h1:<{detail_width}} | {h2:<{result_width}}', BOLD)}")
    print(
        f"  {'-' * label_width}-+-{'-' * detail_width}-+-{'-' * result_width}"
    )


def print_table(
    rows: list[tuple[str, str, str]],
    *,
    headers: tuple[str, str, str] = ("component", "detail", "result"),
    show_header: bool = True,
    wrap_details: bool = False,
    detail_highlighter: Callable[[str, str], str] | None = None,
) -> tuple[int, int, int]:
    ok_count = check_count = miss_count = 0
    label_width, detail_width, _result_width = _table_widths()
    if show_header:
        print_table_columns(headers=headers)
    for label, detail, result in rows:
        if wrap_details:
            detail_lines: list[str] = []
            for paragraph in strip_ansi(detail).replace("\r", "").splitlines() or [""]:
                detail_lines.extend(
                    textwrap.wrap(
                        paragraph,
                        width=detail_width,
                        break_long_words=True,
                        break_on_hyphens=False,
                    )
                    or [""]
                )
        elif detail.startswith(("/", "~")):
            detail_lines = [shorten_path(detail, detail_width)]
        else:
            detail_lines = [_fit_line(detail, detail_width)]

        for line_number, detail_fit in enumerate(detail_lines):
            detail_padded = f"{detail_fit:<{detail_width}}"
            if detail_highlighter is not None:
                detail_padded = detail_highlighter(detail, detail_padded)
            if line_number == 0:
                label_fit = _fit_line(label, label_width)
                print(f"  {label_fit:<{label_width}} | {detail_padded} | ", end="")
                # Padded, not bare: the Bash renderer pads this cell too, and a
                # test there requires every table line to be exactly as wide as
                # the terminal.
                result_fit = _fit_line(result, _result_width)
                print(color_result(result_fit) + " " * (_result_width - len(result_fit)))
            else:
                print(f"  {'':<{label_width}} | {detail_padded} |")
        # Counted by the same vocabulary the row was coloured with, never by a
        # second list beside it. `info` joins `ok`: it is something the reader
        # is being told, not something being asked of them. `skipped` is a
        # deliberate non-event and stays out of the tally entirely.
        match result_class(result):
            case "ok" | "info":
                ok_count += 1
            case "missing":
                miss_count += 1
            case "skipped":
                continue
            case _:
                check_count += 1
    return ok_count, check_count, miss_count


def print_rollup(*, ok: int, check: int, miss: int) -> None:
    print()
    if ok == check == miss == 0:
        # Nothing was inspected, which is not the same as everything being
        # fine. `cli-config status` on a machine with none of the three CLIs
        # said "All 0 component(s) look good.", which reads like a verdict on
        # work that never happened. Dim, because it is a non-event.
        print(f"  {_c('Nothing to report.', DIM)}")
    elif miss == 0 and check == 0:
        print(f"  {_c(f'All {ok} component(s) look good.', GREEN)}")
    elif miss == 0:
        print(
            f"  {_c(f'{ok} ok', GREEN)}, "
            f"{_c(f'{check} need attention', YELLOW)}."
        )
    else:
        print(
            f"  {_c(f'{ok} ok', GREEN)}, "
            f"{_c(f'{miss} missing', RED)}, "
            f"{_c(f'{check} need attention', YELLOW)}."
        )
    print()


def print_note(text: str) -> None:
    """A sentence or paragraph beneath a table, wrapped to the same width.

    Printed as one long line it soft-wraps at the terminal edge, and the
    continuation starts at column zero -- so the closing note on `boost status`,
    which lists twelve paths, was the one block on the screen that did not keep
    the margin. Wrapped here, every line of it starts where the table does.
    """
    width = max(MINIMUM_COLUMNS, terminal_columns())
    for line in textwrap.wrap(text, width=width - 2) or [""]:
        print(f"  {line}")


def print_four_column_table(
    rows: list[tuple[str, str, str, str]],
    *,
    headers: tuple[str, str, str, str] = ("component", "installed", "available", "action"),
) -> tuple[int, int, int]:
    """The plan and report layout, in the same widths the Bash renderer uses.

    The layout comes from the shared module rather than from a formula here, so
    a `dotfiles full-update` -- which prints both products' tables into one
    terminal -- lines them up. The action column is coloured by the action
    vocabulary, which says what will *happen*, not by the result vocabulary,
    which says what something *is*: `current` is green in one and yellow in the
    other, and this column means the first.
    """
    widths = _shared.four_column_widths(max(MINIMUM_COLUMNS, terminal_columns()))
    columns, rule = _shared.format_four_column_header(widths, headers)
    print(_c(columns, BOLD))
    print(_c(rule, DIM))
    ok = check = miss = 0
    for cells in rows:
        line = _shared.format_four_column_row(widths, cells)
        print(_color_action_cell(line, cells[3], widths))
        match result_class(cells[3]):
            case "ok" | "info":
                ok += 1
            case "missing":
                miss += 1
            case "skipped":
                continue
            case _:
                check += 1
    return ok, check, miss


#: What will happen, as opposed to what something is. `current` is green here
#: and yellow in the result vocabulary, which is why the two are separate.
ACTION_GREEN = {"up to date", "skip", "current", "verified current", "none"}
ACTION_YELLOW = {"refresh", "continue", "check", "unchecked", "reconcile", "configure"}
ACTION_CYAN = {"verified", "install", "apply", "merge"}


def color_action(action: str) -> str:
    key = action.strip().lower()
    if key in ACTION_GREEN:
        return _c(action, GREEN)
    if key in ACTION_CYAN:
        return _c(action, CYAN)
    if key in ACTION_YELLOW or key.startswith(("upgrade", "replace", "refresh")):
        return _c(action, YELLOW)
    if key.startswith("latest "):
        return _c(action, DIM)
    return action


def _color_action_cell(line: str, action: str, widths) -> str:
    """Paint the last cell in place, leaving the padding uncoloured.

    The row is built by the shared layout so both renderers agree byte for
    byte; colour goes on afterwards, over the text only, or the padding would
    carry escape bytes the width maths never counted.
    """
    fitted = _shared.fit_line(action, widths[3])
    if not fitted:
        return line
    painted = color_action(fitted)
    return line.replace(fitted, painted, 1) if painted != fitted else line


def format_shortcuts(*pairs: str) -> str:
    """`c confirm   e edit   q back`, keys lit, exactly as the Bash helper draws.

    Mirrors ui_format_shortcuts in scripts/lib/shared/tui/ui.sh: three spaces
    between pairs, the key in cyan and the label plain. The two products put
    these prompts in the same terminal, so they are the same shape.
    """
    if len(pairs) % 2:
        raise ValueError("shortcuts take key/label pairs")
    return "   ".join(
        f"{_c(key, CYAN)} {label}" for key, label in zip(pairs[::2], pairs[1::2], strict=True)
    )


def print_section_block(label: str) -> None:
    print()
    print_section(label)
    print()
    print_table_columns()


def table_rows(table: Table) -> list[dict[str, str]]:
    return [
        {"section": section.label, "component": component, "detail": detail, "result": result}
        for section in table.sections
        for component, detail, result in section.rows
    ]


def print_table_model(table: Table) -> tuple[int, int, int]:
    print_header(table.title, table.breadcrumb)
    total_ok = total_check = total_miss = 0
    for section in table.sections:
        # An unlabelled section draws no rule. The L10 contract gives a rule to
        # a surface with two or more groups; a single group is its own section
        # and a rule above it separates nothing.
        if section.label:
            print_section_block(section.label)
        ok, check, miss = print_table(
            list(section.rows), show_header=not section.label
        )
        total_ok += ok
        total_check += check
        total_miss += miss
    print_rollup(ok=total_ok, check=total_check, miss=total_miss)
    return total_ok, total_check, total_miss
