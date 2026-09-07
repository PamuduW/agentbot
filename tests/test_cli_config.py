"""Preview-first ownership of the Claude, Codex, and Cursor CLI configs."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.cli_config import (
    apply,
    desired_source,
    doctor_cli_configs,
    managed_clis,
    merge_toml_text,
    plan_cli,
    preview,
    read_desired,
)
from src.vscode import strip_jsonc
from tests.support import agentbot_paths

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


class CliConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = agentbot_paths(Path(temporary.name))
        self.clis = {cli.name: cli for cli in managed_clis(self.paths)}
        for cli in self.clis.values():
            cli.config_path.parent.mkdir(parents=True, exist_ok=True)

    def _declare(self, cli_name: str, text: str) -> None:
        source = desired_source(self.paths.root, self.clis[cli_name])
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")

    def _write_config(self, cli_name: str, text: str) -> None:
        self.clis[cli_name].config_path.write_text(text, encoding="utf-8")

    def _read_config(self, cli_name: str) -> dict:
        cli = self.clis[cli_name]
        text = cli.config_path.read_text(encoding="utf-8")
        # JSONC: these files may carry the operator's comments.
        return tomllib.loads(text) if cli.fmt == "toml" else json.loads(strip_jsonc(text))

    def test_nothing_declared_is_a_skip(self) -> None:
        report = preview(self.paths)

        self.assertTrue(all(plan.skipped for plan in report.plans.values()))
        self.assertFalse(report.has_work)

    def test_a_missing_config_directory_is_a_reported_skip(self) -> None:
        """A CLI that is not installed here is not a failure."""
        self._declare("codex", 'model = "gpt-5"\n')
        import shutil

        shutil.rmtree(self.paths.codex_home)

        plan = plan_cli(self.paths.root, self.clis["codex"])

        self.assertIsNotNone(plan.skipped)
        self.assertIsNone(plan.error)

    def test_owned_keys_merge_and_unowned_keys_survive(self) -> None:
        self._write_config("claude", '{"theme": "dark", "model": "old"}')
        self._declare("claude", '{"model": "new"}')

        apply(self.paths)

        config = self._read_config("claude")
        self.assertEqual(config["model"], "new")
        self.assertEqual(config["theme"], "dark")

    def test_json_comments_survive_a_merge(self) -> None:
        """Reuses the VS Code merge, which edits text rather than
        re-serialising, so hand-written comments are not deleted."""
        self._write_config("cursor", '{\n  // mine\n  "model": "old"\n}\n')
        self._declare("cursor", '{"model": "new"}')

        apply(self.paths)

        text = self.clis["cursor"].config_path.read_text(encoding="utf-8")
        self.assertIn("// mine", text)
        self.assertEqual(self._read_config("cursor")["model"], "new")

    def test_toml_tables_and_comments_are_left_alone(self) -> None:
        self._write_config(
            "codex",
            '# my notes\nmodel = "old"\n\n[tui]\ntheme = "dark"\n',
        )
        self._declare("codex", 'model = "new"\n')

        apply(self.paths)

        text = self.clis["codex"].config_path.read_text(encoding="utf-8")
        self.assertIn("# my notes", text)
        config = self._read_config("codex")
        self.assertEqual(config["model"], "new")
        self.assertEqual(config["tui"], {"theme": "dark"})

    def test_a_top_level_key_is_not_confused_with_a_table_key(self) -> None:
        """A bare `key = value` after a table header belongs to that table.
        Rewriting it there would move the setting into somebody's [tui] block."""
        original = 'other = 1\n\n[tui]\nmodel = "table-scoped"\n'

        merged = merge_toml_text(original, {"model": "root"})

        parsed = tomllib.loads(merged)
        self.assertEqual(parsed["model"], "root")
        self.assertEqual(parsed["tui"]["model"], "table-scoped")

    def test_a_declared_toml_table_is_refused_not_half_written(self) -> None:
        self._declare("codex", '[hooks]\nbefore = "x"\n')

        plan = plan_cli(self.paths.root, self.clis["codex"])

        self.assertIsNotNone(plan.error)
        self.assertIn("tables are not supported", plan.error)

    def test_credential_shaped_values_are_refused(self) -> None:
        """The desired-state files are committed, so a literal here would be a
        secret in version control."""
        self._declare("claude", '{"apiKey": "sk-live-abc123"}')

        desired, error = read_desired(self.paths.root, self.clis["claude"])

        self.assertEqual(desired, {})
        self.assertIsNotNone(error)
        self.assertIn("credential-shaped", error)

    def test_an_environment_variable_name_is_allowed(self) -> None:
        """Referencing where a secret lives is fine; carrying its value is not."""
        self._declare("claude", '{"apiKeyEnvVar": "ANTHROPIC_API_KEY"}')
        desired, error = read_desired(self.paths.root, self.clis["claude"])

        # The key still looks credential-shaped, so it is refused. Naming the
        # variable in an unambiguous key is the supported form.
        self.assertIsNotNone(error)

        self._declare("claude", '{"authSource": "ANTHROPIC_API_KEY"}')
        desired, error = read_desired(self.paths.root, self.clis["claude"])
        self.assertIsNone(error)
        self.assertEqual(desired, {"authSource": "ANTHROPIC_API_KEY"})

    def test_an_unparseable_config_is_reported_and_not_written(self) -> None:
        broken = '{"model": '
        self._write_config("claude", broken)
        self._declare("claude", '{"model": "new"}')

        report = apply(self.paths)

        self.assertTrue(report.failures)
        self.assertEqual(self.clis["claude"].config_path.read_text(encoding="utf-8"), broken)

    def test_one_bad_target_rolls_the_whole_run_back(self) -> None:
        """A half-applied run across three agents is worse than none: the
        operator would have to work out which two of three now disagree."""
        self._write_config("claude", '{"model": "old"}')
        self._declare("claude", '{"model": "new"}')
        # Codex config is a directory, so writing it raises after Claude wrote.
        self.clis["codex"].config_path.mkdir(parents=True)
        self._declare("codex", 'model = "new"\n')

        report = apply(self.paths)

        self.assertFalse(report.applied)
        self.assertTrue(report.rolled_back)
        self.assertEqual(self._read_config("claude")["model"], "old")

    def test_applying_twice_changes_nothing(self) -> None:
        self._write_config("claude", '{"theme": "dark"}')
        self._declare("claude", '{"model": "new"}')

        apply(self.paths)
        first = self.clis["claude"].config_path.read_text(encoding="utf-8")
        second_report = apply(self.paths)

        self.assertTrue(second_report.plans["claude"].is_noop)
        self.assertEqual(self.clis["claude"].config_path.read_text(encoding="utf-8"), first)

    def test_applying_backs_the_original_up(self) -> None:
        self._write_config("claude", '{"model": "old"}')
        self._declare("claude", '{"model": "new"}')

        apply(self.paths)

        backup = self.clis["claude"].config_path.with_name("settings.json.agentbot-backup")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), {"model": "old"})

    def test_preview_writes_nothing(self) -> None:
        self._write_config("claude", '{"model": "old"}')
        self._declare("claude", '{"model": "new"}')

        report = preview(self.paths)

        self.assertTrue(report.has_work)
        self.assertEqual(self._read_config("claude")["model"], "old")

    def test_doctor_reports_one_row_per_cli(self) -> None:
        rows = doctor_cli_configs(self.paths)

        self.assertEqual([row[0] for row in rows], ["claude config", "codex config", "cursor config"])


if __name__ == "__main__":
    unittest.main()
