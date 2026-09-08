"""The interactive half of a menu: read keys, redraw, return the choice.

The frames come from `menu.py`; this is the loop around them, and the two are
separate because only one of them can be compared byte for byte. Ported from
scripts/lib/shared/tui/menu_simple.sh and menu_keys.sh, whose behaviour it
matches key for key -- including the parts that look odd and are not:

* A read that fails is `confirm`, not an error. That is how a menu behaves when
  its input reaches EOF: it takes what the cursor is on rather than hanging.
* A bare ESC is `cancel`, but ESC followed by anything within 10 ms is the start
  of an arrow key. The peek is what tells those apart.

The terminal seam is the same one the Bash uses: DOTFILES_TTY_INPUT and
DOTFILES_TTY_OUTPUT, so a test can drive a menu through files with no terminal
anywhere. Reading that seam here rather than a bare /dev/tty is what makes this
testable at all.

The menu to draw arrives on stdin as `name\x1fvalue` lines; the chosen key goes
to stdout. A cancelled menu prints nothing and exits 1, and anything unexpected
exits 3, so the caller can tell the two apart.
"""

from __future__ import annotations

import os
import select
import sys
from typing import BinaryIO

from . import menu

ESCAPE_PEEK_SECONDS = 0.01
_ESCAPE_SEQUENCES = {
    "[A": "up",
    "OA": "up",
    "[B": "down",
    "OB": "down",
    "[C": "right",
    "OC": "right",
    "[D": "left",
    "OD": "left",
    "[Z": "shift_tab",
    "[5~": "page_up",
    "[6~": "page_down",
}
_KEYS = {
    " ": "toggle",
    "\r": "confirm",
    "\n": "confirm",
    "a": "all",
    "A": "all",
    "n": "none",
    "N": "none",
    "q": "cancel",
    "Q": "cancel",
    "\x03": "cancel",
    "\t": "tab",
}


def _terminfo(capability: str, fallback: str) -> str:
    """The same bytes `tput <capability>` would emit, from the same terminfo.

    The Bash side calls tput and falls back to a literal sequence when it fails;
    curses reads the same database in-process, so the sequences match without a
    spawn per menu -- and the fallback is the same one for the same reason.
    """
    try:
        import curses

        curses.setupterm()
        value = curses.tigetstr(capability)
    except Exception:  # no terminfo is a normal state, not a failure
        return fallback
    return value.decode("latin-1") if value else fallback


def input_path() -> str:
    return os.environ.get("DOTFILES_TTY_INPUT", "/dev/tty")


def output_path() -> str:
    return os.environ.get("DOTFILES_TTY_OUTPUT", "/dev/tty")


class KeyReader:
    """One character at a time, in cbreak mode when there is a terminal.

    A regular file is read as-is: the test seam points the input at a file of
    keystrokes, and putting a file into cbreak mode is neither possible nor
    needed.

    Takes a descriptor or a path, because the Bash seam this matches has both --
    DOTFILES_TTY_IN_FD for a caller that already owns the stream, and
    DOTFILES_TTY_INPUT for one that does not.
    """

    def __init__(self, path: str | None = None, *, fd: int | None = None) -> None:
        # Closed in close(), not by a context manager: the reader outlives any
        # one read, and the terminal mode it changed must be put back.
        if fd is not None:
            self._file: BinaryIO = os.fdopen(fd, "rb", buffering=0, closefd=False)
        else:
            self._file = open(str(path), "rb", buffering=0)
        self._saved: list | None = None
        if self._file.isatty():
            import termios
            import tty

            self._termios = termios
            self._saved = termios.tcgetattr(self._file)
            tty.setcbreak(self._file.fileno())

    def close(self) -> None:
        if self._saved is not None:
            try:
                self._termios.tcsetattr(self._file, self._termios.TCSADRAIN, self._saved)
            except self._termios.error:
                # The terminal is already gone, which is how the loop got here.
                # Failing to restore a mode nobody can see would turn a normal
                # exit into a crash on the way out.
                pass
        self._file.close()

    def read_char(self, timeout: float | None = None) -> str | None:
        if timeout is not None:
            ready, _, _ = select.select([self._file], [], [], timeout)
            if not ready:
                return None
        try:
            data = self._file.read(1)
        except OSError:
            # A terminal whose other end has gone raises rather than returning
            # nothing. Both mean the same thing here: there is no next key.
            return None
        if not data:
            return None
        return data.decode("utf-8", "replace")

    def read_action(self) -> str:
        char = self.read_char()
        if char is None:
            return "confirm"
        if char == "\x1b":
            sequence = ""
            while len(sequence) < 16:
                nxt = self.read_char(ESCAPE_PEEK_SECONDS)
                if nxt is None:
                    break
                sequence += nxt
            if not sequence:
                return "cancel"
            return _ESCAPE_SEQUENCES.get(sequence, "ignore")
        return _KEYS.get(char, "ignore")


