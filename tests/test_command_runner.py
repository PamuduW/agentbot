from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class CommandRunnerTests(unittest.TestCase):
    def test_success_and_nonzero_results_are_captured(self) -> None:
        from src.command_runner import CommandRunner

        runner = CommandRunner()
        success = runner.run([sys.executable, "-c", "print('ready')"], timeout_seconds=5)
        failure = runner.run(
            [
                sys.executable,
                "-c",
                "import sys; print('bad', file=sys.stderr); raise SystemExit(7)",
            ],
            timeout_seconds=5,
        )

        self.assertEqual(0, success.returncode)
        self.assertEqual("ready\n", success.stdout)
        self.assertEqual(7, failure.returncode)
        self.assertIn("bad", failure.detail())

    def test_timeout_and_missing_executable_are_results_not_exceptions(self) -> None:
        from src.command_runner import CommandRunner

        runner = CommandRunner()
        timed_out = runner.run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout_seconds=0.01,
        )
        missing = runner.run(["agentbot-command-that-does-not-exist"], timeout_seconds=1)

        self.assertTrue(timed_out.timed_out)
        self.assertIn("timed out", timed_out.detail())
        self.assertTrue(missing.missing_executable)
        self.assertIn("executable not found", missing.detail())

    def test_detail_strips_ansi_notices_and_caps_multiline_output(self) -> None:
        from src.command_runner import CommandResult

        result = CommandResult(
            returncode=1,
            stdout="\x1b[31mfirst\x1b[0m\nsecond\nthird\nfourth\n",
            stderr="npm notice update available\nlast\n",
        )

        detail = result.detail(max_length=30)

        self.assertLessEqual(len(detail), 30)
        self.assertNotIn("\x1b", detail)
        self.assertNotIn("npm notice", detail.lower())
        self.assertIn("last", detail)

    def test_explicit_environment_values_are_redacted_from_captured_output(self) -> None:
        from src.command_runner import CommandRunner

        canary = "agentbot-env-canary-7ad91c"
        env = {**os.environ, "AGENTBOT_TEST_CANARY": canary}
        result = CommandRunner().run(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['AGENTBOT_TEST_CANARY'])",
            ],
            timeout_seconds=5,
            env=env,
        )

        self.assertNotIn(canary, result.stdout)
        self.assertNotIn(canary, result.stderr)
        self.assertNotIn(canary, result.detail())
        self.assertNotIn(canary, repr(result))

    def test_inherited_github_token_is_redacted_from_captured_results(self) -> None:
        from src.command_runner import CommandRunner

        canary = "agentbot-inherited-canary-2f4b8e"
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": canary}):
            result = CommandRunner().run(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; print(os.environ['GITHUB_TOKEN']); print(os.environ['GITHUB_TOKEN'], file=sys.stderr)",
                ],
                timeout_seconds=5,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual("[redacted]\n", result.stdout)
        self.assertEqual("[redacted]\n", result.stderr)
        self.assertNotIn(canary, result.stdout)
        self.assertNotIn(canary, result.stderr)
        self.assertNotIn(canary, result.detail())
        self.assertNotIn(canary, repr(result))

    def test_inherited_github_token_is_redacted_from_timeout_output(self) -> None:
        from src.command_runner import CommandRunner

        canary = "agentbot-timeout-canary-9c5d1a"
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": canary}):
            result = CommandRunner().run(
                [
                    sys.executable,
                    "-c",
                    "import os, sys, time; print(os.environ['GITHUB_TOKEN'], flush=True); print(os.environ['GITHUB_TOKEN'], file=sys.stderr, flush=True); time.sleep(2)",
                ],
                timeout_seconds=0.1,
            )

        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.returncode)
        self.assertEqual("[redacted]\n", result.stdout)
        self.assertEqual("[redacted]\n", result.stderr)
        self.assertNotIn(canary, result.stdout)
        self.assertNotIn(canary, result.stderr)
        self.assertNotIn(canary, result.detail())
        self.assertNotIn(canary, repr(result))

    def test_explicit_short_redact_value_is_redacted_from_captured_results(self) -> None:
        from src.command_runner import CommandRunner

        canary = "abc"
        with mock.patch.dict(os.environ, {"AGENTBOT_TEST_SHORT_CANARY": canary}, clear=True):
            result = CommandRunner().run(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; print(os.environ['AGENTBOT_TEST_SHORT_CANARY']); print(os.environ['AGENTBOT_TEST_SHORT_CANARY'], file=sys.stderr)",
                ],
                timeout_seconds=5,
                redact_values=(canary,),
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual("[redacted]\n", result.stdout)
        self.assertEqual("[redacted]\n", result.stderr)
        self.assertNotIn(canary, result.detail())
        self.assertNotIn(canary, repr(result))

    def test_inherited_ordinary_environment_value_remains_readable(self) -> None:
        from src.command_runner import CommandRunner

        readable = "agentbot-ordinary-value-31415"
        with mock.patch.dict(os.environ, {"AGENTBOT_TEST_ORDINARY": readable}):
            result = CommandRunner().run(
                [sys.executable, "-c", "import os; print(os.environ['AGENTBOT_TEST_ORDINARY'])"],
                timeout_seconds=5,
            )

        self.assertEqual(f"{readable}\n", result.stdout)

    def test_cwd_is_honored_and_argv_is_never_shell_parsed(self) -> None:
        from src.command_runner import CommandRunner

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "must-not-exist"
            result = CommandRunner().run(
                [
                    sys.executable,
                    "-c",
                    "import pathlib; print(pathlib.Path.cwd().name)",
                    ";",
                    f"touch {marker}",
                ],
                cwd=root,
                timeout_seconds=5,
            )

        self.assertEqual(0, result.returncode)
        self.assertFalse(marker.exists())
        self.assertIn(root.name, result.stdout)

    def test_interactive_runner_returns_process_status_and_missing_executable(self) -> None:
        from src.command_runner import CommandRunner

        runner = CommandRunner()
        success = runner.run_interactive(
            [sys.executable, "-c", "raise SystemExit(0)"], timeout_seconds=5
        )
        failure = runner.run_interactive(
            [sys.executable, "-c", "raise SystemExit(7)"], timeout_seconds=5
        )
        missing = runner.run_interactive(
            ["agentbot-interactive-command-that-does-not-exist"], timeout_seconds=1
        )

        self.assertEqual(0, success.returncode)
        self.assertEqual(7, failure.returncode)
        self.assertTrue(missing.missing_executable)

    def test_interactive_runner_validates_arguments_and_reports_timeout(self) -> None:
        from src.command_runner import CommandRunner

        runner = CommandRunner()
        with self.assertRaises(ValueError):
            runner.run_interactive([], timeout_seconds=1)
        with self.assertRaises(ValueError):
            runner.run_interactive([sys.executable], timeout_seconds=0)
        timed_out = runner.run_interactive(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout_seconds=0.01,
        )
        self.assertTrue(timed_out.timed_out)

        with mock.patch("src.command_runner.subprocess.run", side_effect=OSError("no pty")):
            failed = runner.run_interactive([sys.executable], timeout_seconds=1)
        self.assertEqual(126, failed.returncode)
        self.assertIn("unable to start process", failed.stderr)


if __name__ == "__main__":
    unittest.main()
