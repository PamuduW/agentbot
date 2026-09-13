from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.mcp_models import McpManagedRecord, McpState
from src.mcp_render import entry_fingerprint
from src.mcp_service import McpService
from src.paths import AgentbotPaths
from src.ui.reports import print_mcp_catalog, print_mcp_status
from tests.support import run_cli_main


class McpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        product = root / "agentbot"
        (product / "mcp").mkdir(parents=True)
        (product / "mcp" / "catalog.json").write_text(
            '{"version":1,"entries":[]}\n', encoding="utf-8"
        )
        self.paths = AgentbotPaths(
            root=product,
            codex_home=root / ".codex",
            claude_home=root / ".claude",
            cursor_home=root / ".cursor",
            config_home=root / ".config" / "agentbot",
            agents_home=root / ".agents",
        )
        self.service = McpService(self.paths)

    def write_catalog(self, *, credential_mode: str = "none") -> None:
        environment = ["DOCS_TOKEN"] if credential_mode == "bearer_env" else []
        entry = {
            "id": "docs",
            "name": "agentbot_docs",
            "publisher": "Example",
            "documentation_url": "https://example.test/docs",
            "license": "MIT",
            "terms_url": "https://example.test/terms",
            "reviewed_on": "2026-09-13",
            "eligible": True,
            "transport": "remote_http",
            "origin": "https://example.test/mcp",
            "clients": {client: {"enabled": True} for client in ("claude", "codex", "cursor")},
            "credential": {"mode": credential_mode, "environment": environment},
            "allowed_tools": ["search"],
            "descriptor_fingerprints": ["sha256:" + "a" * 64],
            "read_only_authority": "public documentation only",
            "limitations": ["publisher behavior can change"],
            "timeout_seconds": 30,
            "max_response_bytes": 1000000,
            "doctor_rules": ["origin_matches"],
        }
        self.paths.mcp_catalog_file.write_text(
            json.dumps({"version": 1, "entries": [entry]}), encoding="utf-8"
        )

    def test_shipped_empty_catalog_and_status_are_read_only(self) -> None:
        before = tuple(self.paths.root.parent.rglob("*"))
        self.assertEqual((), self.service.catalog().entries)
        report = self.service.status()
        self.assertEqual((), report.items)
        self.assertFalse(report.live)
        self.assertEqual(before, tuple(self.paths.root.parent.rglob("*")))

    def test_empty_setup_never_means_all_servers(self) -> None:
        with self.assertRaisesRegex(ValueError, "select at least one MCP server"):
            self.service.setup((), ("claude",), confirmed=True)

    def test_mutations_require_explicit_confirmation(self) -> None:
        for call in (
            lambda: self.service.setup(("github",), ("claude",), confirmed=False),
            lambda: self.service.off(("github",), ("claude",), confirmed=False),
            lambda: self.service.restore("20260913T120000Z-1234abcd", confirmed=False),
        ):
            with self.subTest(call=call):
                with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                    call()

    def test_empty_reports_are_stable_in_plain_and_json_forms(self) -> None:
        for printer, value, expected in (
            (print_mcp_catalog, self.service.catalog(), "No MCP servers are eligible"),
            (print_mcp_status, self.service.status(), "No MCP servers are selected"),
        ):
            with self.subTest(printer=printer.__name__):
                plain = io.StringIO()
                with patch("sys.stdout", plain):
                    printer(value)
                self.assertIn(expected, plain.getvalue())
                machine = io.StringIO()
                with patch("sys.stdout", machine):
                    printer(value, json_output=True)
                self.assertIsInstance(json.loads(machine.getvalue()), dict)

    def test_reports_never_include_environment_secret_values(self) -> None:
        secret = "recognizable-fake-secret-value"
        with patch.dict("os.environ", {"GITHUB_MCP_TOKEN": secret}):
            output = io.StringIO()
            with patch("sys.stdout", output):
                print_mcp_status(self.service.status(), json_output=True)
        self.assertNotIn(secret, output.getvalue())

    def test_cli_catalog_json_uses_the_service_report(self) -> None:
        with patch("src.cli.default_paths", return_value=self.paths):
            rc, stdout, stderr = run_cli_main(["agentbot", "mcp", "catalog", "--json"])
        self.assertEqual(0, rc)
        self.assertEqual({"version": 1, "entries": []}, json.loads(stdout))
        self.assertEqual("", stderr)

    def test_status_reports_unmanaged_collision_and_invalid_config_as_errors(self) -> None:
        self.write_catalog()
        claude = self.paths.claude_home.parent / ".claude.json"
        claude.write_text(
            '{"mcpServers":{"agentbot_docs":{"url":"https://manual.test"}}}',
            encoding="utf-8",
        )
        self.paths.cursor_home.mkdir(parents=True)
        (self.paths.cursor_home / "mcp.json").write_text("[]", encoding="utf-8")

        report = self.service.status()
        states = {(item.client, item.status) for item in report.items}
        self.assertIn(("claude", "unmanaged-conflict"), states)
        self.assertIn(("cursor", "config-invalid"), states)
        self.assertTrue(
            all(
                item.severity == "error"
                for item in report.items
                if item.status in {"unmanaged-conflict", "config-invalid"}
            )
        )

    def test_owned_entry_with_missing_environment_reference_is_reported_without_value(self) -> None:
        self.write_catalog(credential_mode="bearer_env")
        native = {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
        }
        destination = self.paths.claude_home.parent / ".claude.json"
        destination.write_text(
            json.dumps({"mcpServers": {"agentbot_docs": native}}), encoding="utf-8"
        )
        self.service.state_store.replace(
            McpState(
                version=1,
                managed=(
                    McpManagedRecord(
                        catalog_id="docs",
                        client="claude",
                        scope="user",
                        destination=str(destination),
                        name="agentbot_docs",
                        rendered_fingerprint=entry_fingerprint(native),
                        catalog_version=1,
                        reviewed_on="2026-09-13",
                        operation_id="20260913T120000Z-1234abcd",
                        updated_at="2026-09-13T12:00:00Z",
                    ),
                ),
            )
        )

        report = McpService(self.paths, environ={}).status()
        item = next(item for item in report.items if item.client == "claude")
        self.assertEqual("auth-reference-missing", item.status)
        self.assertIn("DOCS_TOKEN", item.detail)


if __name__ == "__main__":
    unittest.main()
