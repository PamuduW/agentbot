"""The install run's progress lines.

The shape is the sibling product's: a legend under the heading, one
[STEP]/[OK] pair per stage, and a dim rule between stages.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from itertools import pairwise

from src.ui.install_log import RULE, InstallLog, duration, log_legend

#: What Lifecycle.install hands the progress callback, in order. Each call names
#: the stage starting and how long the previous one took.
_A_RUN = (
    ("Installing skill sources", 0.0),
    ("Refreshing Graphify integration", 41.4),
    ("Skipping Boost integration", 0.2),
    ("Refreshing managed outputs", 0.0),
    ("Running diagnostics", 0.9),
    ("Install complete", 1.2),
)


def _render(stages=_A_RUN) -> list[str]:
    buffer = io.StringIO()
    log = InstallLog()
    with redirect_stdout(buffer):
        for message, elapsed in stages:
            log.stage(message, elapsed)
    return buffer.getvalue().splitlines()


class InstallLogTests(unittest.TestCase):
    def test_every_stage_that_opens_also_closes(self):
        """Four of the five never closed.

        The completion was written `if elapsed >= 1`, so any stage finishing in
        under a second opened with [STEP] and was followed straight by the next
        [STEP]. On a warm machine that was everything except the skills install.
        """
        lines = _render()
        opened = [line.split("] ", 1)[1] for line in lines if line.startswith("  [STEP]")]
        closed = [
            line.split("] ", 1)[1].rsplit(" (", 1)[0]
            for line in lines
            if line.startswith("  [OK]")
        ]
        self.assertEqual(opened, closed)

    def test_a_completion_names_its_stage_and_its_time(self):
        """It said `[OK] took 0m 41s` -- a duration attached to nothing."""
        self.assertIn("  [OK] Installing skill sources (41s)", _render())

    def test_a_rule_divides_stages_and_never_opens_or_closes_the_run(self):
        lines = _render()
        self.assertIn(f"  {RULE}", lines)
        self.assertFalse(lines[0].endswith(RULE))
        self.assertFalse(lines[-1].endswith(RULE))
        # One per gap, never two together.
        for first, second in pairwise(lines):
            self.assertFalse(first.endswith(RULE) and second.endswith(RULE))

    def test_a_skipped_stage_is_marked_and_closes_nothing(self):
        lines = _render()
        self.assertIn("  [SKIP] Skipping Boost integration", lines)
        self.assertNotIn("  [OK] Skipping Boost integration (0s)", lines)
        # And still gets the rule after it, or the stage below would join it.
        index = lines.index("  [SKIP] Skipping Boost integration")
        self.assertEqual(f"  {RULE}", lines[index + 1])

    def test_the_sentinel_closes_the_last_stage_and_opens_nothing(self):
        lines = _render()
        self.assertEqual("  [OK] Running diagnostics (1s)", lines[-1])
        self.assertNotIn("Install complete", "\n".join(lines))

    def test_a_run_of_one_stage_draws_no_rule(self):
        lines = _render((("Running diagnostics", 0.0), ("Install complete", 2.0)))
        self.assertEqual(["  [STEP] Running diagnostics", "  [OK] Running diagnostics (2s)"], lines)

    def test_the_legend_names_every_marker_the_run_can_print(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            log_legend()
        legend = buffer.getvalue()
        for marker in ("STEP", "OK", "SKIP", "WARN"):
            self.assertIn(f"{marker}=", legend)
        self.assertTrue(legend.startswith("  "), legend)

    def test_durations_read_as_minutes_once_they_are_minutes(self):
        self.assertEqual("0s", duration(0.4))
        self.assertEqual("41s", duration(41.9))
        self.assertEqual("1m 05s", duration(65))
        self.assertEqual("10m 00s", duration(600))


if __name__ == "__main__":
    unittest.main()
