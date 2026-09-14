"""The install run's progress lines, in the shape the sibling product uses.

Dotfiles' install prints a legend under its heading, one `[STEP]`/`[OK]` pair
per component, and a dim rule between components so twenty of them do not read
as a wall. Agentbot printed the prefixed lines and neither of the other two, and
its stage completions were conditional -- `if elapsed >= 1` -- so a fast stage
opened with `[STEP]` and never closed at all. Four of the five did that on a
warm machine, and the one line that did close said only `[OK] took 0m 41s`,
naming nothing.

Mirrors scripts/lib/installers/logging.sh in the sibling repository. Colour is
applied by the TUI seam in scripts/lib/tui.sh, which rewrites these markers on
the way to the terminal, so nothing here emits escapes.
"""

from __future__ import annotations

import os
import threading
from typing import TextIO

from .table import BOLD, CYAN, DIM, GREEN, ORANGE, RED, YELLOW, _c

# The same four words, in the same order, as the sibling product's legend.
#: Marker colours, matching _log_prefix in the sibling product's logging.sh so
#: a full update -- which prints both runs into one terminal -- marks them the
#: same way.
_LEVEL_COLORS = {
    "STEP": BOLD + CYAN,
    "OK": BOLD + GREEN,
    "SKIP": DIM,
    "WARN": BOLD + YELLOW,
    "FAIL": BOLD + RED,
    "ERR": BOLD + RED,
    "INFO": CYAN,
}
#: The legend names its markers without brackets, so it is coloured word by
#: word. A legend whose colours are missing is not a key to anything.
_LEGEND_WORDS = (
    ("STEP=starting", CYAN),
    ("OK=completed", GREEN),
    ("SKIP=already satisfied", DIM),
    ("WARN=needs attention", YELLOW),
)
LEGEND = "[Legend] STEP=starting  OK=completed  SKIP=already satisfied  WARN=needs attention"
# Forty dashes, as log_component_rule draws.
RULE = "-" * 40


def log_legend() -> None:
    legend = LEGEND
    for word, code in _LEGEND_WORDS:
        legend = legend.replace(word, _c(word, code), 1)
    print(f"  {legend}", flush=True)


#: The sibling product's frames and cadence, in _step_spinner_start. Matching
#: them matters because a full update prints both runs into one terminal, and
#: two different spinners read as two different tools disagreeing about what
#: "working" looks like.
_SPINNER_FRAMES = ("\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f")
_SPINNER_INTERVAL = 0.12


