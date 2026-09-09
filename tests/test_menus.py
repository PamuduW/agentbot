"""What the menus say, and the transport that carries it.

The menu definitions are read through a subprocess by the shell suites, which
means a mistake in them shows up as a menu that looks wrong rather than as a
failure with a line number. These are the assertions that fail with one.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from src.ui import graphify_lib, menu_select, menus


class MenuDefinitionTests(unittest.TestCase):
    def test_every_menu_has_a_key_and_a_description_per_label(self) -> None:
        """Three parallel sequences describe one menu. A label without its key
        shifts every entry below it onto the wrong action."""
        for name in menus.names():
            with self.subTest(menu=name):
                defined = menus.menu(name)
                self.assertEqual(len(defined.labels), len(defined.keys))
                if defined.descs:
                    self.assertEqual(len(defined.labels), len(defined.descs))
                self.assertTrue(defined.title)

    def test_a_mismatched_definition_refuses_to_exist(self) -> None:
        with self.assertRaises(ValueError):
            menus.Menu(title="T", breadcrumb="B", labels=("a", "b"), keys=("a",))
        with self.assertRaises(ValueError):
            menus.Menu(title="T", breadcrumb="B", labels=("a",), keys=("a",), descs=("x", "y"))

    def test_an_unknown_menu_names_itself(self) -> None:
        with self.assertRaises(KeyError) as raised:
            menus.menu("no-such-menu")
        self.assertIn("no-such-menu", str(raised.exception))

    def test_the_command_lib_tracks_the_command_metadata(self) -> None:
        """The point of deriving it: a command added to src/commands.py appears
        in the menu with nothing else edited."""
        from src.commands import COMMANDS

        public = {spec.name for spec in COMMANDS if spec.surface == "public"}
        keys = set(menus.menu("command_lib").keys)
        self.assertEqual(public, keys - {"__bootstrap__"})
        self.assertIn("__bootstrap__", keys)

        bootstrap = {spec.name for spec in COMMANDS if spec.surface == "bootstrap"}
        self.assertEqual(bootstrap, set(menus.menu("command_lib_bootstrap").keys))

    def test_the_graphify_menus_key_on_the_command_itself(self) -> None:
        for section in ("assistant", "shell", "platform"):
            with self.subTest(section=section):
                defined = menus.menu(f"graphify_{section}")
                commands = [row.command for row in graphify_lib.rows(section)]
                self.assertEqual(list(defined.keys), commands)
                self.assertEqual(list(defined.descs), commands)
                self.assertIn(section.capitalize(), defined.breadcrumb)

    def test_as_spec_carries_width_and_colour_through(self) -> None:
        spec = menus.as_spec("libraries", cols=100, color=True)
        self.assertEqual(spec["cols"], 100)
        self.assertTrue(spec["color"])
        self.assertEqual(spec["keys"], ["command_lib", "graphify_lib"])


class GraphifyReferenceTests(unittest.TestCase):
    def test_every_row_names_a_graphify_subcommand(self) -> None:
        for section in ("shell", "platform"):
            for row in graphify_lib.rows(section):
                with self.subTest(command=row.command):
                    self.assertTrue(row.command.startswith("graphify "))
                    self.assertTrue(row.label and row.description)

    def test_assistant_rows_are_the_in_conversation_forms(self) -> None:
        prefixes = {row.command[0] for row in graphify_lib.ASSISTANT}
        self.assertEqual(prefixes, {"/", "$"})

    def test_a_row_is_found_by_its_command_and_missing_ones_are_none(self) -> None:
        found = graphify_lib.row("shell", "graphify hook status")
        assert found is not None
        self.assertEqual(found.label, "Hook status")
        self.assertIsNone(graphify_lib.row("shell", "graphify not-a-command"))
        with self.assertRaises(KeyError):
            graphify_lib.rows("nowhere")

    def test_the_read_hatch_answers_both_questions(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(graphify_lib.main(["--commands"]), 0)
        self.assertIn("merge-graphs", out.getvalue().split("\n"))

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                graphify_lib.main(["--section", "shell", "--command", "graphify hook status"]), 0
            )
        self.assertEqual(out.getvalue().strip(), "Hook status\x1fInspect repository hook integration.")

    def test_an_unknown_command_fails_rather_than_printing_a_blank_page(self) -> None:
        self.assertEqual(graphify_lib.main(["--section", "shell", "--command", "nope"]), 1)


class SpecTransportTests(unittest.TestCase):
    def test_a_definition_survives_the_round_trip(self) -> None:
        spec = menus.as_spec("main", cols=80, color=False)
        back = menu_select.parse_spec(menu_select.dump_spec(spec))
        for key in ("title", "breadcrumb", "labels", "keys", "descs"):
            self.assertEqual(back[key], spec[key], key)

    def test_a_two_line_description_survives(self) -> None:
        """Break caught: str.splitlines() treats \\x1e as a line boundary, so
        every escaped newline was cut back apart and the second line lost."""
        text = f"desc{menu_select.FIELD_SEP}first{menu_select.NEWLINE_SUB}second\n"
        self.assertEqual(menu_select.parse_spec(text)["descs"], ["first\nsecond"])

    def test_absent_fields_take_their_defaults(self) -> None:
        spec = menu_select.parse_spec("title\x1fT\n")
        self.assertEqual(spec["cols"], 80)
        self.assertFalse(spec["color"])
        self.assertEqual(spec["breadcrumb"], "")

    def test_cols_and_colour_are_read_as_their_types(self) -> None:
        spec = menu_select.parse_spec("cols\x1f120\ncolor\x1f1\n")
        self.assertEqual(spec["cols"], 120)
        self.assertTrue(spec["color"])


if __name__ == "__main__":
    unittest.main()
