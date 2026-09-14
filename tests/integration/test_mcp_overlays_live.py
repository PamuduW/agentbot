from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from mcp import Client, MCPError
from src.mcp_contract import descriptor_fingerprint, load_filter_contract
from src.mcp_filter import FilteredMcpServer, SdkRemoteUpstream

SAFE_CALLS: dict[str, tuple[str, dict[str, object]]] = {
    "context7": (
        "resolve-library-id",
        {"libraryName": "Python", "query": "asyncio TaskGroup"},
    ),
    "aws_knowledge": ("aws___list_regions", {}),
    "microsoft_learn": (
        "microsoft_docs_search",
        {"query": "Azure Functions Python quickstart"},
    ),
}


class McpOverlaysLiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("AGENTBOT_TEST_MCP_OVERLAYS") != "1":
            raise unittest.SkipTest(
                "set AGENTBOT_TEST_MCP_OVERLAYS=1 for the public overlay gate"
            )
        cls.root = Path(__file__).resolve().parents[2]

    async def test_public_tool_lists_and_safe_calls_match_frozen_contracts(self) -> None:
        for candidate, (tool_name, arguments) in SAFE_CALLS.items():
            with self.subTest(candidate=candidate):
                contract = load_filter_contract(
                    self.root / "mcp" / "contracts" / f"{candidate}.json",
                    candidate,
                )
                async with Client(contract.origin, read_timeout_seconds=60) as client:
                    page = await client.list_tools(cache_mode="bypass")
                self.assertIsNone(page.next_cursor)
                self.assertEqual(set(contract.allowed_tools), {tool.name for tool in page.tools})
                self.assertEqual(
                    contract.descriptor_fingerprints,
                    {tool.name: descriptor_fingerprint(tool) for tool in page.tools},
                )

                filtered = FilteredMcpServer(contract, SdkRemoteUpstream(contract))
                try:
                    await filtered.start()
                    self.assertEqual(contract.allowed_tools, tuple(t.name for t in filtered.list_tools()))
                    result = await filtered.call_tool(tool_name, arguments)
                    self.assertFalse(result.is_error)
                    self.assertLessEqual(
                        len(result.model_dump_json(by_alias=True, exclude_none=True).encode()),
                        contract.max_response_bytes,
                    )
                    with self.assertRaisesRegex(MCPError, "unknown tool"):
                        await filtered.call_tool("agentbot_hidden_tool", {})
                finally:
                    await filtered.close()

    def test_contract_files_contain_no_live_response_content(self) -> None:
        for candidate in SAFE_CALLS:
            with self.subTest(candidate=candidate):
                raw = json.loads(
                    (self.root / "mcp" / "contracts" / f"{candidate}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    {"name", "descriptor_fingerprint"},
                    set(raw["filter"]["tools"][0]),
                )


if __name__ == "__main__":
    unittest.main()
