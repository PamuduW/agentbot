"""Cursor CLI statusLine ownership."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.cursor_statusline import (
    DESIRED_BLOCK,
    config_path,
    inspect_cursor_statusline,
    install_cursor_statusline,
    statusline_destination,
    statusline_source,
)
from tests.support import agentbot_paths

REPO_ROOT = Path(__file__).resolve().parents[1]


class CursorStatuslineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.paths = agentbot_paths(self.home)
        # The managed source lives in the repository, which the fixture roots
        # at a temporary directory.
        source = statusline_source(self.paths)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("#!/bin/bash\n# Managed by Agentbot.\necho hi\n", encoding="utf-8")
        self.paths.cursor_home.mkdir(parents=True, exist_ok=True)

    def _write_config(self, payload: dict) -> None:
        config_path(self.paths).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_config(self) -> dict:
        return json.loads(config_path(self.paths).read_text(encoding="utf-8"))

    def test_no_configuration_reads_as_missing(self) -> None:
        state = inspect_cursor_statusline(self.paths)

        self.assertEqual(state.state, "missing")

    def test_status_exit_codes_report_wrong_not_pending(self) -> None:
        """A read-only status command exits non-zero when something is wrong,
        not when something is pending.

        `missing` is the expected state before `agentbot cursor statusline`
        runs. Reporting it as process failure made the command unusable in an
        && chain and disagreed with `vscode status` and `cli-config status`,
        which both exit 0 in the same un-configured state. It also disagreed
        with doctor_cursor_statusline, which rates only `broken` an error.
        """
        from src.cli import _handle_cursor

        class _Args:
            cursor_command = "status"

        class _Context:
            args = _Args()

        context = _Context()
        context.paths = self.paths

        for payload, expected_state, expected_code in (
            ({}, "missing", 0),
            ({"statusLine": {"type": "command", "command": "/somewhere/else.sh"}}, "unowned", 0),
        ):
            with self.subTest(state=expected_state):
                self._write_config(payload)
                self.assertEqual(inspect_cursor_statusline(self.paths).state, expected_state)
                self.assertEqual(_handle_cursor(context), expected_code)

    def test_installing_writes_the_script_and_the_block(self) -> None:
        state = install_cursor_statusline(self.paths)

        self.assertEqual(state.state, "ready")
        self.assertEqual(self._read_config()["statusLine"], DESIRED_BLOCK)
        destination = statusline_destination(self.paths)
        self.assertTrue(destination.is_file())
        self.assertTrue(destination.stat().st_mode & 0o111, "script must be executable")

    def test_unrelated_cursor_settings_survive(self) -> None:
        """cli-config.json holds the operator's model, permissions and editor
        preferences. Only the statusLine key is ours."""
        self._write_config({"model": {"modelId": "composer"}, "permissions": {"allow": ["x"]}})

        install_cursor_statusline(self.paths)

        config = self._read_config()
        self.assertEqual(config["model"], {"modelId": "composer"})
        self.assertEqual(config["permissions"], {"allow": ["x"]})

    def test_a_statusline_pointed_elsewhere_is_preserved(self) -> None:
        """Somebody else's statusline is reported, never replaced."""
        self._write_config({"statusLine": {"type": "command", "command": "~/mine.sh"}})

        state = install_cursor_statusline(self.paths)

        self.assertEqual(state.state, "unowned")
        self.assertEqual(self._read_config()["statusLine"]["command"], "~/mine.sh")
        self.assertFalse(statusline_destination(self.paths).exists())

    def test_an_absolute_path_to_our_script_still_counts_as_ours(self) -> None:
        """Cursor expands `~` itself, so the expanded path means the same file
        and must not read as somebody else's statusline."""
        expanded = str(self.paths.cursor_home / "statusline-command.sh")
        self._write_config({"statusLine": {"type": "command", "command": expanded}})
        install_cursor_statusline(self.paths)

        self.assertEqual(inspect_cursor_statusline(self.paths).state, "ready")

    def test_unparseable_config_is_never_overwritten(self) -> None:
        broken = '{"model": '
        config_path(self.paths).write_text(broken, encoding="utf-8")

        state = install_cursor_statusline(self.paths)

        self.assertEqual(state.state, "broken")
        self.assertEqual(config_path(self.paths).read_text(encoding="utf-8"), broken)

    def test_a_drifted_script_reads_as_stale(self) -> None:
        install_cursor_statusline(self.paths)
        statusline_destination(self.paths).write_text("#!/bin/bash\necho drifted\n", encoding="utf-8")

        self.assertEqual(inspect_cursor_statusline(self.paths).state, "stale")

    def test_a_configured_but_absent_script_reads_as_stale(self) -> None:
        install_cursor_statusline(self.paths)
        statusline_destination(self.paths).unlink()

        self.assertEqual(inspect_cursor_statusline(self.paths).state, "stale")

    def test_installing_twice_changes_nothing(self) -> None:
        install_cursor_statusline(self.paths)
        first = config_path(self.paths).read_text(encoding="utf-8")

        install_cursor_statusline(self.paths)

        self.assertEqual(config_path(self.paths).read_text(encoding="utf-8"), first)

    def test_the_claude_statusline_is_untouched(self) -> None:
        """Two separate surfaces; installing one must not disturb the other."""
        claude_settings = self.paths.claude_home / "settings.json"
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text('{"statusLine": {"command": "~/.claude/x.sh"}}', encoding="utf-8")
        claude_script = self.paths.claude_home / "statusline-command.sh"
        claude_script.write_text("claude\n", encoding="utf-8")

        install_cursor_statusline(self.paths)

        self.assertEqual(
            json.loads(claude_settings.read_text(encoding="utf-8"))["statusLine"]["command"],
            "~/.claude/x.sh",
        )
        self.assertEqual(claude_script.read_text(encoding="utf-8"), "claude\n")


