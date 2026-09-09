"""Checkbox menu frames.

The second menu shape: a list where every row is on or off, with a status
column, paging, and a footer. Frames only, as with the simple menu -- the loop
that reads keys and toggles is still Bash, and this is the half a parity oracle
can reach.

Two things here are fiddlier than they look, and both are why the port is worth
byte-comparing rather than eyeballing:

* Four colour combinations per row, twice. Checked and unchecked cross selected
  and not, and the head and the label are painted separately so a dim unchecked
  row still shows a bold cursor. Getting one of the eight wrong is invisible
  until an operator is looking at that exact row in that exact state.
* The label's width is what is left after the index, the mark, the separators
  and the status column -- and the status column is as wide as the widest
  status, not a constant. A label fitted against the wrong remainder wraps the
  row and pushes the frame out of step with the redraw height.
"""

from __future__ import annotations

from .menu import (
    CLEAR_EOL,
    DESC_LINES,
    Palette,
    description_lines,
    fit_indent,
    fit_line,
    header_lines,
)

FIXED_ROWS = 8
STATUS_COL_WIDTH = 16
MID_SEP = " · "
DEFAULT_HINT = "Up/Down navigate   Space toggle   a all   n none   Enter confirm   q back"

# Transcribed from _menu_cb_status_context in
# scripts/lib/shared/tui/menu_checkbox.sh, in its order, including one reading
# that is wrong: "not backed up" contains "backed up", so the first test claims
# it and a missing backup is drawn in green. Matched here rather than corrected,
# because a port that silently improves what it replaces cannot be compared
# against it -- see the commit that fixes both sides together.
_OK = ("backed up", "installed", "configured", "up to date")
_WARN = ("not backed", "upgrade", "delta", "warn")
_ERR = ("missing", "failed", "error", "drift", "extra")
_DIM = ("skipped",)
_DIM_EXACT = ("—", "-")


def status_context(status: str) -> str:
    if any(word in status for word in _OK) or status in ("ok", "OK"):
        return "ok"
    if any(word in status for word in _WARN):
        return "warn"
    if any(word in status for word in _ERR):
        return "err"
    if any(word in status for word in _DIM) or status in _DIM_EXACT:
        return "dim"
    return "info"


def color_word(word: str, context: str, palette: Palette) -> str:
    code = {
        "ok": palette.green,
        "warn": palette.yellow,
        "err": palette.red,
        "info": palette.cyan,
        "dim": palette.dim,
    }.get(context)
    if code is None:
        return word
    return f"{code}{word}{palette.reset}"


def index_width(count: int) -> int:
    if count >= 1000:
        return 4
    if count >= 100:
        return 3
    return 2


def status_width(statuses: list[str]) -> int:
    return max([STATUS_COL_WIDTH, *(len(status) for status in statuses)])


def page_size(rows: int, fixed_rows: int) -> int:
    return max(rows - fixed_rows, 1)


def page_for_cursor(cursor: int, size: int) -> int:
    return cursor // size


def page_count(count: int, size: int) -> int:
    return (count + size - 1) // size


def page_range(count: int, size: int, page: int) -> tuple[int, int]:
    start = page * size
    return start, min(start + size - 1, count - 1)


def fixed_rows(*, descs: list[str] | None) -> int:
    return FIXED_ROWS + (DESC_LINES if descs else 0)


def frame_height(count: int, size: int, page: int, *, descs: list[str] | None) -> int:
    start, end = page_range(count, size, page)
    return (end - start + 1) + fixed_rows(descs=descs)


def _paint(text: str, *, checked: bool, selected: bool, palette: Palette) -> str:
    """The eight combinations, in one place rather than at each of the two
    call sites that need them."""
    if checked and selected:
        return f"{palette.bold}{text}{palette.reset}"
    if checked:
        return text
    if selected:
        return f"{palette.bold}{palette.dim}{text}{palette.reset}"
    return f"{palette.dim}{text}{palette.reset}"


def draw_row(
    *,
    index: int,
    cursor: int,
    cols: int,
    labels: list[str],
    statuses: list[str],
    checked: list[int],
    compact: bool,
    palette: Palette,
) -> str:
    mark = "x" if checked[index] else " "
    label = labels[index]
    selected = index == cursor
    is_checked = bool(checked[index])

    if compact:
        prefix = ">" if selected else " "
        head = f"{prefix}{index + 1:2d}. [{mark}] {label}"
        if not is_checked:
            lead = f"  {palette.dim}{palette.bold if selected else ''}"
        elif selected:
            lead = f"  {palette.bold}"
        else:
            lead = "  "
        return f"{lead}{fit_line(head, cols - 2)}{palette.reset}{CLEAR_EOL}"

    status = statuses[index] if index < len(statuses) else ""
    width = status_width(statuses)
    cursor_col = "> " if selected else "  "
    head = f"  {cursor_col}{index + 1:>{index_width(len(labels))}d}. [{mark}]{MID_SEP}"
    padding = max(width - len(status), 0)

    max_label = max(cols - len(head) - width - len(MID_SEP), 1)
    if len(label) > max_label:
        label = fit_line(label, max_label)

    return "".join(
        (
            _paint(head, checked=is_checked, selected=selected, palette=palette),
            color_word(status, status_context(status), palette),
            " " * padding,
            MID_SEP,
            _paint(label, checked=is_checked, selected=selected, palette=palette),
            CLEAR_EOL,
        )
    )


def draw(
    *,
    title: str,
    breadcrumb: str = "",
    labels: list[str],
    statuses: list[str] | None = None,
    checked: list[int],
    descs: list[str] | None = None,
    hint: str = DEFAULT_HINT,
    status_message: str = "",
    cursor: int,
    size: int,
    cols: int,
    palette: Palette,
    compact: bool = False,
) -> str:
    from .menu import color_input_hint

    statuses = statuses or [""] * len(labels)
    count = len(labels)
    page = page_for_cursor(cursor, size)
    start, end = page_range(count, size, page)

    lines = header_lines(title, breadcrumb, cols, palette)
    lines.append(
        f"  {palette.dim}{color_input_hint(fit_indent(hint, cols, 2), palette)}"
        f"{palette.reset}{CLEAR_EOL}"
    )
    paging = (
        f"Page {page + 1}/{page_count(count, size)}   "
        f"Showing {start + 1}-{end + 1} of {count}"
    )
    lines.append(f"  {palette.dim}{fit_indent(paging, cols, 2)}{palette.reset}{CLEAR_EOL}")
    lines.append("")

    for index in range(start, end + 1):
        lines.append(
            draw_row(
                index=index,
                cursor=cursor,
                cols=cols,
                labels=labels,
                statuses=statuses,
                checked=checked,
                compact=compact,
                palette=palette,
            )
        )

    if status_message:
        lines.append(
            f"  {palette.yellow}{fit_indent(status_message, cols, 2)}{palette.reset}{CLEAR_EOL}"
        )
    else:
        lines.append(CLEAR_EOL)

    if descs:
        desc = descs[cursor] if cursor < len(descs) else ""
        lines.extend(description_lines(desc, cols, palette))

    return "\n".join(lines) + "\n"
