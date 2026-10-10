from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from src.github_mcp_bridge import (
    GithubBridgeServer,
    github_entry,
    github_filter_contract,
    github_headers,
    main,
)
from src.mcp_catalog import load_mcp_catalog
from src.mcp_contract import McpFilterContract, descriptor_fingerprint
from src.mcp_filter import SdkRemoteUpstream
from src.paths import AgentbotPaths
from tests.test_mcp_filter import FakeUpstream

ROOT = Path(__file__).resolve().parents[1]


def _paths(home: Path) -> AgentbotPaths:
    return AgentbotPaths(
        root=ROOT,
        codex_home=home / ".codex",
        claude_home=home / ".claude",
        cursor_home=home / ".cursor",
        config_home=home / ".config" / "agentbot",
        agents_home=home / ".agents",
    )


class GithubBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = _paths(Path(self.temporary.name))
        self.entry = github_entry(self.paths)

    def test_contract_is_the_catalog_entry_and_its_pins(self) -> None:
        contract = github_filter_contract(self.entry)
        self.assertEqual("https://api.githubcopilot.com/mcp/readonly", contract.origin)
        self.assertEqual(self.entry.allowed_tools, contract.allowed_tools)
        self.assertEqual(
            self.entry.descriptor_fingerprints,
            tuple(contract.descriptor_fingerprints[name] for name in contract.allowed_tools),
        )

    def test_headers_carry_the_token_and_the_read_only_controls(self) -> None:
        headers = github_headers(self.entry, {"GITHUB_MCP_TOKEN": "tok"})
        self.assertEqual("Bearer tok", headers["Authorization"])
        self.assertEqual("true", headers["X-MCP-Readonly"])
        self.assertEqual(",".join(self.entry.allowed_tools), headers["X-MCP-Tools"])

    def test_every_client_sends_the_same_read_only_controls(self) -> None:
        catalog = load_mcp_catalog(self.paths.mcp_catalog_file)
        entry = next(entry for entry in catalog.entries if entry.id == "github")
        controls = {
            contract.client: contract.static_headers
            for contract in entry.clients
            if contract.client != "antigravity"
        }
        self.assertEqual(1, len(set(controls.values())), controls)

    def test_missing_token_fails_closed_without_connecting(self) -> None:
        with self.assertRaisesRegex(ValueError, "GITHUB_MCP_TOKEN is not set"):
            github_headers(self.entry, {})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(1, main(paths=self.paths, environ={}))
        self.assertIn("GITHUB_MCP_TOKEN", stderr.getvalue())

    async def test_upstream_with_headers_owns_and_closes_its_http_client(self) -> None:
        contract = github_filter_contract(self.entry)
        upstream = SdkRemoteUpstream(contract, headers={"Authorization": "Bearer tok"})
        http_client = upstream._http_client
        assert http_client is not None
        self.assertEqual("Bearer tok", http_client.headers["Authorization"])
        await upstream.close()
        self.assertTrue(http_client.is_closed)
        self.assertIsNone(SdkRemoteUpstream(contract)._http_client)

    async def test_bridge_serves_pinned_tools_in_a_shape_the_models_accept(self) -> None:
        upstream_tool = Tool(
            name="list_branches",
            description="List branches",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={"type": "object", "properties": {"x": {"type": "string"}}},
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        contract = McpFilterContract(
            catalog_id="github",
            origin="https://api.githubcopilot.com/mcp/readonly",
            allowed_tools=("list_branches",),
            descriptor_fingerprints={"list_branches": descriptor_fingerprint(upstream_tool)},
            timeout_seconds=1.0,
            max_response_bytes=4096,
        )
        upstream = FakeUpstream(
            {None: ListToolsResult(tools=[upstream_tool])},
            call=lambda _name, _args: CallToolResult.model_construct(
                content=[TextContent(text='[{"name": "main"}]')],
                structured_content=[{"name": "main"}],
            ),
        )
        server = GithubBridgeServer(contract, upstream)
        await server.start()

        (served,) = server.list_tools()
        self.assertIsNone(served.output_schema)
        result = await server.call_tool("list_branches", {})
        self.assertIsNone(result.structured_content)
        self.assertEqual('[{"name": "main"}]', result.content[0].text)

    async def test_bridge_keeps_object_structured_content(self) -> None:
        upstream_tool = Tool(
            name="get_me",
            description="Me",
            inputSchema={"type": "object", "properties": {}},
            annotations=ToolAnnotations(readOnlyHint=True),
        )
        contract = McpFilterContract(
            catalog_id="github",
            origin="https://api.githubcopilot.com/mcp/readonly",
            allowed_tools=("get_me",),
            descriptor_fingerprints={"get_me": descriptor_fingerprint(upstream_tool)},
            timeout_seconds=1.0,
            max_response_bytes=4096,
        )
        upstream = FakeUpstream(
            {None: ListToolsResult(tools=[upstream_tool])},
            call=lambda _name, _args: CallToolResult(
                content=[TextContent(text="{}")], structuredContent={"login": "x"}
            ),
        )
        server = GithubBridgeServer(contract, upstream)
        await server.start()
        result = await server.call_tool("get_me", {})
        self.assertEqual({"login": "x"}, result.structured_content)


if __name__ == "__main__":
    unittest.main()