class _StepSpinner:
    """Says a step is still working, without putting frames in the log.

    The install's stdout is piped -- `dotfiles full-update` reads it through a
    relay, and that is exactly the run whose silences were longest -- so frames
    written there would be captured rather than animated, and a log full of
    spinner frames is worse than no spinner. They go straight to the controlling
    terminal instead, on their own line below the step, erased before the next
    line prints. This is what the sibling product's tty_printf does; no terminal,
    or AGENTBOT_NO_PROGRESS_ANIMATION set, simply has none.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tty: TextIO | None = None
        self._tty_tried = False

    def _terminal(self) -> TextIO | None:
        if self._tty_tried:
            return self._tty
        self._tty_tried = True
        if os.environ.get("AGENTBOT_NO_PROGRESS_ANIMATION"):
            return None
        try:
            self._tty = open("/dev/tty", "w", encoding="utf-8")
        except OSError:
            # No controlling terminal: a cron run, a CI job, a pipe with no tty
            # behind it. The prefixed lines still print; only the animation is
            # absent, which is the degradation this whole seam is designed for.
            self._tty = None
        return self._tty

    def start(self, message: str) -> None:
        self.stop()
        terminal = self._terminal()
        if terminal is None:
            return
        self._stop = threading.Event()
        # Daemon: a run that dies mid-stage must not be held open by the thread
        # that was drawing its spinner.
        self._thread = threading.Thread(
            target=self._spin, args=(terminal, message), daemon=True
        )
        self._thread.start()

    def _spin(self, terminal: TextIO, message: str) -> None:
        index = 0
        while not self._stop.is_set():
            # The cursor is parked back at column 0 after each frame, as the
            # sibling does, so anything printed mid-step overwrites the
            # animation from the left instead of being welded to the end of it.
            try:
                terminal.write(f"\r    {_SPINNER_FRAMES[index % len(_SPINNER_FRAMES)]} {message}\r")
                terminal.flush()
            except (OSError, ValueError):
                return
            index += 1
            self._stop.wait(_SPINNER_INTERVAL)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None
        terminal = self._tty
        if terminal is None:
            return
        # Erase the line the animation owned, so the next log line starts clean.
        try:
            terminal.write("\r\033[K")
            terminal.flush()
        except (OSError, ValueError):
            return


_SPINNER = _StepSpinner()


def stop_progress_animation() -> None:
    """End any running animation.

    The closing `[OK]` normally does this, but the report tables that follow a
    run print straight to stdout rather than through `log_line`. A run that
    ended without closing its last stage would otherwise draw a table over a
    spinner still writing to the terminal underneath it.
    """
    _SPINNER.stop()


def log_rule() -> None:
    # A boundary ends the step before it, so no animation is left running
    # underneath the next component's output.
    _SPINNER.stop()
    print(f"  {_c(RULE, DIM)}", flush=True)


def log_line(level: str, message: str) -> None:
    """A prefixed progress line, coloured here rather than downstream.

    The markers used to be painted by the Bash relay the menu runs this backend
    under, which meant they were plain everywhere else -- including a Dotfiles
    full update, where they sat beside that product's own coloured ones.
    """
    code = _LEVEL_COLORS.get(level)
    # No code, no escapes at all: _c with an empty colour still appends a
    # reset, which would put a stray byte on a line nothing had painted.
    marker = _c(f"[{level}]", code) if code else f"[{level}]"
    # Any line ends the step that was running: the run has moved on, and the
    # animation must not still be claiming otherwise underneath it.
    _SPINNER.stop()
    print(f"  {marker} {message}", flush=True)
    if level == "STEP":
        _SPINNER.start(message)


def duration(seconds: float) -> str:
    """`41s`, or `1m 05s` once a stage passes a minute."""
    whole = int(seconds)
    return f"{whole // 60}m {whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def clock(seconds: float) -> str:
    """`0m 41s`, always both units.

    The timing block writes every row this way so a column of them lines up and
    compares; `duration` above drops the minutes when there are none, which
    reads better mid-sentence and worse in a column.
    """
    whole = int(seconds)
    return f"{whole // 60}m {whole % 60:02d}s"


#: How many rows the slowest-first list shows. Enough to tell a network-bound
#: phase from a slow one without turning a summary into a profile.
SLOWEST_ROWS = 6


def print_timing(label: str, seconds: dict[str, float], total: float) -> None:
    """`<label> took 0m 19s. Slowest phases:` and the slowest handful.

    The shape the sibling product closes its install with, so a full update --
    which prints both products' runs into one terminal -- reads as one report
    rather than two conventions.
    """
    if not seconds:
        return
    print()
    print(f"  {_c(f'{label} took {clock(total)}. Slowest phases:', ORANGE)}")
    ranked = sorted(seconds.items(), key=lambda item: item[1], reverse=True)
    for name, elapsed in ranked[:SLOWEST_ROWS]:
        # Whole seconds, so a phase that rounds to nothing says nothing.
        if int(elapsed) <= 0:
            continue
        print(f"    {clock(elapsed):>7}  {name}")


class InstallLog:
    """Opens a stage on `[STEP]` and closes the same stage by name on `[OK]`.

    Lifecycle.install reports a stage as it *starts*, handing over how long the
    previous one took -- so closing a stage means remembering what was opened.
    """

    #: Lifecycle's final call, which closes the last stage and opens nothing.
    #: The update run reports "Update complete" for the same purpose.
    SENTINEL = "Install complete"

    def __init__(self, *, sentinel: str | None = None, quiet: bool = False) -> None:
        #: Measure without narrating. The update's planning phases are timed --
        #: the closing summary reports them -- but printing a heading, a legend
        #: and three [STEP]/[OK] pairs in front of one table was scaffolding
        #: rather than progress.
        self._quiet = quiet
        self._sentinel = sentinel or self.SENTINEL
        self._open: str | None = None
        self._any = False
        #: Wall-clock per named phase, and for the whole run. The total counts
        #: the setup before the first phase too, which belongs to the run even
        #: though no phase owns it.
        self.seconds: dict[str, float] = {}
        self.total = 0.0

    def stage(self, message: str, elapsed: float) -> None:
        self.total += elapsed
        if self._open is not None:
            self.seconds[self._open] = self.seconds.get(self._open, 0.0) + elapsed
            if not self._quiet:
                log_line("OK", f"{self._open} ({duration(elapsed)})")
            self._open = None
        if message == self._sentinel:
            return
        if self._quiet:
            if not message.startswith("Skipping"):
                self._open = message
            return
        # Between stages, never above the first: the heading already separates
        # the run from what came before it. Tracked apart from the open stage,
        # because a skipped one closes nothing and still needs the rule after
        # it.
        if self._any:
            log_rule()
        self._any = True
        # A deselected component is reported and skipped, so it has no work to
        # close and opens nothing.
        if message.startswith("Skipping"):
            log_line("SKIP", message)
            return
        log_line("STEP", message)
        self._open = message
