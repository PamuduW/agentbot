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


class McpInstallComponentTests(unittest.TestCase):
    """MCP as an install component, driven by the catalog the way skills are
    driven by skills.sources.yaml.

    Eligibility is the review. An entry marked eligible has already been
    vetted, so a selected install registers every one of them rather than
    asking which -- and an ineligible entry stays out no matter what the
    component does, because that is the admission gate, not a preference.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        product = self.root / "agentbot"
        (product / "mcp").mkdir(parents=True)
        self.catalog_file = product / "mcp" / "catalog.json"
        self.paths = AgentbotPaths(
            root=product,
            codex_home=self.root / ".codex",
            claude_home=self.root / ".claude",
            cursor_home=self.root / ".cursor",
            config_home=self.root / ".config" / "agentbot",
            agents_home=self.root / ".agents",
        )
        for home in (self.paths.codex_home, self.paths.claude_home, self.paths.cursor_home):
            home.mkdir(parents=True, exist_ok=True)
        # The shipped catalog, so these tests move with the servers the product
        # actually vets rather than a fixture that can drift away from it.
        shipped = Path("mcp/catalog.json").read_text(encoding="utf-8")
        self.catalog_file.write_text(shipped, encoding="utf-8")
        self.service = McpService(self.paths)

    def test_selection_is_every_eligible_entry_and_its_clients(self) -> None:
        selection, targets = self.service.eligible_selection()
        catalog = self.service.catalog()
        eligible = {entry.id for entry in catalog.entries if entry.eligible}
        ineligible = {entry.id for entry in catalog.entries if not entry.eligible}

        self.assertEqual(eligible, set(selection))
        self.assertEqual(("claude", "codex", "cursor"), targets)
        # The admission gate still holds even though the shipped catalog now
        # admits every entry: an ineligible one is unreachable through the
        # component, which _selected_entries enforces by raising.
        self.assertFalse(ineligible & set(selection))
        with self.assertRaises(ValueError):
            self.service.plan(("not_a_catalog_id",), targets)

    def test_install_registers_every_reviewed_pair(self) -> None:
        selection, targets = self.service.eligible_selection()
        outcome = self.service.install_eligible(apply=True)

        self.assertTrue(outcome.applied)
        self.assertEqual(len(selection) * len(targets), len(outcome.admitted))
        self.assertEqual((), outcome.blocked)
        self.assertEqual("installed", outcome.result)
        managed = self.service.state_store.load().managed
        self.assertEqual(len(outcome.admitted), len(managed))

    def test_a_second_install_changes_nothing(self) -> None:
        """Install runs on every bootstrap. A component that rewrote three
        client configs each time would churn a backup per run to change
        nothing."""
        self.service.install_eligible(apply=True)
        before = self.paths.mcp_state_file.read_bytes()

        again = self.service.install_eligible(apply=True)

        self.assertFalse(again.applied)
        self.assertEqual((), again.admitted)
        self.assertTrue(again.current)
        self.assertEqual("ok", again.result)
        self.assertEqual(before, self.paths.mcp_state_file.read_bytes())

    def test_a_deselected_component_reads_without_writing(self) -> None:
        """It still reports what it would have registered.

        `admitted` is the plan's reading -- what is absent -- and `applied` is
        what says whether it was written. Blanking the first when not applying
        erased the number the update preview needs.
        """
        outcome = self.service.install_eligible(apply=False)

        self.assertFalse(outcome.applied)
        self.assertTrue(outcome.admitted)
        self.assertEqual("skipped", outcome.result)
        self.assertFalse(self.paths.mcp_state_file.exists())

    def test_an_entry_another_tool_owns_blocks_instead_of_being_overwritten(self) -> None:
        """The one thing this component must never do.

        A name already present that Agentbot does not own is somebody's
        configuration. Registering over it would destroy it, so the run reports
        the pair and touches nothing.
        """
        cursor_config = self.paths.cursor_home / "mcp.json"
        cursor_config.write_text(
            json.dumps({"mcpServers": {"agentbot_context7": {"url": "https://example.invalid"}}}),
            encoding="utf-8",
        )

        outcome = self.service.install_eligible(apply=True)

        self.assertFalse(outcome.applied)
        self.assertIn(("context7", "cursor", "unmanaged-conflict"), outcome.blocked)
        self.assertEqual("check", outcome.result)
        # Untouched, byte for byte.
        self.assertEqual(
            {"mcpServers": {"agentbot_context7": {"url": "https://example.invalid"}}},
            json.loads(cursor_config.read_text(encoding="utf-8")),
        )

    def test_the_update_preview_names_what_the_apply_will_register(self) -> None:
        """The preview is the screen the operator confirms from.

        The apply registers MCP servers, so a report that did not mention them
        would have a newly reviewed server arrive without the screen before it
        saying so.
        """
        from src.ui.reports import _mcp_plan_row

        selection, targets = self.service.eligible_selection()
        total = len(selection) * len(targets)

        pending = self.service.install_eligible(apply=False)
        self.assertEqual(
            ("MCP servers", "0 registered", f"{total} to register", "register"),
            _mcp_plan_row(pending),
        )

        self.service.install_eligible(apply=True)
        settled = self.service.install_eligible(apply=False)
        self.assertEqual(
            ("MCP servers", f"{total} registered", "none", "up to date"),
            _mcp_plan_row(settled),
        )

    def test_the_update_preview_registers_nothing(self) -> None:
        from src.ui.reports import _mcp_plan_row

        _mcp_plan_row(self.service.install_eligible(apply=False))

        self.assertFalse(self.paths.mcp_state_file.exists())

    def test_an_empty_catalog_is_not_an_error(self) -> None:
        self.catalog_file.write_text('{"version":1,"entries":[]}\n', encoding="utf-8")
        outcome = McpService(self.paths).install_eligible(apply=True)

        self.assertEqual((), outcome.admitted)
        self.assertEqual((), outcome.blocked)
        self.assertEqual("ok", outcome.result)
