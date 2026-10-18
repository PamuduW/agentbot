from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import atlassian_token as store
from tests.support import run_cli_main

VALUES = {
    "ATLASSIAN_MCP_CLOUD_ID": "11111111-2222-3333-4444-555555555555",
    "ATLASSIAN_MCP_EMAIL": "reader@example.test",
    "JIRA_MCP_READ_TOKEN": "jira-test-read-token",
    "CONFLUENCE_MCP_READ_TOKEN": "confluence-test-read-token",
}


class AtlassianTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_home = self.root / "config" / "agentbot"

    def test_round_trip_is_private_and_environment_overrides_saved_values(self) -> None:
        store.write(self.config_home, VALUES)
        path = self.config_home / "atlassian.json"
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.config_home.stat().st_mode))
        self.assertEqual(VALUES, store.resolve(self.config_home, {}))
        self.assertEqual(
            "override-token",
            store.resolve(self.config_home, {"JIRA_MCP_READ_TOKEN": "override-token"})[
                "JIRA_MCP_READ_TOKEN"
            ],
        )
        self.assertEqual(["atlassian.json"], sorted(p.name for p in self.config_home.iterdir()))

    def test_empty_store_does_not_create_files(self) -> None:
        self.assertEqual({}, store.read(self.config_home))
        self.assertFalse(self.config_home.exists())
        self.assertFalse(store.remove(self.config_home))
        with self.assertRaises(store.TokenError):
            store.resolve(self.config_home, {})

    def test_bad_configuration_is_not_written_or_echoed(self) -> None:
        for key, value in (
            ("ATLASSIAN_MCP_CLOUD_ID", "../secret"),
            ("ATLASSIAN_MCP_EMAIL", "not-email"),
            ("JIRA_MCP_READ_TOKEN", ""),
            ("CONFLUENCE_MCP_READ_TOKEN", "line\nbreak"),
        ):
            with self.subTest(key=key), self.assertRaises(store.TokenError) as caught:
                store.write(self.config_home, {**VALUES, key: value})
            self.assertNotIn(VALUES["JIRA_MCP_READ_TOKEN"], str(caught.exception))
            self.assertFalse(self.config_home.exists())
        with self.assertRaises(store.TokenError):
            store.write(self.config_home, {**VALUES, "unknown": "secret"})

    def test_unsafe_files_and_symlinks_are_refused(self) -> None:
        store.write(self.config_home, VALUES)
        path = self.config_home / "atlassian.json"
        path.chmod(0o644)
        for operation in (store.read, store.remove):
            with self.assertRaises(store.TokenError):
                operation(self.config_home)
        with self.assertRaises(store.TokenError):
            store.write(self.config_home, VALUES)
        path.chmod(0o600)
        path.unlink()
        target = self.root / "outside"
        target.write_text("user-owned")
        path.symlink_to(target)
        for operation in (store.read, store.remove):
            with self.assertRaises(store.TokenError):
                operation(self.config_home)
        with self.assertRaises(store.TokenError):
            store.write(self.config_home, VALUES)
        self.assertEqual("user-owned", target.read_text())

    def test_unsafe_directory_and_malformed_saved_content_are_refused(self) -> None:
        store.write(self.config_home, VALUES)
        self.config_home.chmod(0o755)
        with self.assertRaises(store.TokenError):
            store.read(self.config_home)
        with self.assertRaises(store.TokenError):
            store.write(self.config_home, VALUES)
        self.config_home.chmod(0o700)
        path = self.config_home / "atlassian.json"
        path.write_text("not JSON jira-test-read-token")
        with self.assertRaises(store.TokenError) as caught:
            store.read(self.config_home)
        self.assertNotIn("jira-test-read-token", str(caught.exception))

    def test_failed_replace_preserves_saved_values_and_removes_temporary(self) -> None:
        store.write(self.config_home, VALUES)
        with patch.object(os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(store.TokenError):
                store.write(self.config_home, {**VALUES, "JIRA_MCP_READ_TOKEN": "replacement"})
        self.assertEqual(VALUES, store.read(self.config_home))
        self.assertEqual(["atlassian.json"], sorted(p.name for p in self.config_home.iterdir()))

    def test_complete_environment_does_not_need_an_unsafe_saved_file(self) -> None:
        store.write(self.config_home, VALUES)
        (self.config_home / "atlassian.json").chmod(0o644)
        self.assertEqual(VALUES, store.resolve(self.config_home, VALUES))

    def test_filesystem_inspection_failures_are_sanitized(self) -> None:
        store.write(self.config_home, VALUES)
        with patch.object(Path, "stat", side_effect=PermissionError("synthetic denial")):
            for operation in (store.read, store.remove):
                with self.assertRaises(store.TokenError):
                    operation(self.config_home)

    def test_deeply_nested_saved_json_is_sanitized(self) -> None:
        store.write(self.config_home, VALUES)
        (self.config_home / "atlassian.json").write_text("[" * 10000 + "0" + "]" * 10000)
        with self.assertRaises(store.TokenError):
            store.read(self.config_home)
        self.assertEqual(1, self._run("set", stdin="[" * 10000 + "0" + "]" * 10000)[0])

    def _run(self, *args: str, stdin: str = "") -> tuple[int, str, str]:
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}, clear=True),
            patch("sys.stdin", io.StringIO(stdin)),
        ):
            return run_cli_main(["agentbot", "--root", str(Path.cwd()), "atlassian-token", *args])

    def test_cli_set_and_status_show_fingerprints_without_credentials(self) -> None:
        rc, output, error = self._run("set", stdin=json.dumps(VALUES))
        self.assertEqual(0, rc, error)
        self.assertEqual(VALUES, store.read(self.config_home))
        rc, output, error = self._run("status")
        self.assertEqual(0, rc, error)
        for token in (VALUES["JIRA_MCP_READ_TOKEN"], VALUES["CONFLUENCE_MCP_READ_TOKEN"]):
            self.assertNotIn(token, output)
            self.assertIn(token[-4:], output)
        self.assertEqual(0, self._run("remove")[0])
        self.assertEqual(1, self._run("status")[0])

    def test_cli_malformed_input_returns_sanitized_failure(self) -> None:
        rc, output, error = self._run("set", stdin="malformed jira-test-read-token")
        self.assertEqual(1, rc)
        self.assertNotIn("jira-test-read-token", output + error)
        self.assertFalse(self.config_home.exists())

    def test_mcp_status_accepts_saved_configuration_without_exports(self) -> None:
        from src.mcp_service import McpService
        from src.paths import AgentbotPaths

        paths = AgentbotPaths(
            root=Path.cwd(),
            codex_home=self.root / ".codex",
            claude_home=self.root / ".claude",
            cursor_home=self.root / ".cursor",
            config_home=self.config_home,
            agents_home=self.root / ".agents",
        )
        service = McpService(paths, environ={})
        service.setup(("atlassian_read",), ("codex",), confirmed=True)
        item = next(
            item
            for item in service.status().items
            if item.catalog_id == "atlassian_read" and item.client == "codex"
        )
        self.assertEqual("auth-reference-missing", item.status)
        self.assertEqual("info", item.severity)
        store.write(self.config_home, VALUES)
        item = next(
            item
            for item in service.status().items
            if item.catalog_id == "atlassian_read" and item.client == "codex"
        )
        self.assertEqual("ok", item.status)
        (self.config_home / "atlassian.json").write_text("{}", encoding="utf-8")
        item = next(
            item for item in service.status().items
            if item.catalog_id == "atlassian_read" and item.client == "codex"
        )
        self.assertEqual("config-invalid", item.status)
        self.assertEqual("error", item.severity)

    def test_mcp_startup_uses_saved_credentials_without_exports(self) -> None:
        from unittest.mock import AsyncMock

        from src.atlassian_read_mcp import main

        store.write(self.config_home, VALUES)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("src.atlassian_read_mcp.run_atlassian_stdio", new_callable=AsyncMock) as run,
        ):
            self.assertEqual(0, main([], config_home=self.config_home))
            self.assertEqual(
                VALUES["ATLASSIAN_MCP_CLOUD_ID"], run.call_args.args[0].client.cloud_id
            )


if __name__ == "__main__":
    unittest.main()
