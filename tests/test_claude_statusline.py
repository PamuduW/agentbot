import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import agentbot_paths


class ClaudeStatuslineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.claude_home = self.root / "home" / ".claude"
        source_dir = self.root / "global" / "claude"
        source_dir.mkdir(parents=True)
        self.source = source_dir / "statusline-command.sh"
        self.source.write_text(
            "#!/bin/bash\n"
            "# Managed by Agentbot. Edit global/claude/statusline-command.sh, then run ./install.sh global.\n"
            "echo statusline\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _paths(self):
        return agentbot_paths(self.root)

    def _run_real_statusline(
        self, payload: dict | str, **env_overrides: str
    ) -> subprocess.CompletedProcess[str]:
        script = Path(__file__).resolve().parents[1] / "global/claude/statusline-command.sh"
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["bash", str(script)],
            input=raw,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "TZ": "UTC", **env_overrides},
        )

    @staticmethod
    def _plain_output(result: subprocess.CompletedProcess[str]) -> str:
        return re.sub(r"\033\[[0-9;]*m", "", result.stdout).strip()

    def test_real_statusline_preserves_sparse_fields(self) -> None:
        result = self._run_real_statusline(
            {
                "workspace": {"current_dir": str(self.root)},
                "model": {"display_name": "High"},
                "context_window": {"used_percentage": 42},
            }
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertIn("High", result.stdout)
        self.assertIn("Context 42% used", self._plain_output(result))
        self.assertNotIn("High (42)", result.stdout)

    def test_real_statusline_accepts_null_pre_response_fields(self) -> None:
        result = self._run_real_statusline(
            {
                "workspace": {"current_dir": str(self.root)},
                "model": {"display_name": "Opus"},
                "output_style": None,
                "context_window": None,
                "effort": None,
                "thinking": None,
            }
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertIn("Opus", result.stdout)

    def test_real_statusline_falls_back_for_malformed_json(self) -> None:
        result = self._run_real_statusline("{not-json")

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual("Claude Code\n", result.stdout)

    def test_real_statusline_renders_complete_high_effort_payload(self) -> None:
        result = self._run_real_statusline(
            {
                "workspace": {"current_dir": str(self.root)},
                "model": {"display_name": "Opus 4.1 (1M context)"},
                "output_style": {"name": "default"},
                "context_window": {
                    "used_percentage": 42,
                    "remaining_percentage": 58,
                    "total_input_tokens": 420000,
                    "context_window_size": 1000000,
                },
                "effort": {"level": "high"},
                "thinking": {"enabled": True},
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 24,
                        "resets_at": 4102444800,
                    },
                    "seven_day": {
                        "used_percentage": 41,
                        "resets_at": 4102444800,
                    },
                },
            },
            COLUMNS="240",
            AGENTBOT_STATUSLINE_BOOST="0",
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual(
            f"Opus 4.1 High (1M context) · {self.root} · "
            "Context 42% used · 5h 76% left (reset Jan 1 00:00) · "
            "7d 59% left (reset Jan 1 00:00)",
            self._plain_output(result),
        )

    def test_real_statusline_wraps_complete_payload_at_segment_boundaries(
        self,
    ) -> None:
        result = self._run_real_statusline(
            {
                "workspace": {"current_dir": str(self.root)},
                "model": {"display_name": "Opus 4.1 (1M context)"},
                "context_window": {
                    "used_percentage": 42,
                    "remaining_percentage": 58,
                },
                "effort": {"level": "high"},
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 24,
                        "resets_at": 4102444800,
                    },
                    "seven_day": {
                        "used_percentage": 41,
                        "resets_at": 4102444800,
                    },
                },
            },
            COLUMNS="72",
            AGENTBOT_STATUSLINE_BOOST="0",
        )
        lines = self._plain_output(result).splitlines()

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual(2, len(lines))
        self.assertTrue(all(len(line) <= 72 for line in lines), lines)
        combined = " · ".join(lines)
        for expected in (
            "Opus 4.1",
            self.root.name,
            "Context 42%",
            "5h 76%",
            "7d 59%",
        ):
            self.assertIn(expected, combined)

    def test_real_statusline_ignores_invalid_columns(self) -> None:
        result = self._run_real_statusline(
            {
                "workspace": {"current_dir": str(self.root)},
                "model": {"display_name": "Opus"},
                "context_window": {"used_percentage": 10},
            },
            COLUMNS="wide",
            AGENTBOT_STATUSLINE_BOOST="0",
        )

        self.assertEqual(
            f"Opus · {self.root} · Context 10% used",
            self._plain_output(result),
        )

    def test_real_statusline_hides_unavailable_rate_limits(self) -> None:
        result = self._run_real_statusline(
            {
                "workspace": {"current_dir": str(self.root)},
                "model": {"display_name": "Opus"},
                "context_window": {
                    "used_percentage": 10,
                    "remaining_percentage": 90,
                },
            }
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual(
            f"Opus · {self.root} · Context 10% used",
            self._plain_output(result),
        )

    def test_install_creates_executable_script_and_settings(self) -> None:
        from src.claude_statusline import install_claude_statusline

        install_claude_statusline(self._paths())

        destination = self.claude_home / "statusline-command.sh"
        self.assertTrue(destination.is_file())
        self.assertTrue(destination.stat().st_mode & 0o111)
        self.assertIn("Managed by Agentbot", destination.read_text(encoding="utf-8"))

        settings = json.loads((self.claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {"type": "command", "command": "~/.claude/statusline-command.sh"},
            settings["statusLine"],
        )

    def test_install_merges_settings_without_clobbering_other_keys(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        (self.claude_home / "settings.json").write_text(
            json.dumps({"theme": "dark", "model": "opus"}, indent=2) + "\n",
            encoding="utf-8",
        )

        install_claude_statusline(self._paths())

        settings = json.loads((self.claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual("dark", settings["theme"])
        self.assertEqual("opus", settings["model"])
        self.assertEqual("~/.claude/statusline-command.sh", settings["statusLine"]["command"])

    def test_settings_fsync_failure_preserves_original_json_and_mode(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        settings_path = self.claude_home / "settings.json"
        original = json.dumps({"theme": "dark", "model": "opus"}, indent=2) + "\n"
        settings_path.write_text(original, encoding="utf-8")
        os.chmod(settings_path, 0o640)
        real_fsync = os.fsync

        def fail_settings_fsync(fd: int) -> None:
            try:
                name = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                name = ""
            if "settings.json" in Path(name).name:
                raise OSError("temporary write failed")
            real_fsync(fd)

        with (
            mock.patch("os.fsync", side_effect=fail_settings_fsync),
            self.assertRaisesRegex(OSError, "temporary write failed"),
        ):
            install_claude_statusline(self._paths())

        self.assertEqual(original, settings_path.read_text(encoding="utf-8"))
        self.assertEqual(0o640, stat.S_IMODE(settings_path.stat().st_mode))
        self.assertEqual(
            [],
            list(self.claude_home.glob(".settings.json.agentbot-*")),
        )

    def test_settings_replace_failure_preserves_original_json(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        settings_path = self.claude_home / "settings.json"
        original = json.dumps({"theme": "dark", "model": "opus"}, indent=2) + "\n"
        settings_path.write_text(original, encoding="utf-8")
        real_replace = os.replace

        def fail_settings_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            if Path(dst).name == "settings.json":
                raise OSError("replace failed")
            real_replace(src, dst)

        with (
            mock.patch("os.replace", side_effect=fail_settings_replace),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            install_claude_statusline(self._paths())

        self.assertEqual(original, settings_path.read_text(encoding="utf-8"))
        json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [],
            list(self.claude_home.glob(".settings.json.agentbot-*")),
        )

    def test_settings_symlink_destination_is_rejected(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        real_settings = self.root / "real-settings.json"
        original = json.dumps({"theme": "dark"}, indent=2) + "\n"
        real_settings.write_text(original, encoding="utf-8")
        (self.claude_home / "settings.json").symlink_to(real_settings)

        with self.assertRaisesRegex(ValueError, "symlink"):
            install_claude_statusline(self._paths())

        self.assertEqual(original, real_settings.read_text(encoding="utf-8"))
        self.assertTrue((self.claude_home / "settings.json").is_symlink())

    def test_install_preserves_foreign_statusline_command(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        (self.claude_home / "settings.json").write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": "~/.claude/custom-statusline.sh",
                        "padding": 2,
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        install_claude_statusline(self._paths())

        settings = json.loads((self.claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual("~/.claude/custom-statusline.sh", settings["statusLine"]["command"])
        self.assertEqual(2, settings["statusLine"]["padding"])

    def test_install_only_replaces_exact_managed_statusline_commands(self) -> None:
        from src.claude_statusline import install_claude_statusline

        cases = {
            "~/.claude/statusline-command.sh": "~/.claude/statusline-command.sh",
            "bash ~/.claude/statusline-command.sh": "~/.claude/statusline-command.sh",
            "/usr/bin/bash ~/.claude/statusline-command.sh": "~/.claude/statusline-command.sh",
            "/tmp/custom/statusline-command.sh": "/tmp/custom/statusline-command.sh",
            "bash /tmp/custom/statusline-command.sh": "bash /tmp/custom/statusline-command.sh",
            "~/.claude/statusline-command.sh --compact": "~/.claude/statusline-command.sh --compact",
            "bash -c ~/.claude/statusline-command.sh": "bash -c ~/.claude/statusline-command.sh",
            "printf statusline-command.sh": "printf statusline-command.sh",
            "bash '~/.claude/statusline-command.sh": "bash '~/.claude/statusline-command.sh",
        }

        for command, expected in cases.items():
            with self.subTest(command=command):
                settings_path = self.claude_home / "settings.json"
                self.claude_home.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(
                    json.dumps({"statusLine": {"type": "command", "command": command}}) + "\n",
                    encoding="utf-8",
                )

                install_claude_statusline(self._paths())

                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(expected, settings["statusLine"]["command"])

    def test_install_preserves_user_authored_script(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        destination = self.claude_home / "statusline-command.sh"
        custom = "#!/bin/bash\necho custom-user-statusline\n"
        destination.write_text(custom, encoding="utf-8")

        install_claude_statusline(self._paths())

        self.assertEqual(custom, destination.read_text(encoding="utf-8"))
        self.assertTrue(destination.stat().st_mode & 0o111)

    def test_install_refreshes_managed_script(self) -> None:
        from src.claude_statusline import install_claude_statusline

        self.claude_home.mkdir(parents=True)
        destination = self.claude_home / "statusline-command.sh"
        destination.write_text(
            "#!/bin/bash\n# Managed by Agentbot.\necho old\n",
            encoding="utf-8",
        )

        install_claude_statusline(self._paths())

        refreshed = destination.read_text(encoding="utf-8")
        self.assertIn("echo statusline", refreshed)
        self.assertNotIn("echo old", refreshed)

    def test_render_global_outputs_installs_statusline(self) -> None:
        from unittest import mock

        from src.render import render_global_outputs

        (self.root / "global").mkdir(exist_ok=True)
        (self.root / "global" / "AGENTS.md").write_text("# Global\n", encoding="utf-8")
        agents_home = self.root / "home" / ".agents" / "skills"
        agents_home.mkdir(parents=True)

        paths = self._paths()
        with mock.patch.object(
            type(paths),
            "agents_skills_home",
            new_callable=lambda: property(lambda self: agents_home),
        ):
            render_global_outputs(paths)

        self.assertTrue((self.claude_home / "statusline-command.sh").is_file())
        settings = json.loads((self.claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual("~/.claude/statusline-command.sh", settings["statusLine"]["command"])

    def test_doctor_reports_stale_and_missing_jq(self) -> None:
        from unittest import mock

        from src.claude_statusline import doctor_claude_statusline, install_claude_statusline

        paths = self._paths()
        install_claude_statusline(paths)
        destination = self.claude_home / "statusline-command.sh"
        destination.write_text(
            "#!/bin/bash\n# Managed by Agentbot.\necho stale\n",
            encoding="utf-8",
        )

        with mock.patch("src.claude_statusline.shutil.which", return_value=None):
            messages = [issue.message for issue in doctor_claude_statusline(paths)]

        self.assertTrue(any("stale" in message for message in messages))
        self.assertTrue(any("jq is not installed" in message for message in messages))

    def test_doctor_reports_missing_install(self) -> None:
        from src.claude_statusline import doctor_claude_statusline

        messages = [issue.message for issue in doctor_claude_statusline(self._paths())]
        self.assertTrue(any("not installed" in message for message in messages))


if __name__ == "__main__":
    unittest.main()


class BoostWrappedStatuslineTests(ClaudeStatuslineTests):
    """Boost edits this script in place, so the Agentbot marker survives.

    Without an explicit check the managed path reads that as "stale" and
    reverts it -- and `agentbot boost setup` refreshes outputs right after
    `boost init`, so the revert would land in the same command.
    """

    WRAPPED = (
        "#!/bin/bash\n"
        "# Managed by Agentbot. Edit global/claude/statusline-command.sh, then run ./install.sh global.\n"
        "# boost-status-line-prev-command: ~/.claude/statusline-command.sh.orig\n"
        "printf '%s' \"$input\" | boost status-line 2>/dev/null || true\n"
    )

    def _install_wrapped(self):
        from src.claude_statusline import install_claude_statusline

        destination = self.claude_home / "statusline-command.sh"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.WRAPPED, encoding="utf-8")
        return destination, install_claude_statusline(self._paths())

    def test_a_boost_wrapped_statusline_is_not_reverted(self):
        destination, result = self._install_wrapped()

        self.assertEqual("preserved_boost", result.script_action)
        self.assertEqual(self.WRAPPED, destination.read_text(encoding="utf-8"))
        self.assertTrue(result.state.boost_wrapped)
        self.assertEqual("boost-wrapped", result.state.status_label)
        self.assertEqual("check", result.state.status_result)

    def test_preserving_it_is_reported_not_silent(self):
        from src.claude_statusline import doctor_claude_statusline

        self._install_wrapped()
        messages = [
            issue.message
            for issue in doctor_claude_statusline(self._paths())
            if issue.scope == "claude-statusline"
        ]

        self.assertTrue(
            any("wrapped by Boost" in message for message in messages),
            f"expected a Boost statusline warning, got {messages}",
        )

    def test_an_unwrapped_managed_statusline_still_refreshes(self):
        from src.claude_statusline import install_claude_statusline

        destination = self.claude_home / "statusline-command.sh"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "#!/bin/bash\n# Managed by Agentbot. stale\necho old\n", encoding="utf-8"
        )

        result = install_claude_statusline(self._paths())

        self.assertEqual("updated", result.script_action)
        self.assertFalse(result.state.boost_wrapped)
        self.assertEqual(
            self.source.read_text(encoding="utf-8"),
            destination.read_text(encoding="utf-8"),
        )


class BoostStatuslineSegmentTests(unittest.TestCase):
    """The managed script calls `boost status-line` itself.

    Agentbot owns this file, so it must never be wrapped by Boost's own
    status-line component -- we render the segment instead.
    """

    SCRIPT = Path(__file__).resolve().parents[1] / "global/claude/statusline-command.sh"
    PAYLOAD = json.dumps(
        {
            "workspace": {"current_dir": "/tmp"},
            "model": {"display_name": "Opus 5"},
            "context_window": {"used_percentage": 42},
        }
    )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.stub_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _stub_boost(self, body: str) -> None:
        stub = self.stub_dir / "boost"
        stub.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)

    def _run(self, **env_overrides) -> str:
        env = {
            **os.environ,
            "TZ": "UTC",
            "PATH": f"{self.stub_dir}:{os.environ['PATH']}",
            **env_overrides,
        }
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            input=self.PAYLOAD,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return re.sub(r"\033\[[0-9;]*m", "", result.stdout).strip()

    def test_savings_are_rendered_as_a_segment(self):
        self._stub_boost("cat >/dev/null; printf 'Boost saved 12.4k tokens\\n'")
        self.assertIn("Boost saved 12.4k tokens", self._run())

    def test_boost_colors_are_stripped_and_padding_trimmed(self):
        self._stub_boost("cat >/dev/null; printf '\\033[32m   Boost saved 3 tokens   \\033[0m\\n'")
        output = self._run()
        self.assertIn("· Boost saved 3 tokens", output)
        self.assertNotIn("   Boost", output)

    def test_no_savings_means_no_segment(self):
        # The common case: Boost prints nothing until something is filtered.
        self._stub_boost("cat >/dev/null")
        output = self._run()
        self.assertIn("Context 42% used", output)
        self.assertNotIn("Boost", output)

    def test_a_failing_boost_never_breaks_the_statusline(self):
        self._stub_boost("exit 3")
        output = self._run()
        self.assertIn("Opus 5", output)
        self.assertIn("Context 42% used", output)
        self.assertNotIn("Boost", output)

    def test_the_segment_can_be_switched_off(self):
        self._stub_boost("cat >/dev/null; printf 'Boost saved 12.4k tokens\\n'")
        output = self._run(AGENTBOT_STATUSLINE_BOOST="0")
        self.assertNotIn("Boost", output)
        self.assertIn("Context 42% used", output)

    def test_narrow_output_keeps_the_boost_segment(self):
        self._stub_boost("cat >/dev/null; printf 'Boost saved 123456789 tokens total\\n'")
        lines = self._run(COLUMNS="48").splitlines()

        self.assertEqual(2, len(lines))
        self.assertTrue(all(len(line) <= 48 for line in lines), lines)
        self.assertIn("Boost", " ".join(lines))

    def test_only_the_first_line_is_used(self):
        self._stub_boost("cat >/dev/null; printf 'line one\\nline two\\n'")
        output = self._run()
        self.assertIn("line one", output)
        self.assertNotIn("line two", output)


class BoostDetectionPrecisionTests(ClaudeStatuslineTests):
    """The managed script invokes `boost status-line` itself.

    Detection must key on markers Boost authors, not on that invocation, or
    Agentbot flags its own file as wrapped.
    """

    def test_the_managed_script_is_not_mistaken_for_a_boost_wrap(self):
        from src.claude_statusline import inspect_claude_statusline

        real = (
            Path(__file__).resolve().parents[1] / "global/claude/statusline-command.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("boost status-line", real, "expected the savings segment")

        self.source.write_text(real, encoding="utf-8")
        destination = self.claude_home / "statusline-command.sh"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(real, encoding="utf-8")

        self.assertFalse(inspect_claude_statusline(self._paths()).boost_wrapped)

    def test_a_stale_managed_script_is_not_mistaken_either(self):
        from src.claude_statusline import inspect_claude_statusline

        destination = self.claude_home / "statusline-command.sh"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "#!/bin/bash\n# Managed by Agentbot. older\n"
            '"$BOOST_CMD" status-line 2>/dev/null || true\n',
            encoding="utf-8",
        )

        state = inspect_claude_statusline(self._paths())
        self.assertFalse(state.boost_wrapped)
        self.assertEqual("stale", state.status_label)

    def test_boost_authored_markers_are_still_detected(self):
        from src.claude_statusline import inspect_claude_statusline

        destination = self.claude_home / "statusline-command.sh"
        destination.parent.mkdir(parents=True, exist_ok=True)
        for marker in ("# boost-status-line-prev-command: old", "# boost-hook-version: 1"):
            destination.write_text(
                f"#!/bin/bash\n# Managed by Agentbot.\n{marker}\necho hi\n",
                encoding="utf-8",
            )
            self.assertTrue(
                inspect_claude_statusline(self._paths()).boost_wrapped,
                f"{marker} should read as wrapped",
            )
