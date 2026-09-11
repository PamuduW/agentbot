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

from .table import ORANGE, _c

# The same four words, in the same order, as the sibling product's legend.
LEGEND = "[Legend] STEP=starting  OK=completed  SKIP=already satisfied  WARN=needs attention"
# Forty dashes, as log_component_rule draws.
RULE = "-" * 40


def log_legend() -> None:
    print(f"  {LEGEND}", flush=True)


def log_rule() -> None:
    print(f"  {RULE}", flush=True)


def log_line(level: str, message: str) -> None:
    print(f"  [{level}] {message}", flush=True)


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
