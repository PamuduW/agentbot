"""VS Code host detection, extension planning, and JSONC settings merging."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.vscode import (
    VSCodeManifestError,
    apply_settings,
    desired_settings,
    install_extensions,
    installed_extensions,
    load_manifest,
    manifest_path,
    merge_settings_text,
    plan_extensions,
    plan_settings,
    preview,
    read_settings,
    render_manifest,
    seed_manifest,
    settings_source,
    strip_jsonc,
    windows_host,
    wsl_host,
)


class VSCodeHostTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _server_with_cli(self, *builds: str) -> Path:
        server = self.root / ".vscode-server"
        (server / "extensions").mkdir(parents=True)
        for build in builds:
            cli = server / "bin" / build / "bin" / "remote-cli" / "code"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
        return server

    def test_the_wsl_host_uses_its_own_cli_not_the_one_on_path(self) -> None:
        """`code` on PATH inside WSL is the Windows binary reached through
        interop. Driving the WSL host with it installs into the other host
        while reporting success for this one."""
        server = self._server_with_cli("abc123")

        host = wsl_host(self.root)

        self.assertEqual(host.cli, server / "bin/abc123/bin/remote-cli/code")
        self.assertEqual(host.settings_path, server / "data/Machine/settings.json")
        self.assertTrue(host.available)

    def test_the_wsl_host_picks_the_most_recent_server_build(self) -> None:
        """Server builds accumulate; the newest is the one in use."""
        server = self._server_with_cli("old", "new")
        older = server / "bin/old/bin/remote-cli/code"
        newer = server / "bin/new/bin/remote-cli/code"
        os.utime(older, (1_000, 1_000))
        os.utime(newer, (2_000, 2_000))

        self.assertEqual(wsl_host(self.root).cli, newer)

    def test_a_missing_wsl_server_is_unavailable_not_an_error(self) -> None:
        host = wsl_host(self.root)

        self.assertFalse(host.available)
        self.assertFalse(host.can_install)

    def test_several_windows_profiles_refuse_to_guess(self) -> None:
        """Writing into one of several Windows accounts on a coin flip is worse
        than reporting that the host cannot be resolved."""
        for user in ("alice", "bob"):
            (self.root / "Users" / user / "AppData/Roaming/Code/User").mkdir(parents=True)

        host = windows_host(self.root)

        self.assertFalse(host.can_install)
        self.assertIn("cannot choose one", host.detail)

    def test_no_windows_profile_is_a_reported_skip(self) -> None:
        host = windows_host(self.root)

        self.assertFalse(host.can_install)
        self.assertIn("no Windows VS Code profile", host.detail)

    def test_a_single_windows_profile_resolves_both_paths(self) -> None:
        user_dir = self.root / "Users/pamud/AppData/Roaming/Code/User"
        user_dir.mkdir(parents=True)
        extensions = self.root / "Users/pamud/.vscode/extensions"
        extensions.mkdir(parents=True)
        cli = self.root / "Program Files/Microsoft VS Code/bin/code"
        cli.parent.mkdir(parents=True)
        cli.write_text("", encoding="utf-8")

        host = windows_host(self.root)

        self.assertEqual(host.settings_path, user_dir / "settings.json")
        self.assertEqual(host.extensions_dir, extensions)
        self.assertEqual(host.cli, cli)
        self.assertTrue(host.can_install)


class ExtensionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.extensions = self.root / "extensions"
        self.extensions.mkdir()

    def _host(self):
        return replace(wsl_host(self.root), extensions_dir=self.extensions)

    def _install(self, name: str) -> None:
        (self.extensions / name).mkdir()

    def test_identifiers_drop_versions_and_platform_suffixes(self) -> None:
        self._install("charliermarsh.ruff-2026.78.0-linux-x64")
        self._install("davidanson.vscode-markdownlint-0.62.1")
        (self.extensions / "extensions.json").write_text("[]", encoding="utf-8")

        self.assertEqual(
            installed_extensions(self._host()),
            ("charliermarsh.ruff", "davidanson.vscode-markdownlint"),
        )

    def test_an_unmanaged_extension_is_reported_never_removed(self) -> None:
        """Absence from the manifest is not a request to uninstall."""
        self._install("keepme.tool-1.0.0")

        plan = plan_extensions(self._host(), ["wanted.thing"])

        self.assertEqual(plan.missing, ("wanted.thing",))
        self.assertEqual(plan.unmanaged, ("keepme.tool",))

    def test_an_installed_desired_extension_is_not_reinstalled(self) -> None:
        self._install("keepme.tool-1.0.0")

        plan = plan_extensions(self._host(), ["keepme.tool"])

        self.assertTrue(plan.is_noop)

    def test_an_unavailable_host_is_a_skip_not_a_failure(self) -> None:
        plan = plan_extensions(wsl_host(self.root), ["wanted.thing"])

        self.assertIsNotNone(plan.skipped)
        self.assertEqual(plan.missing, ())


class SettingsMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Path(temporary.name) / "settings.json"

    def test_comments_and_trailing_commas_parse(self) -> None:
        self.settings.write_text(
            '{\n  // a line comment\n  "editor.fontSize": 13, /* inline */\n'
            '  "files.autoSave": "off",\n}\n',
            encoding="utf-8",
        )

        parsed, error = read_settings(self.settings)

        self.assertIsNone(error)
        self.assertEqual(parsed, {"editor.fontSize": 13, "files.autoSave": "off"})

    def test_a_url_inside_a_string_is_not_a_comment(self) -> None:
        """`//` inside a string starts no comment. Treating it as one truncates
        the value and makes the rest of the file unparseable."""
        text = '{"a": "https://example.com/x", "b": 1}'

        self.assertEqual(
            json.loads(strip_jsonc(text)),
            {"a": "https://example.com/x", "b": 1},
        )

    def test_merging_preserves_comments_and_unrelated_keys(self) -> None:
        """The reason the merge edits text rather than re-serialising a parsed
        object: re-serialising deletes every comment the operator wrote."""
        original = '{\n  // keep me\n  "editor.fontSize": 13,\n  "other": true\n}\n'

        merged = merge_settings_text(original, {"editor.fontSize": 15})

        self.assertIn("// keep me", merged)
        self.assertIn('"other": true', merged)
        self.assertEqual(json.loads(strip_jsonc(merged))["editor.fontSize"], 15)

    def test_a_key_that_is_absent_is_appended(self) -> None:
        merged = merge_settings_text('{\n  "a": 1\n}\n', {"b": "two"})

        self.assertEqual(json.loads(strip_jsonc(merged)), {"a": 1, "b": "two"})

    def test_merging_into_an_empty_object_stays_valid(self) -> None:
        merged = merge_settings_text("{}", {"a": 1})

        self.assertEqual(json.loads(strip_jsonc(merged)), {"a": 1})

    def test_an_object_value_is_replaced_whole(self) -> None:
        original = '{\n  "nested": {"keep": 1, "drop": 2},\n  "after": true\n}\n'

        merged = merge_settings_text(original, {"nested": {"only": 3}})

        parsed = json.loads(strip_jsonc(merged))
        self.assertEqual(parsed["nested"], {"only": 3})
        self.assertIs(parsed["after"], True)

    def test_an_unchanged_object_value_is_not_rewritten(self) -> None:
        """Declaring a value the file already has must cost nothing.

        An object value is replaced whole, so re-rendering one deletes any
        comment inside it. Changing a single unrelated scalar used to re-render
        every declared key, which stripped comments from blocks the operator
        never touched.
        """
        original = '{\n  "[python]": {\n    // ruff owns this\n    "editor.tabSize": 4\n  },\n  "editor.fontSize": 13\n}\n'

        merged = merge_settings_text(
            original, {"[python]": {"editor.tabSize": 4}, "editor.fontSize": 15}
        )

        self.assertIn("// ruff owns this", merged)
        self.assertIn('{\n    // ruff owns this\n    "editor.tabSize": 4\n  }', merged)
        self.assertEqual(json.loads(strip_jsonc(merged))["editor.fontSize"], 15)

    def test_a_replaced_object_is_indented_for_its_position(self) -> None:
        """json.dumps indents from column zero; the value sits one level in."""
        original = '{\n  "nested": {"a": 1}\n}\n'

        merged = merge_settings_text(original, {"nested": {"b": 2}})

        self.assertIn('  "nested": {\n    "b": 2\n  }\n', merged)

    def test_replacing_a_value_keeps_its_trailing_comment_and_newline(self) -> None:
        """The value's span ends at the value, not at the next structural
        character: everything between was the operator's, not ours."""
        merged = merge_settings_text('{\n  "a": 1 // keep this note\n}\n', {"a": 2})

        self.assertEqual(merged, '{\n  "a": 2 // keep this note\n}\n')

    def test_a_nested_key_of_the_same_name_is_left_alone(self) -> None:
        """Depth matters: replacing the inner "a" would corrupt an unrelated
        block and leave the top-level setting untouched."""
        original = '{\n  "outer": {"a": 1},\n  "a": 2\n}\n'

        merged = merge_settings_text(original, {"a": 99})

        self.assertEqual(json.loads(strip_jsonc(merged)), {"outer": {"a": 1}, "a": 99})

    def test_a_key_named_inside_a_comment_is_not_matched(self) -> None:
        original = '{\n  // "a": 1 is what we used to do\n  "a": 2\n}\n'

        merged = merge_settings_text(original, {"a": 3})

        self.assertEqual(json.loads(strip_jsonc(merged)), {"a": 3})
        self.assertIn("is what we used to do", merged)

    def test_an_existing_trailing_comma_does_not_become_two(self) -> None:
        """A trailing comma before the closing brace is legal JSONC and common
        in hand-written settings. A second one is legal nowhere -- this broke
        every real settings file that had one."""
        original = '{\n  "a": 1,\n}\n'

        merged = merge_settings_text(original, {"b": 2})

        self.assertNotIn(",,", merged)
        self.assertEqual(json.loads(strip_jsonc(merged)), {"a": 1, "b": 2})

    def test_appending_matches_the_file_s_line_endings(self) -> None:
        """Settings written on the Windows side are CRLF; appending LF would
        leave one mixed line in a file the operator maintains by hand."""
        original = '{\r\n  "a": 1\r\n}\r\n'

        merged = merge_settings_text(original, {"b": 2})

        self.assertNotIn('\n  "b"', merged.replace("\r\n", "\r"))
        self.assertEqual(json.loads(strip_jsonc(merged)), {"a": 1, "b": 2})

    def test_the_plan_separates_additions_from_changes(self) -> None:
        self.settings.write_text('{"same": 1, "differs": 2}', encoding="utf-8")

        plan = plan_settings(self.settings, {"same": 1, "differs": 3, "new": 4})

        self.assertEqual(plan.additions, {"new": 4})
        self.assertEqual(plan.changes, {"differs": (2, 3)})

    def test_an_unparseable_file_is_never_written(self) -> None:
        """A file we cannot read is a file we must not replace."""
        broken = '{"a": '
        self.settings.write_text(broken, encoding="utf-8")

        plan = apply_settings(self.settings, {"a": 1})

        self.assertIsNotNone(plan.unreadable)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), broken)

    def test_applying_backs_up_the_original(self) -> None:
        self.settings.write_text('{"a": 1}\n', encoding="utf-8")

        apply_settings(self.settings, {"a": 2})

        backup = self.settings.with_name(self.settings.name + ".agentbot-backup")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), {"a": 1})

    def test_applying_is_idempotent_and_keeps_comments(self) -> None:
        self.settings.write_text('{\n  // hi\n  "a": 1\n}\n', encoding="utf-8")

        self.assertTrue(apply_settings(self.settings, {"a": 1}).is_noop)
        apply_settings(self.settings, {"a": 2})
        after_first = self.settings.read_text(encoding="utf-8")

        self.assertTrue(apply_settings(self.settings, {"a": 2}).is_noop)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), after_first)
        self.assertIn("// hi", after_first)


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_missing_manifest_is_empty_not_an_error(self) -> None:
        """Nothing selected yet is a normal state, not a broken install."""
        manifest = load_manifest(manifest_path(self.root))

        self.assertTrue(manifest.is_empty)
        self.assertEqual(manifest.extensions_for("wsl"), ())

    def test_an_unsupported_version_is_refused(self) -> None:
        manifest_path(self.root).write_text("version: 99\n", encoding="utf-8")

        with self.assertRaises(VSCodeManifestError):
            load_manifest(manifest_path(self.root))

    def test_a_malformed_extensions_list_is_refused(self) -> None:
        manifest_path(self.root).write_text(
            "version: 1\nextensions:\n  wsl: not-a-list\n", encoding="utf-8"
        )

        with self.assertRaises(VSCodeManifestError):
            load_manifest(manifest_path(self.root))

    def test_hosts_keep_separate_lists(self) -> None:
        """An extension installed in WSL is not evidence about Windows."""
        manifest_path(self.root).write_text(
            "version: 1\nextensions:\n  wsl: [a.one]\n  windows: [b.two]\n",
            encoding="utf-8",
        )

        manifest = load_manifest(manifest_path(self.root))

        self.assertEqual(manifest.extensions_for("wsl"), ("a.one",))
        self.assertEqual(manifest.extensions_for("windows"), ("b.two",))

    def test_seeding_records_installed_extensions_and_round_trips(self) -> None:
        extensions = self.root / "wsl-extensions"
        extensions.mkdir()
        (extensions / "a.one-1.0.0").mkdir()
        host = replace(wsl_host(self.root), extensions_dir=extensions)

        seeded = seed_manifest(manifest_path(self.root), {"wsl": host})

        self.assertEqual(seeded.extensions_for("wsl"), ("a.one",))
        self.assertEqual(load_manifest(manifest_path(self.root)).extensions_for("wsl"), ("a.one",))

    def test_seeding_never_records_settings(self) -> None:
        """Copying a settings file wholesale would claim ownership of every key
        in it, and explicit ownership is the point of the manifest."""
        extensions = self.root / "wsl-extensions"
        extensions.mkdir()
        host = replace(wsl_host(self.root), extensions_dir=extensions)

        seeded = seed_manifest(manifest_path(self.root), {"wsl": host})

        self.assertNotIn("settings", render_manifest(seeded))

    def test_seeding_skips_a_host_that_is_not_there(self) -> None:
        seeded = seed_manifest(manifest_path(self.root), {"wsl": wsl_host(self.root)})

        self.assertEqual(seeded.extensions_for("wsl"), ())


