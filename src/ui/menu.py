"""Menu frames, drawn in Python.

ADR-0001 moves everything above the bootstrap to Python, and the Dotfiles menu
stack is the one part that cannot follow: it draws before that machine has an
interpreter. Agentbot has no such constraint -- `bootstrap.sh` will not install
this repository until `python3` and PyYAML are there -- so its menus can move,
and this is the first piece of them.

Frames only. Reading keys and looping is still
`scripts/lib/shared/tui/menu_simple.sh`; what is here is the half that decides
what a frame looks like, which is the half that can be compared byte for byte
against the Bash it replaces. tests/test_menu_parity.sh does exactly that, the
same oracle that made the table migration verifiable rather than hopeful.

Every escape below is deliberate and load-bearing: `\\x1b[K` clears the tail of
a line the previous frame drew longer, and dropping it leaves the old text
behind when a description shortens.
"""

from __future__ import annotations

from .table import BOLD, CYAN, DIM, GREEN, ORANGE, RED, RESET, YELLOW

CLEAR_EOL = "\x1b[K"
DESC_LINES = 2
DEFAULT_HINT = "Up/Down navigate   Enter confirm"

# Mirrors ui_color_input_hint in scripts/lib/shared/tui/ui.sh, transcribed
# rather than derived: the replacements run in this order there, and the order
# is load-bearing. `[q] back` is listed before `[q] back_to_menu`, so a hint
# carrying the longer phrase is lit by the shorter rule and the longer one then
# finds nothing. Deriving the lit forms from the patterns would quietly "fix"
# that and stop matching the Bash.
_HINT_SUBSTITUTIONS = (
    ("Up/Down", "{s}Up/Down{e}"),
    ("Space toggle", "{s}Space{e} toggle"),
    ("Enter confirm", "{s}Enter{e} confirm"),
    ("Enter system package details", "{s}Enter{e} system package details"),
    ("[c]onfirm", "{s}[c]{e}onfirm"),
    ("[e]dit", "{s}[e]{e_}dit"),
    ("[q] back", "{s}[q]{e} back"),
    ("[c] confirm", "{s}[c]{e} confirm"),
    ("[e] edit", "{s}[e]{e} edit"),
    ("[q] back_to_menu", "{s}[q]{e} back_to_menu"),
    ("   a all", "   {s}a{e} all"),
    ("   n none", "   {s}n{e} none"),
    ("   q back", "   {s}q{e} back"),
)


class Palette:
    """The six colours the menu draws with, or nothing at all.

    A palette rather than a boolean because the Bash side reads six separate
    variables that are empty under NO_COLOR, and an empty string is what makes
    the two outputs identical without a branch at every printf.
    """

    def __init__(self, *, color: bool) -> None:
        self.bold = BOLD if color else ""
        self.dim = DIM if color else ""
        self.orange = ORANGE if color else ""
        self.yellow = YELLOW if color else ""
        self.cyan = CYAN if color else ""
        self.green = GREEN if color else ""
        self.red = RED if color else ""
        self.reset = RESET if color else ""


def fit_line(text: str, cols: int) -> str:
    """menu_fit_line: one column is always left for the cursor."""
    max_cols = max(cols - 1, 1)
    if len(text) > max_cols:
        if max_cols > 3:
            return text[: max_cols - 3] + "..."
        return text[:max_cols]
    return text


def fit_indent(text: str, cols: int, indent: int) -> str:
    return fit_line(text, max(cols - indent, 1))


def color_input_hint(hint: str, palette: Palette) -> str:
    """The key names inside a hint line are lit; the rest stays dim."""
    key_start = f"{palette.reset}{palette.cyan}"
    key_end = f"{palette.reset}{palette.dim}"
    for phrase, lit in _HINT_SUBSTITUTIONS:
        hint = hint.replace(phrase, lit.format(s=key_start, e=key_end, e_=key_end))
    return hint


def header_lines(title: str, breadcrumb: str, cols: int, palette: Palette) -> list[str]:
    """ui_print_header, including its trailing blank line."""
    lines = [
        f"  {palette.bold}{palette.orange}"
        f"{fit_indent(f'=== {title} ===', cols, 2)}{palette.reset}{CLEAR_EOL}"
    ]
    if breadcrumb:
        lines.append(f"  {palette.dim}{fit_indent(breadcrumb, cols, 2)}{palette.reset}{CLEAR_EOL}")
    lines.append(CLEAR_EOL)
    return lines


def section_line(label: str, cols: int, palette: Palette) -> str:
    return f"  {palette.bold}{palette.yellow}{fit_indent(label, cols, 2)}{palette.reset}{CLEAR_EOL}"


def description_lines(desc: str, cols: int, palette: Palette) -> list[str]:
    """Always DESC_LINES rows: the footer holds its height as the cursor moves,
    so a one-line description does not make the frame jump."""
    parts = desc.split("\n") if desc else []
    out = []
    for index in range(DESC_LINES):
        text = parts[index] if index < len(parts) else ""
        out.append(f"  {palette.dim}{fit_indent(text, cols, 2)}{palette.reset}{CLEAR_EOL}")
    return out


def draw_simple(
    *,
    title: str,
    breadcrumb: str = "",
    labels: list[str],
    types: list[str] | None = None,
    descs: list[str] | None = None,
    hint: str = DEFAULT_HINT,
    cursor: int,
    cols: int,
    palette: Palette,
) -> str:
    """_menu_simple_draw: one frame, ending in a newline.

    `types[i] == "header"` makes a row a section label rather than a choice, and
    section rows are not numbered -- the numbers the operator types must count
    only the things they can pick.
    """
    types = types or []
    lines = header_lines(title, breadcrumb, cols, palette)
    lines.append(
        f"  {palette.dim}{color_input_hint(fit_indent(hint, cols, 2), palette)}"
        f"{palette.reset}{CLEAR_EOL}"
    )
    lines.append(CLEAR_EOL)

    item_number = 0
    for index, label in enumerate(labels):
        if index < len(types) and types[index] == "header":
            lines.append(section_line(label, cols, palette))
            continue
        item_number += 1
        prefix = ">" if index == cursor else " "
        row = f"{prefix} {item_number}. {label}"
        if index == cursor:
            lines.append(f"  {palette.bold}{fit_line(row, cols - 2)}{palette.reset}{CLEAR_EOL}")
        else:
            lines.append(f"  {fit_line(row, cols - 2)}{CLEAR_EOL}")

    lines.append(CLEAR_EOL)
    if descs:
        desc = descs[cursor] if cursor < len(descs) else ""
        lines.extend(description_lines(desc, cols, palette))

    return "\n".join(lines) + "\n"


def frame_height(labels: list[str], *, breadcrumb: str = "", descs: list[str] | None = None) -> int:
    """_menu_simple_menu_lines: how far the next redraw moves the cursor up.

    Wrong by one and every frame after the first is drawn over the wrong place,
    which is why it is derived here rather than counted by the caller.
    """
    count = len(labels)
    lines = count + (6 if breadcrumb else 5)
    if descs:
        lines = lines - 1 + DESC_LINES + 1
    return lines
