from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from src.mcp_catalog import load_mcp_catalog
from src.mcp_render import parse_mcp_config, render_mcp_config

EXPECTED_TOOLS = (
    "get_me",
    "search_repositories",
    "get_file_contents",
    "get_commit",
    "list_commits",
    "list_branches",
    "list_tags",
    "get_tag",
    "list_releases",
    "get_latest_release",
    "get_release_by_tag",
    "list_issues",
    "issue_read",
    "list_pull_requests",
    "pull_request_read",
    "actions_list",
    "actions_get",
    "get_job_logs",
)


class GitHubMcpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.contract_path = cls.root / "mcp" / "contracts" / "github.json"
        cls.catalog_path = cls.root / "mcp" / "catalog.json"

    def load_contract(self) -> dict[str, object]:
        return json.loads(self.contract_path.read_text(encoding="utf-8"))

    def catalog_entry(self):
        catalog = load_mcp_catalog(self.catalog_path)
        return next(entry for entry in catalog.entries if entry.id == "github")

    def test_contract_freezes_exact_read_only_remote_boundary(self) -> None:
        contract = self.load_contract()
        self.assertEqual(1, contract["version"])
        self.assertEqual("https://api.githubcopilot.com/mcp/readonly", contract["endpoint"])
        self.assertEqual("true", contract["headers"]["X-MCP-Readonly"])
        self.assertEqual(",".join(EXPECTED_TOOLS), contract["headers"]["X-MCP-Tools"])
        self.assertNotIn("X-MCP-Toolsets", contract["headers"])
        self.assertNotIn("*", contract["headers"]["X-MCP-Tools"])

    def test_contract_has_no_write_permission_or_trigger(self) -> None:
        contract = self.load_contract()
        tools = tuple(tool["name"] for tool in contract["tools"])
        self.assertEqual(EXPECTED_TOOLS, tools)
        self.assertNotIn("actions_run_trigger", tools)
        self.assertIn("actions_run_trigger", contract["prohibited_tools"])
        self.assertTrue(all(level == "read" for level in contract["permissions"].values()))

    def test_every_tool_has_a_unique_descriptor_fingerprint(self) -> None:
        contract = self.load_contract()
        fingerprints = tuple(tool["descriptor_fingerprint"] for tool in contract["tools"])
        self.assertEqual(len(EXPECTED_TOOLS), len(fingerprints))
        self.assertEqual(len(fingerprints), len(set(fingerprints)))
        for fingerprint in fingerprints:
            self.assertRegex(fingerprint, r"^sha256:[0-9a-f]{64}$")

    def test_catalog_entry_matches_contract_but_is_not_yet_eligible(self) -> None:
        contract = self.load_contract()
        entry = self.catalog_entry()
        self.assertFalse(entry.eligible)
        self.assertEqual("agentbot_github", entry.name)
        self.assertEqual(contract["endpoint"], entry.origin)
        self.assertEqual(EXPECTED_TOOLS, entry.allowed_tools)
        self.assertEqual(
            tuple(tool["descriptor_fingerprint"] for tool in contract["tools"]),
            entry.descriptor_fingerprints,
        )
        self.assertEqual(("GITHUB_MCP_TOKEN",), entry.credential.environment)

    def test_native_rendering_keeps_both_exact_boundaries(self) -> None:
        entry = replace(self.catalog_entry(), eligible=True)
        expected_tools = ",".join(EXPECTED_TOOLS)
        for client, empty in (("claude", "{}"), ("codex", ""), ("cursor", "{}")):
            rendered = render_mcp_config(client, empty, (entry,))
            native = parse_mcp_config(client, rendered)
            server = native["mcp_servers"][entry.name] if client == "codex" else native["mcpServers"][entry.name]
            headers = server["http_headers"] if client == "codex" else server["headers"]
            self.assertEqual("true", headers["X-MCP-Readonly"])
            self.assertEqual(expected_tools, headers["X-MCP-Tools"])
            if client == "codex":
                self.assertEqual(list(EXPECTED_TOOLS), server["enabled_tools"])


if __name__ == "__main__":
    unittest.main()
