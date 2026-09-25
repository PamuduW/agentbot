"""Codex Remote Control is driven through Codex's own daemon command."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import codex_remote_control
from src.command_runner import CommandResult
from tests.support import agentbot_paths

_CONNECTED = json.dumps({"mode": "daemon", "status": "connected", "daemon": {}})


class CodexRemoteControlTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = agentbot_paths(Path(temporary.name))
        self.paths.codex_home.mkdir(parents=True)
        self.runner = mock.Mock()
        which = mock.patch.object(codex_remote_control.shutil, "which", return_value="/bin/codex")
        self.which = which.start()
        self.addCleanup(which.stop)

    def test_missing_codex_home_is_a_skip_and_runs_nothing(self) -> None:
        self.paths.codex_home.rmdir()

        self.assertEqual("skipped", codex_remote_control.ensure(self.paths, self.runner)[1])
        self.assertEqual("skipped", codex_remote_control.inspect(self.paths, self.runner)[1])
        self.runner.run.assert_not_called()

    def _persist(self, enabled: bool) -> None:
        settings = self.paths.codex_home / "app-server-daemon" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"remoteControlEnabled": enabled}), encoding="utf-8")

    def test_missing_codex_is_a_skip_and_runs_nothing(self) -> None:
        self.which.return_value = None

        self.assertEqual("skipped", codex_remote_control.ensure(self.paths, self.runner)[1])
        self.assertEqual("skipped", codex_remote_control.inspect(self.paths, self.runner)[1])
        self.runner.run.assert_not_called()

    def test_ensure_starts_the_daemon_with_remote_control(self) -> None:
        self.runner.run.return_value = CommandResult(0, stdout=_CONNECTED)

        detail, result = codex_remote_control.ensure(self.paths, self.runner)

        self.assertEqual("ok", result, detail)
        argv = self.runner.run.call_args.args[0]
        self.assertEqual(["/bin/codex", "remote-control", "start", "--json"], argv)

    def test_ensure_reports_a_failed_start(self) -> None:
        self.runner.run.return_value = CommandResult(1, stderr="not logged in")

        detail, result = codex_remote_control.ensure(self.paths, self.runner)

        self.assertEqual("check", result)
        self.assertIn("not logged in", detail)

    def test_ensure_reports_a_relay_that_did_not_connect(self) -> None:
        self.runner.run.return_value = CommandResult(0, stdout='{"status": "timedOut"}')

        detail, result = codex_remote_control.ensure(self.paths, self.runner)

        self.assertEqual("check", result)
        self.assertIn("timedOut", detail)

    def test_inspect_flags_remote_control_that_is_off(self) -> None:
        self._persist(False)

        self.assertEqual("check", codex_remote_control.inspect(self.paths, self.runner)[1])
        self.runner.run.assert_not_called()

    def test_inspect_accepts_a_stopped_daemon_once_persisted(self) -> None:
        """After a reboot the next `codex` session starts the daemon again."""
        self._persist(True)
        self.runner.run.return_value = CommandResult(1)

        detail, result = codex_remote_control.inspect(self.paths, self.runner)

        self.assertEqual("ok", result)
        self.assertIn("next codex session", detail)

    def test_inspect_never_starts_anything(self) -> None:
        self._persist(True)
        self.runner.run.return_value = CommandResult(0, stdout="{}")

        codex_remote_control.inspect(self.paths, self.runner)

        argv = self.runner.run.call_args.args[0]
        self.assertEqual(["/bin/codex", "app-server", "daemon", "version"], argv)


if __name__ == "__main__":
    unittest.main()
