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
    configured = os.environ.get("AGENTBOT_MENU_COLS", "")
    if configured.isdigit():
        return max(20, int(configured))
    if os.environ.get("AGENTBOT_TUI"):
        return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)
    return 80


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


def print_header(title: str, breadcrumb: str = "") -> None:
    width = terminal_columns() - 2
    print()
    print(f"  {_c(_fit_line(f'=== {title} ===', width), BOLD + ORANGE)}")
    if breadcrumb:
        print(f"  {_c(_fit_line(breadcrumb, width), DIM)}")
    print()


def print_section(label: str) -> None:
    print(f"  {_c(_fit_line(label, terminal_columns() - 2), BOLD + YELLOW)}")


def color_result(result: str) -> str:
    key = result.strip().lower()
    if key in {"ok", "installed", "configured", "linked", "up to date", "current", "applied", "read-only"}:
        return _c(result, GREEN)
    if key in {"missing", "failed", "error", "conflict"}:
        return _c(result, RED)
    if key.startswith("skipped") or key in {
        "check",
        "warn",
        "warning",
        "partial",
        "drift",
        "extra",
        "applied-with-local-changes",
        "mutating",
    }:
        return _c(result, YELLOW)
    if key in {"info", "dry-run", "preview"}:
        return _c(result, CYAN)
    return result


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
        key = result.strip().lower()
        if key in {"ok", "installed", "configured", "linked", "up to date", "current"}:
            ok_count += 1
        elif key in {"missing", "failed", "error"}:
            miss_count += 1
        elif key.startswith("skipped"):
            continue
        else:
            check_count += 1
    return ok_count, check_count, miss_count


def print_rollup(*, ok: int, check: int, miss: int) -> None:
    print()
    if miss == 0 and check == 0:
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
        print_section_block(section.label)
        ok, check, miss = print_table(list(section.rows), show_header=False)
        total_ok += ok
        total_check += check
        total_miss += miss
    print_rollup(ok=total_ok, check=total_check, miss=total_miss)
    return total_ok, total_check, total_miss
