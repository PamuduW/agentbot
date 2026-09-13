from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.mcp_catalog import load_mcp_catalog
from src.paths import AgentbotPaths


class McpCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = AgentbotPaths(
            root=Path(__file__).resolve().parents[1],
            codex_home=root / ".codex",
            claude_home=root / ".claude",
            cursor_home=root / ".cursor",
            config_home=root / ".config" / "agentbot",
            agents_home=root / ".agents",
        )
        self.catalog_path = root / "catalog.json"

    def write_catalog(self, value: object) -> None:
        self.catalog_path.write_text(json.dumps(value), encoding="utf-8")

    def valid_entry(self, **overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
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
            "credential": {"mode": "none", "environment": []},
            "allowed_tools": ["search"],
            "descriptor_fingerprints": ["sha256:" + "a" * 64],
            "read_only_authority": "public documentation only",
            "limitations": ["publisher behavior can change"],
            "timeout_seconds": 30,
            "max_response_bytes": 1000000,
            "doctor_rules": ["origin_matches"],
        }
        entry.update(overrides)
        return entry

    def test_shipped_catalog_contains_no_eligible_entry(self) -> None:
        catalog = load_mcp_catalog(self.paths.mcp_catalog_file)
        self.assertEqual(1, catalog.version)
        self.assertFalse(any(entry.eligible for entry in catalog.entries))

    def test_static_headers_reject_credential_values(self) -> None:
        entry = self.valid_entry()
        entry["clients"] = {
            client: {
                "enabled": True,
                "static_headers": {"Private-Token": "literal-secret"},
            }
            for client in ("claude", "codex", "cursor")
        }
        self.write_catalog({"version": 1, "entries": [entry]})
        with self.assertRaisesRegex(ValueError, "unsupported public control header"):
            load_mcp_catalog(self.catalog_path)

    def test_unknown_top_level_key_fails_closed(self) -> None:
        self.write_catalog({"version": 1, "entries": [], "mystery": True})
        with self.assertRaisesRegex(ValueError, "unknown catalog key: mystery"):
            load_mcp_catalog(self.catalog_path)

    def test_unknown_entry_key_fails_closed(self) -> None:
        self.write_catalog({"version": 1, "entries": [{"id": "x", "mystery": True}]})
        with self.assertRaisesRegex(ValueError, "unknown catalog entry key: mystery"):
            load_mcp_catalog(self.catalog_path)

    def test_unsupported_version_fails_closed(self) -> None:
        self.write_catalog({"version": 2, "entries": []})
        with self.assertRaisesRegex(ValueError, "unsupported catalog version: 2"):
            load_mcp_catalog(self.catalog_path)

    def test_boolean_version_is_not_an_integer(self) -> None:
        self.write_catalog({"version": True, "entries": []})
        with self.assertRaisesRegex(ValueError, "catalog version must be an integer"):
            load_mcp_catalog(self.catalog_path)

    def test_duplicate_ids_fail_closed(self) -> None:
        entry = self.valid_entry()
        self.write_catalog({"version": 1, "entries": [entry, entry]})
        with self.assertRaisesRegex(ValueError, "duplicate catalog id: docs"):
            load_mcp_catalog(self.catalog_path)

    def test_duplicate_rendered_names_fail_closed(self) -> None:
        first = self.valid_entry()
        second = self.valid_entry(id="other")
        self.write_catalog({"version": 1, "entries": [first, second]})
        with self.assertRaisesRegex(ValueError, "duplicate rendered name: agentbot_docs"):
            load_mcp_catalog(self.catalog_path)

    def test_non_https_remote_origin_fails_closed(self) -> None:
        self.write_catalog(
            {
                "version": 1,
                "entries": [self.valid_entry(origin="http://example.test/mcp")],
            }
        )
        with self.assertRaisesRegex(ValueError, "remote origin must use https"):
            load_mcp_catalog(self.catalog_path)

    def test_literal_secret_field_fails_closed(self) -> None:
        entry = self.valid_entry()
        entry["credential"] = {
            "mode": "bearer_env",
            "environment": ["EXAMPLE_TOKEN"],
            "token": "literal-secret",
        }
        self.write_catalog({"version": 1, "entries": [entry]})
        with self.assertRaisesRegex(ValueError, "unknown credential key: token"):
            load_mcp_catalog(self.catalog_path)

    def test_eligible_entry_requires_all_client_contracts(self) -> None:
        entry = self.valid_entry()
        entry["clients"] = {"claude": {"enabled": True}}
        self.write_catalog({"version": 1, "entries": [entry]})
        with self.assertRaisesRegex(
            ValueError, "eligible catalog entry docs must define claude, codex, and cursor"
        ):
            load_mcp_catalog(self.catalog_path)

    def test_unknown_enum_fails_closed(self) -> None:
        self.write_catalog({"version": 1, "entries": [self.valid_entry(transport="magic")]})
        with self.assertRaisesRegex(ValueError, "unknown transport: magic"):
            load_mcp_catalog(self.catalog_path)

    def test_blank_string_fails_closed(self) -> None:
        self.write_catalog({"version": 1, "entries": [self.valid_entry(publisher=" ")]})
        with self.assertRaisesRegex(ValueError, "publisher must not be blank"):
            load_mcp_catalog(self.catalog_path)

    def test_remote_transport_rejects_local_entrypoint(self) -> None:
        self.write_catalog(
            {
                "version": 1,
                "entries": [self.valid_entry(entrypoint=["python3", "-m", "server"])],
            }
        )
        with self.assertRaisesRegex(
            ValueError, "remote_http catalog entry must not define entrypoint"
        ):
            load_mcp_catalog(self.catalog_path)

    def test_mcp_paths_stay_within_injected_roots(self) -> None:
        self.assertEqual(self.paths.root / "mcp" / "catalog.json", self.paths.mcp_catalog_file)
        self.assertEqual(self.paths.config_home / "mcp.json", self.paths.mcp_state_file)
        self.assertEqual(self.paths.config_home / "backups" / "mcp", self.paths.mcp_backup_home)


if __name__ == "__main__":
    unittest.main()
