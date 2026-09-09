"""The checkbox frame's arithmetic and colouring.

The parity suite compares whole frames against the Bash, which catches anything
that differs but says little about why. These are the pieces on their own: the
column widths a long status pushes around, the paging maths, and the status
reading -- including the one that is currently wrong in both languages, pinned
here so the fix has something to change.
"""

from __future__ import annotations

import unittest

from src.ui import checkbox
from src.ui.menu import Palette


class StatusContextTests(unittest.TestCase):
    def test_each_family_gets_its_colour(self) -> None:
        for status, want in (
            ("installed", "ok"),
            ("configured", "ok"),
            ("up to date", "ok"),
            ("ok", "ok"),
            ("upgrade available", "warn"),
            ("delta", "warn"),
            ("missing", "err"),
            ("failed", "err"),
            ("drift", "err"),
            ("skipped", "dim"),
            ("—", "dim"),
            ("something else", "info"),
        ):
            with self.subTest(status=status):
                self.assertEqual(checkbox.status_context(status), want)

    def test_not_backed_up_reads_as_ok_which_is_the_bug(self) -> None:
        """Pinned, not endorsed: "not backed up" contains "backed up", so the
        first test claims it and a missing backup is drawn green. The Bash does
        the same, and the two are corrected together or not at all."""
        self.assertEqual(checkbox.status_context("not backed up"), "ok")

    def test_an_unknown_context_is_left_unpainted(self) -> None:
        palette = Palette(color=True)
        self.assertEqual(checkbox.color_word("x", "nonsense", palette), "x")
        self.assertIn("\033[32m", checkbox.color_word("x", "ok", palette))
        self.assertEqual(checkbox.color_word("x", "ok", Palette(color=False)), "x")


class LayoutTests(unittest.TestCase):
    def test_the_index_column_widens_with_the_list(self) -> None:
        self.assertEqual(checkbox.index_width(9), 2)
        self.assertEqual(checkbox.index_width(100), 3)
        self.assertEqual(checkbox.index_width(1000), 4)

    def test_the_status_column_is_never_narrower_than_its_default(self) -> None:
        self.assertEqual(checkbox.status_width(["ok", "drift"]), checkbox.STATUS_COL_WIDTH)
        wide = "a status longer than sixteen characters"
        self.assertEqual(checkbox.status_width(["ok", wide]), len(wide))

    def test_a_long_label_is_cut_to_what_the_row_has_left(self) -> None:
        row = checkbox.draw_row(
            index=0,
            cursor=0,
            cols=40,
            labels=["a label far too long to fit in a narrow row"],
            statuses=["ok"],
            checked=[1],
            compact=False,
            palette=Palette(color=False),
        )
        self.assertLessEqual(len(row.replace(checkbox.CLEAR_EOL, "")), 40)
        self.assertIn("...", row)

    def test_paging_covers_every_entry_exactly_once(self) -> None:
        count, size = 7, 3
        seen: list[int] = []
        for page in range(checkbox.page_count(count, size)):
            start, end = checkbox.page_range(count, size, page)
            seen.extend(range(start, end + 1))
        self.assertEqual(seen, list(range(count)))

    def test_the_last_page_stops_at_the_last_entry(self) -> None:
        self.assertEqual(checkbox.page_range(7, 3, 2), (6, 6))
        self.assertEqual(checkbox.page_for_cursor(6, 3), 2)

    def test_a_page_is_at_least_one_row_however_small_the_terminal(self) -> None:
        self.assertEqual(checkbox.page_size(4, checkbox.FIXED_ROWS), 1)

    def test_the_frame_height_follows_the_rows_on_the_page(self) -> None:
        without = checkbox.frame_height(7, 3, 0, descs=None)
        with_descs = checkbox.frame_height(7, 3, 0, descs=["a"] * 7)
        self.assertEqual(with_descs - without, 2)
        # The short last page draws fewer rows, so the redraw must move less.
        self.assertLess(
            checkbox.frame_height(7, 3, 2, descs=None),
            checkbox.frame_height(7, 3, 0, descs=None),
        )


class RowPaintingTests(unittest.TestCase):
    def _row(self, *, checked: int, cursor: int, compact: bool = False) -> str:
        return checkbox.draw_row(
            index=0,
            cursor=cursor,
            cols=80,
            labels=["Skills"],
            statuses=["installed"],
            checked=[checked],
            compact=compact,
            palette=Palette(color=True),
        )

    def test_the_four_states_are_all_different(self) -> None:
        """Checked and unchecked cross selected and not; a dim unchecked row
        still shows a bold cursor, and collapsing any pair loses that."""
        rows = {
            self._row(checked=1, cursor=0),
            self._row(checked=1, cursor=1),
            self._row(checked=0, cursor=0),
            self._row(checked=0, cursor=1),
        }
        self.assertEqual(len(rows), 4)

    def test_the_mark_says_whether_a_row_is_on(self) -> None:
        self.assertIn("[x]", self._row(checked=1, cursor=1))
        self.assertIn("[ ]", self._row(checked=0, cursor=1))

    def test_the_cursor_is_only_on_the_selected_row(self) -> None:
        self.assertIn(">", self._row(checked=1, cursor=0))
        self.assertNotIn(">", self._row(checked=1, cursor=1))
        self.assertIn(">", self._row(checked=1, cursor=0, compact=True))

    def test_a_compact_row_drops_the_status_column(self) -> None:
        self.assertNotIn("installed", self._row(checked=1, cursor=0, compact=True))


if __name__ == "__main__":
    unittest.main()
