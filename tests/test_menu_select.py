"""The interactive menu loop, driven through a real pty.

A menu is the one thing in this repository that cannot be tested by calling it
and reading what it returned: it decides what to return from what it read while
it was drawing. So this opens a pseudo-terminal, types into it, and asserts on
both halves -- the key that came back and the frames that reached the screen.

The pty matters. Feeding keys from a file cannot test navigation at all: after
an ESC the reader peeks for the rest of an arrow sequence, and a file always
reports ready, so the whole file is swallowed as one sequence. That is true of
the Bash this replaces as well, which is why its own key tests feed exactly one
key per file. A terminal is what makes ESC-then-nothing and ESC-then-[A
different, and that difference is the thing worth testing.
"""

from __future__ import annotations

import os
import pty
import threading
import time
import unittest

from src.ui import menu_select

SPEC = {
    "title": "Agentbot",
    "breadcrumb": "Agentbot",
    "labels": ["Check Status", "Install Agentbot", "Update", "Quit"],
    "keys": ["status", "install", "update", "quit"],
    "descs": ["Status.", "Install.", "Update.", "Quit."],
    "cols": 80,
    "color": False,
}

DOWN = b"\x1b[B"
UP = b"\x1b[A"
ENTER = b"\r"


class MenuSelectTests(unittest.TestCase):
    def _run(
        self, keys: list[bytes], spec: dict | None = None, *, close_input: bool = False
    ) -> tuple[str | None, str]:
        """Type `keys` into a menu and return (choice, what was drawn)."""
        controller, follower = pty.openpty()
        out_read, out_write = os.pipe()
        drawn: list[bytes] = []

        def drain() -> None:
            while True:
                chunk = os.read(out_read, 65536)
                if not chunk:
                    return
                drawn.append(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        result: list[str | None] = []
        error: list[BaseException] = []

        def run_menu() -> None:
            try:
                result.append(
                    menu_select.run(spec or SPEC, input_fd=follower, output_fd=out_write)
                )
            except BaseException as exc:  # re-raised on the main thread
                error.append(exc)
            finally:
                os.close(out_write)

        worker = threading.Thread(target=run_menu)
        worker.start()

        for key in keys:
            # Each key is a separate write with a gap, so an escape sequence
            # arrives whole and a lone ESC stays lone -- which is exactly the
            # distinction the reader's peek makes.
            time.sleep(0.05)
            os.write(controller, key)
        if close_input:
            # The only way a pty reaches EOF: the other end goes away.
            time.sleep(0.05)
            os.close(controller)
        worker.join(timeout=5)
        reader.join(timeout=5)
        if not close_input:
            os.close(controller)
        os.close(follower)
        os.close(out_read)

        if error:
            raise error[0]
        self.assertFalse(worker.is_alive(), "the menu did not return")
        return result[0], b"".join(drawn).decode()

    def test_down_twice_and_enter_selects_the_third_entry(self) -> None:
        choice, drawn = self._run([DOWN, DOWN, ENTER])
        self.assertEqual(choice, "update")
        # Three frames: the first draw and one redraw per move.
        self.assertEqual(drawn.count("=== Agentbot ==="), 3)
        self.assertIn("> 3. Update", drawn)

    def test_the_cursor_stops_at_the_ends_rather_than_wrapping(self) -> None:
        """A menu that wraps makes the last entry reachable by pressing up once,
        which is how an operator confirms Quit while meaning Status."""
        choice, _ = self._run([UP, UP, ENTER])
        self.assertEqual(choice, "status")
        choice, _ = self._run([DOWN, DOWN, DOWN, DOWN, DOWN, ENTER])
        self.assertEqual(choice, "quit")

    def test_q_cancels_and_returns_nothing(self) -> None:
        choice, _ = self._run([b"q"])
        self.assertIsNone(choice)

    def test_a_bare_escape_cancels_but_an_arrow_does_not(self) -> None:
        self.assertIsNone(self._run([b"\x1b"])[0])
        self.assertEqual(self._run([b"\x1b[B", ENTER])[0], "install")

    def test_input_ending_takes_what_the_cursor_is_on(self) -> None:
        """EOF is a confirm, not a hang and not an error: it is what the Bash
        does, and a menu whose input dies mid-session must still return."""
        choice, _ = self._run([], close_input=True)
        self.assertEqual(choice, "status")

    def test_section_headers_are_skipped_and_not_numbered(self) -> None:
        spec = dict(SPEC)
        spec["labels"] = ["Setup", "Install", "Other", "Quit"]
        spec["keys"] = ["", "install", "", "quit"]
        spec["types"] = ["header", "", "header", ""]
        spec["descs"] = ["", "Install.", "", "Quit."]
        choice, drawn = self._run([DOWN, ENTER], spec)
        self.assertEqual(choice, "quit")
        # The numbers count only what can be chosen.
        self.assertIn("1. Install", drawn)
        self.assertIn("2. Quit", drawn)
        self.assertNotIn("3.", drawn)

    def test_the_cursor_is_hidden_while_drawing_and_restored_on_the_way_out(self) -> None:
        _, drawn = self._run([b"q"])
        self.assertTrue(drawn.endswith(menu_select._terminfo("cnorm", "\x1b[?25h")))


CHECKBOX = {
    "title": "Install Agentbot",
    "breadcrumb": "Agentbot › Install Agentbot",
    "labels": ["Skills", "Graphify integration", "Boost integration"],
    "checked": [1, 1, 1],
    "descs": ["Skills.", "Graphify.", "Boost."],
    "cols": 80,
    "rows": 24,
    "color": False,
}
SPACE = b" "


class CheckboxTests(unittest.TestCase):
    """The other loop, driven the same way. What a checkbox has that a menu does
    not is state that survives a keystroke, so these press keys in sequences and
    assert on where the marks ended up."""

    def _run(
        self, keys: list[bytes], spec: dict | None = None, *, close_input: bool = False
    ) -> tuple[list[int] | None, str]:
        controller, follower = pty.openpty()
        out_read, out_write = os.pipe()
        drawn: list[bytes] = []

        def drain() -> None:
            while True:
                chunk = os.read(out_read, 65536)
                if not chunk:
                    return
                drawn.append(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        result: list[list[int] | None] = []

        def run_checkbox() -> None:
            try:
                result.append(
                    menu_select.run_checkbox(
                        spec or CHECKBOX, input_fd=follower, output_fd=out_write
                    )
                )
            finally:
                os.close(out_write)

        worker = threading.Thread(target=run_checkbox)
        worker.start()
        for key in keys:
            time.sleep(0.05)
            os.write(controller, key)
        if close_input:
            time.sleep(0.05)
            os.close(controller)
        worker.join(timeout=5)
        reader.join(timeout=5)
        if not close_input:
            os.close(controller)
        os.close(follower)
        os.close(out_read)
        self.assertFalse(worker.is_alive(), "the checkbox did not return")
        return result[0], b"".join(drawn).decode()

    def test_space_toggles_the_row_under_the_cursor_only(self) -> None:
        selection, _ = self._run([SPACE, ENTER])
        self.assertEqual(selection, [0, 1, 1])
        selection, _ = self._run([DOWN, SPACE, ENTER])
        self.assertEqual(selection, [1, 0, 1])

    def test_a_row_toggles_back(self) -> None:
        selection, _ = self._run([SPACE, SPACE, ENTER])
        self.assertEqual(selection, [1, 1, 1])

    def test_all_and_none_take_and_clear_everything(self) -> None:
        selection, drawn = self._run([b"n", ENTER])
        self.assertEqual(selection, [0, 0, 0])
        self.assertIn("All items cleared", drawn)
        selection, drawn = self._run([b"n", b"a", ENTER])
        self.assertEqual(selection, [1, 1, 1])
        self.assertIn("All items selected", drawn)

    def test_the_bulk_message_clears_when_the_cursor_moves(self) -> None:
        """It says what a key just did, not what is true, so it must not linger
        over a list the operator has since moved through."""
        _, drawn = self._run([b"n", DOWN, ENTER])
        last_frame = drawn.rsplit("=== Install Agentbot ===", 1)[-1]
        self.assertNotIn("All items cleared", last_frame)

    def test_q_backs_out_and_keeps_nothing(self) -> None:
        selection, _ = self._run([SPACE, b"q"])
        self.assertIsNone(selection)

    def test_the_cursor_stops_at_both_ends(self) -> None:
        selection, _ = self._run([UP, UP, SPACE, ENTER])
        self.assertEqual(selection, [0, 1, 1])
        selection, _ = self._run([DOWN, DOWN, DOWN, DOWN, SPACE, ENTER])
        self.assertEqual(selection, [1, 1, 0])

    def test_paging_moves_a_screen_and_stops_at_the_last_entry(self) -> None:
        spec = dict(CHECKBOX)
        spec["labels"] = [f"entry {index}" for index in range(20)]
        spec["checked"] = [0] * 20
        spec["descs"] = ["d"] * 20
        # A short terminal, so a page holds fewer rows than the list has.
        spec["rows"] = 16
        selection, drawn = self._run([b"[6~", SPACE, ENTER], spec)
        assert selection is not None
        self.assertEqual(sum(selection), 1)
        self.assertEqual(selection.index(1), 6)
        self.assertIn("Page 2/", drawn)

        selection, _ = self._run([b"[6~"] * 9 + [SPACE, ENTER], spec)
        assert selection is not None
        self.assertEqual(selection.index(1), 19)

    def test_an_empty_list_returns_nothing_rather_than_drawing(self) -> None:
        spec = dict(CHECKBOX)
        spec["labels"] = []
        spec["checked"] = []
        spec["descs"] = []
        self.assertIsNone(menu_select.run_checkbox(spec))

    def test_input_ending_confirms_what_is_marked(self) -> None:
        selection, _ = self._run([SPACE], close_input=True)
        self.assertEqual(selection, [0, 1, 1])


if __name__ == "__main__":
    unittest.main()