class CursorStatuslineScriptTests(unittest.TestCase):
    """The shipped script, run against the payload shapes Cursor documents."""

    script = REPO_ROOT / "global" / "cursor" / "statusline-command.sh"

    def _render(self, payload: dict) -> str:
        completed = subprocess.run(
            ["bash", str(self.script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        import re

        return re.sub(r"\033\[[0-9;]*m", "", completed.stdout).strip()

    def test_a_display_name_containing_a_space_does_not_shift_fields(self) -> None:
        """Break caught: whitespace splitting on "Composer 1" moved every later
        field, so the width landed in the vim segment and truncation never
        fired."""
        rendered = self._render(
            {
                "workspace": {"current_dir": "/tmp/project"},
                "model": {"display_name": "Composer 1"},
                "context_window": {"used_percentage": 34.5},
                "render_width_chars": 200,
            }
        )

        self.assertIn("Composer 1", rendered)
        self.assertIn("Context 34% used", rendered)
        self.assertNotIn("200", rendered)

    def test_absent_optional_fields_do_not_shift_the_rest(self) -> None:
        """vim and worktree are absent most of the time, and tab is IFS
        whitespace, so consecutive separators used to collapse."""
        rendered = self._render(
            {
                "workspace": {"current_dir": "/tmp/p"},
                "model": {"display_name": "M"},
                "context_window": {"used_percentage": 12.0},
                "render_width_chars": 200,
            }
        )

        self.assertNotIn("200", rendered)
        self.assertNotIn("wt:", rendered)

    def test_the_reported_width_truncates_the_line(self) -> None:
        """Cursor reports render_width_chars; the Claude script's COLUMNS is
        not set for this command."""
        rendered = self._render(
            {
                "workspace": {"current_dir": "/very/long/path/that/keeps/going/and/going"},
                "model": {"display_name": "Composer 1"},
                "context_window": {"used_percentage": 99.9},
                "render_width_chars": 30,
            }
        )

        self.assertLessEqual(len(rendered), 30)
        self.assertTrue(rendered.endswith("…"))

    def test_the_layout_matches_the_claude_statusline(self) -> None:
        """`like the Claude one` is the requirement, so the palette, separator,
        segment order and wording are pinned rather than left to drift."""
        rendered = self._render(
            {
                "workspace": {"current_dir": "/tmp/p"},
                "model": {"display_name": "Composer 1"},
                "context_window": {"used_percentage": 34.5},
                "render_width_chars": 200,
            }
        )

        self.assertEqual(rendered, "Composer 1 · /tmp/p · Context 34% used")

    def test_an_empty_payload_still_renders_something(self) -> None:
        self.assertEqual(self._render({}), "Cursor")

    def test_a_null_context_percentage_is_omitted(self) -> None:
        """Documented as null before the first API call."""
        rendered = self._render(
            {
                "model": {"display_name": "X"},
                "context_window": {"used_percentage": None},
                "render_width_chars": 80,
            }
        )

        self.assertNotIn("Context", rendered)
        self.assertIn("X", rendered)


if __name__ == "__main__":
    unittest.main()