class InstallExecutionTests(unittest.TestCase):
    class _Runner:
        def __init__(self, returncode: int = 0) -> None:
            self.calls: list[list[str]] = []
            self._returncode = returncode

        def run(self, argv, **kwargs):
            self.calls.append(list(argv))

            class _Result:
                returncode = self._returncode

                def detail(self, max_length: int = 240) -> str:
                    return "boom"

            return _Result()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _host(self, cli: Path | None):
        return replace(wsl_host(self.root), cli=cli)

    def test_each_extension_is_installed_through_that_host_s_cli(self) -> None:
        """One invocation per extension: a batched call reports one exit status,
        so a single failure would be recorded against all of them."""
        cli = self.root / "remote-cli-code"
        runner = self._Runner()

        results = install_extensions(self._host(cli), ("a.one", "b.two"), runner)

        self.assertEqual([call[0] for call in runner.calls], [str(cli), str(cli)])
        self.assertEqual(runner.calls[0][1:3], ["--install-extension", "a.one"])
        self.assertEqual(results, {"a.one": "installed", "b.two": "installed"})

    def test_a_host_without_a_cli_reports_rather_than_running_anything(self) -> None:
        runner = self._Runner()

        results = install_extensions(self._host(None), ("a.one",), runner)

        self.assertEqual(runner.calls, [])
        self.assertEqual(results, {"a.one": "no CLI for this host"})

    def test_a_failed_install_is_reported_against_that_extension(self) -> None:
        runner = self._Runner(returncode=1)

        results = install_extensions(self._host(self.root / "code"), ("a.one",), runner)

        self.assertEqual(results, {"a.one": "boom"})


class UniversalSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.mount = self.root / "mnt"
        (self.home / ".vscode-server/data/Machine").mkdir(parents=True)
        (self.home / ".vscode-server/extensions").mkdir(parents=True)
        windows_user = self.mount / "Users/pamud/AppData/Roaming/Code/User"
        windows_user.mkdir(parents=True)
        (self.mount / "Users/pamud/.vscode/extensions").mkdir(parents=True)
        self.wsl_settings = self.home / ".vscode-server/data/Machine/settings.json"
        self.windows_settings = windows_user / "settings.json"

    def _write_scope(self, scope: str, text: str) -> None:
        target = settings_source(self.root, scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_universal_keys_reach_every_host(self) -> None:
        """One universal settings set, applied to both files. Writing only the
        Windows user file would silently miss machine-scoped keys on the
        remote, which is what the per-host file exists to hold."""
        self._write_scope("universal", '{"editor.fontSize": 13}')

        report = preview(self.home, self.root, self.mount)

        self.assertEqual(sorted(report.settings), ["windows", "wsl"])
        for host in ("wsl", "windows"):
            self.assertEqual(report.settings[host].additions, {"editor.fontSize": 13})

    def test_a_string_valued_setting_survives_authoring(self) -> None:
        """Why settings are JSON and not a YAML block: `files.autoSave: off` is
        the string "off" to VS Code and the boolean false to YAML, and
        `editor.wordWrap: on` is the string "on". Authored as JSON, they stay
        the values VS Code actually accepts."""
        self._write_scope(
            "universal",
            '{"files.autoSave": "off", "editor.wordWrap": "on"}',
        )

        desired, error = desired_settings(self.root, "wsl")

        self.assertIsNone(error)
        self.assertEqual(desired, {"files.autoSave": "off", "editor.wordWrap": "on"})

    def test_settings_left_in_the_yaml_manifest_are_refused(self) -> None:
        """A silent move would leave the old block being ignored while the
        report claimed everything was current."""
        manifest_path(self.root).write_text(
            'version: 1\nsettings:\n  universal:\n    a: 1\n', encoding="utf-8"
        )

        report = preview(self.home, self.root, self.mount)

        self.assertIsNotNone(report.manifest_error)
        self.assertIn("settings.<scope>.json", report.manifest_error)

    def test_an_unparseable_desired_settings_file_stops_that_host(self) -> None:
        self._write_scope("universal", '{"a": ')

        report = preview(self.home, self.root, self.mount)

        self.assertIsNotNone(report.settings["wsl"].unreadable)

    def test_a_host_override_beats_the_universal_value(self) -> None:
        """An interpreter path is machine-specific whether or not the editor is."""
        self._write_scope("universal", '{"a": 1, "b": 2}')
        self._write_scope("wsl", '{"b": 99}')

        self.assertEqual(desired_settings(self.root, "wsl")[0], {"a": 1, "b": 99})
        self.assertEqual(desired_settings(self.root, "windows")[0], {"a": 1, "b": 2})

    def test_an_unavailable_host_is_reported_not_written(self) -> None:
        import shutil

        shutil.rmtree(self.mount / "Users")
        self._write_scope("universal", '{"a": 1}')

        report = preview(self.home, self.root, self.mount)

        self.assertIsNotNone(report.settings["windows"].unreadable)
        self.assertEqual(report.settings["wsl"].additions, {"a": 1})


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.mount = self.root / "mnt"
        self.mount.mkdir()

    def test_preview_writes_nothing(self) -> None:
        (self.home / ".vscode-server/extensions").mkdir(parents=True)
        settings = self.home / ".vscode-server/data/Machine/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"a": 1}\n', encoding="utf-8")
        scope = settings_source(self.root, "wsl")
        scope.parent.mkdir(parents=True)
        scope.write_text('{"a": 2}', encoding="utf-8")

        report = preview(self.home, self.root, self.mount)

        self.assertTrue(report.has_work)
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"a": 1}\n')
        self.assertFalse(report.applied)

    def test_a_broken_manifest_stops_before_any_planning(self) -> None:
        manifest_path(self.root).write_text("version: 99\n", encoding="utf-8")

        report = preview(self.home, self.root, self.mount)

        self.assertIsNotNone(report.manifest_error)
        self.assertEqual(report.extensions, {})


if __name__ == "__main__":
    unittest.main()