def _selectable(types: list[str], index: int) -> bool:
    return not (index < len(types) and types[index] == "header")


def _first_selectable(types: list[str], count: int) -> int | None:
    for index in range(count):
        if _selectable(types, index):
            return index
    return None


def _move(cursor: int, step: int, count: int, types: list[str]) -> int:
    index = cursor
    while True:
        index += step
        if index < 0 or index >= count:
            return cursor
        if _selectable(types, index):
            return index


def _open_output(fd: int | None):
    if fd is not None:
        return os.fdopen(fd, "w", closefd=False)
    # Append rather than truncate, matching tty_printf: identical on /dev/tty,
    # and a seam pointed at a regular file must accumulate rather than replace.
    return open(output_path(), "a")  # closed by the caller's with


def run(spec: dict, *, input_fd: int | None = None, output_fd: int | None = None) -> str | None:
    """Draw, read, redraw until the operator confirms. None means cancelled."""
    labels = spec["labels"]
    types = spec.get("types") or []
    descs = spec.get("descs") or None
    breadcrumb = spec.get("breadcrumb", "")
    palette = menu.Palette(color=bool(spec.get("color")))
    cols = int(spec.get("cols", 80))

    cursor = _first_selectable(types, len(labels))
    if cursor is None:
        return None

    height = menu.frame_height(labels, breadcrumb=breadcrumb, descs=descs)
    def frame(index: int) -> str:
        return menu.draw_simple(
            title=spec["title"],
            breadcrumb=breadcrumb,
            labels=labels,
            types=types,
            descs=descs,
            hint=spec.get("hint") or menu.DEFAULT_HINT,
            cursor=index,
            cols=cols,
            palette=palette,
        )

    reader = KeyReader(None if input_fd is not None else input_path(), fd=input_fd)
    with _open_output(output_fd) as out:
        try:
            out.write(_terminfo("civis", "\x1b[?25l"))
            out.write(_terminfo("clear", "\x1b[2J\x1b[H"))
            out.write(frame(cursor))
            out.flush()
            while True:
                action = reader.read_action()
                if action == "up":
                    cursor = _move(cursor, -1, len(labels), types)
                elif action == "down":
                    cursor = _move(cursor, 1, len(labels), types)
                elif action == "confirm":
                    return spec["keys"][cursor]
                elif action == "cancel":
                    return None
                else:
                    continue
                out.write(f"\x1b[{height}A")
                out.write(frame(cursor))
                out.flush()
        finally:
            out.write(_terminfo("cnorm", "\x1b[?25h"))
            out.flush()
            reader.close()


FIELD_SEP = "\x1f"
NEWLINE_SUB = "\x1e"
_REPEATED = {"label": "labels", "key": "keys", "desc": "descs", "type": "types"}


def parse_spec(text: str) -> dict:
    """`name\x1fvalue` per line; repeated names build the lists.

    The same shape the probe classifier's requests use, and for the same reason:
    the writer is Bash, and a format with no quoting has nothing to get wrong. A
    description carries newlines, which travel as \x1e.
    """
    spec: dict = {"title": "", "breadcrumb": "", "hint": "", "cols": 80, "color": False}
    # split("\n"), never splitlines(): \x1e is a line boundary to splitlines, so
    # it would cut every escaped newline back apart and drop the second half of
    # every two-line description -- which is exactly what it did.
    for line in text.split("\n"):
        if not line:
            continue
        name, _, value = line.partition(FIELD_SEP)
        value = value.replace(NEWLINE_SUB, "\n")
        if name in _REPEATED:
            spec.setdefault(_REPEATED[name], []).append(value)
        elif name == "cols":
            spec["cols"] = int(value or 80)
        elif name == "color":
            spec["color"] = value == "1"
        else:
            spec[name] = value
    return spec


def main(argv: list[str] | None = None) -> int:
    """Exit 0 with the chosen key, 1 for a cancelled menu, 3 for anything else.

    Three rather than one so the caller can tell "the operator pressed q" from
    "this did not work", and fall back to the Bash loop only for the second.
    """
    try:
        choice = run(parse_spec(sys.stdin.read()))
    except Exception:  # the caller has a Bash menu to fall back to
        import traceback

        traceback.print_exc()
        return 3
    if choice is None:
        return 1
    print(choice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
