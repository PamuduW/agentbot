from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable

from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from mcp import Client, MCPError
from src.cli import _requires_raw_stdout
from src.mcp_contract import McpFilterContract, descriptor_fingerprint
from src.mcp_filter import (
    CrossOriginRedirectError,
    FilterAudit,
    FilteredMcpServer,
    build_stdio_server,
)


def tool(name: str, *, description: str | None = None) -> Tool:
    return Tool(
        name=name,
        description=description or f"Read {name}",
        inputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(readOnlyHint=True),
    )


class FakeUpstream:
    def __init__(
        self,
        pages: dict[str | None, ListToolsResult],
        *,
        call: Callable[[str, dict[str, object]], object] | None = None,
    ) -> None:
        self.pages = pages
        self.call = call
        self.initialize_count = 0
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def initialize(self) -> None:
        self.initialize_count += 1

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        return self.pages[cursor]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        self.calls.append((name, arguments))
        if self.call is not None:
            result = self.call(name, arguments)
            if asyncio.iscoroutine(result):
                return await result
            if isinstance(result, Exception):
                raise result
            if isinstance(result, CallToolResult):
                return result
        return CallToolResult(content=[TextContent(text="ok")])

    async def close(self) -> None:
        self.closed = True


class McpFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_internal_stdio_server_bypasses_human_output_wrapping(self) -> None:
        self.assertTrue(_requires_raw_stdout(("--root", "/opt/agentbot", "mcp", "serve")))
        self.assertFalse(_requires_raw_stdout(("mcp", "status")))

    def contract(self, *tools: Tool, **overrides: object) -> McpFilterContract:
        values: dict[str, object] = {
            "catalog_id": "docs",
            "origin": "https://docs.example.test/mcp",
            "allowed_tools": tuple(item.name for item in tools),
            "descriptor_fingerprints": {item.name: descriptor_fingerprint(item) for item in tools},
            "timeout_seconds": 1.0,
            "max_response_bytes": 1024,
        }
        values.update(overrides)
        return McpFilterContract(**values)

    async def test_initialization_is_independent_and_consumes_every_page(self) -> None:
        first, second, hidden = tool("first"), tool("second"), tool("write_anything")
        upstream = FakeUpstream(
            {
                None: ListToolsResult(tools=[second, hidden], nextCursor="page-2"),
                "page-2": ListToolsResult(tools=[first]),
            }
        )
        filtered = FilteredMcpServer(self.contract(first, second), upstream)
        self.assertEqual(0, upstream.initialize_count)
        await filtered.start()
        self.assertEqual(1, upstream.initialize_count)
        self.assertEqual(["first", "second"], [item.name for item in filtered.list_tools()])

    async def test_hidden_tool_cannot_be_called_directly(self) -> None:
        allowed, hidden = tool("read_docs"), tool("write_anything")
        upstream = FakeUpstream({None: ListToolsResult(tools=[allowed, hidden])})
        filtered = FilteredMcpServer(self.contract(allowed), upstream)
        await filtered.start()
        with self.assertRaisesRegex(MCPError, "unknown tool"):
            await filtered.call_tool("write_anything", {})
        self.assertEqual([], upstream.calls)

    async def test_missing_tool_and_descriptor_drift_fail_closed(self) -> None:
        approved = tool("read_docs")
        for present, message in (
            ([], "missing approved tool"),
            ([tool("read_docs", description="changed")], "descriptor drift"),
        ):
            with self.subTest(message=message):
                upstream = FakeUpstream({None: ListToolsResult(tools=present)})
                filtered = FilteredMcpServer(self.contract(approved), upstream)
                with self.assertRaisesRegex(MCPError, message):
                    await filtered.start()

    async def test_cross_origin_redirect_is_sanitized(self) -> None:
        approved = tool("read_docs")
        upstream = FakeUpstream(
            {None: ListToolsResult(tools=[approved])},
            call=lambda _name, _args: CrossOriginRedirectError("https://evil.test/secret"),
        )
        filtered = FilteredMcpServer(self.contract(approved), upstream)
        await filtered.start()
        with self.assertRaisesRegex(MCPError, "cross-origin redirect blocked") as caught:
            await filtered.call_tool("read_docs", {})
        self.assertNotIn("evil.test", str(caught.exception))

    async def test_timeout_and_response_cap_fail_closed(self) -> None:
        approved = tool("read_docs")

        async def slow(_name: str, _args: dict[str, object]) -> CallToolResult:
            await asyncio.sleep(1)
            return CallToolResult(content=[TextContent(text="late")])

        for call, overrides, message in (
            (slow, {"timeout_seconds": 0.01}, "timed out"),
            (
                lambda _name, _args: CallToolResult(content=[TextContent(text="x" * 2048)]),
                {"max_response_bytes": 128},
                "response limit",
            ),
        ):
            with self.subTest(message=message):
                upstream = FakeUpstream({None: ListToolsResult(tools=[approved])}, call=call)
                filtered = FilteredMcpServer(self.contract(approved, **overrides), upstream)
                await filtered.start()
                with self.assertRaisesRegex(MCPError, message):
                    await filtered.call_tool("read_docs", {})

    async def test_cancellation_reaches_the_upstream_call(self) -> None:
        approved = tool("read_docs")
        cancelled = asyncio.Event()

        async def blocked(_name: str, _args: dict[str, object]) -> CallToolResult:
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        upstream = FakeUpstream({None: ListToolsResult(tools=[approved])}, call=blocked)
        filtered = FilteredMcpServer(self.contract(approved), upstream)
        await filtered.start()
        task = asyncio.create_task(filtered.call_tool("read_docs", {}))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_audit_records_never_include_arguments_results_or_errors(self) -> None:
        approved = tool("read_docs")
        audits: list[FilterAudit] = []
        secret = "filter-canary-secret"
        upstream = FakeUpstream(
            {None: ListToolsResult(tools=[approved])},
            call=lambda _name, _args: RuntimeError(secret),
        )
        filtered = FilteredMcpServer(self.contract(approved), upstream, audit=audits.append)
        await filtered.start()
        with self.assertRaisesRegex(MCPError, "upstream tool call failed") as caught:
            await filtered.call_tool("read_docs", {"query": secret})
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, repr(audits))

    async def test_close_is_idempotent_and_closes_upstream(self) -> None:
        approved = tool("read_docs")
        upstream = FakeUpstream({None: ListToolsResult(tools=[approved])})
        filtered = FilteredMcpServer(self.contract(approved), upstream)
        await filtered.start()
        await filtered.close()
        await filtered.close()
        self.assertTrue(upstream.closed)

    async def test_stdio_adapter_advertises_tools_only(self) -> None:
        approved = tool("read_docs")
        upstream = FakeUpstream({None: ListToolsResult(tools=[approved])})
        filtered = FilteredMcpServer(self.contract(approved), upstream)
        await filtered.start()
        server = build_stdio_server(filtered)
        capabilities = server.get_capabilities()
        self.assertIsNotNone(capabilities.tools)
        self.assertIsNone(capabilities.prompts)
        self.assertIsNone(capabilities.resources)
        self.assertIsNone(capabilities.logging)

    async def test_sdk_protocol_round_trip_lists_and_calls_only_approved_tools(self) -> None:
        approved, hidden = tool("read_docs"), tool("write_anything")
        upstream = FakeUpstream({None: ListToolsResult(tools=[hidden, approved])})
        filtered = FilteredMcpServer(self.contract(approved), upstream)
        await filtered.start()
        server = build_stdio_server(filtered)
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            self.assertEqual(["read_docs"], [item.name for item in listed.tools])
            result = await client.call_tool("read_docs", {"query": "public"})
            self.assertFalse(result.is_error)
            with self.assertRaisesRegex(MCPError, "unknown tool"):
                await client.call_tool("write_anything", {})
        await filtered.close()
        self.assertTrue(upstream.closed)


if __name__ == "__main__":
    unittest.main()
