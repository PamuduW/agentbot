from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    Tool,
)

from mcp import Client, MCPError

from .mcp_contract import McpFilterContract, descriptor_fingerprint, load_filter_contract
from .paths import default_paths


class CrossOriginRedirectError(Exception):
    """An upstream tried to leave its catalog-pinned origin."""


class McpUpstream(Protocol):
    async def initialize(self) -> None: ...

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class FilterAudit:
    method: str
    tool_name: str
    decision: str
    fingerprint: str


class FilteredMcpServer:
    def __init__(
        self,
        contract: McpFilterContract,
        upstream: McpUpstream,
        *,
        audit: Callable[[FilterAudit], None] | None = None,
    ) -> None:
        self.contract = contract
        self.upstream = upstream
        self.audit = audit or (lambda _record: None)
        self._tools: tuple[Tool, ...] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise MCPError(-32002, "filter is closed")
        if self._tools is not None:
            return
        try:
            await self.upstream.initialize()
            discovered = await self._all_tools()
            by_name: dict[str, Tool] = {}
            for discovered_tool in discovered:
                if discovered_tool.name in by_name:
                    raise MCPError(-32603, f"duplicate upstream tool: {discovered_tool.name}")
                by_name[discovered_tool.name] = discovered_tool
            approved = []
            for name in self.contract.allowed_tools:
                approved_tool = by_name.get(name)
                if approved_tool is None:
                    raise MCPError(-32603, f"missing approved tool: {name}")
                actual = descriptor_fingerprint(approved_tool)
                if actual != self.contract.descriptor_fingerprints[name]:
                    raise MCPError(-32603, f"descriptor drift: {name}")
                approved.append(approved_tool)
            self._tools = tuple(approved)
        except BaseException:
            await self.close()
            raise

    def list_tools(self) -> tuple[Tool, ...]:
        if self._tools is None:
            raise MCPError(-32002, "filter is not initialized")
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        if self._tools is None:
            raise MCPError(-32002, "filter is not initialized")
        if name not in self.contract.descriptor_fingerprints:
            self._audit("tools/call", name, "rejected", "none")
            raise MCPError(-32601, f"unknown tool: {name}")
        fingerprint = self.contract.descriptor_fingerprints[name]
        self._audit("tools/call", name, "forwarded", fingerprint)
        try:
            result = await asyncio.wait_for(
                self.upstream.call_tool(name, arguments),
                timeout=self.contract.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise MCPError(-32001, f"approved tool timed out: {name}") from error
        except CrossOriginRedirectError as error:
            raise MCPError(-32003, f"cross-origin redirect blocked: {name}") from error
        except Exception as error:
            raise MCPError(-32603, f"upstream tool call failed: {name}") from error
        encoded = result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        if len(encoded) > self.contract.max_response_bytes:
            raise MCPError(-32004, f"upstream response limit exceeded: {name}")
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.upstream.close()

    async def _all_tools(self) -> tuple[Tool, ...]:
        tools: list[Tool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = await self.upstream.list_tools(cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tuple(tools)
            if cursor in seen_cursors:
                raise MCPError(-32603, "upstream tool pagination repeated a cursor")
            seen_cursors.add(cursor)

    def _audit(self, method: str, name: str, decision: str, fingerprint: str) -> None:
        self.audit(FilterAudit(method, name, decision, fingerprint))


class SdkRemoteUpstream:
    def __init__(self, contract: McpFilterContract) -> None:
        self.client = Client(contract.origin, read_timeout_seconds=contract.timeout_seconds)
        self._entered = False

    async def initialize(self) -> None:
        if not self._entered:
            await self.client.__aenter__()
            self._entered = True

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        return await self.client.list_tools(cursor=cursor, cache_mode="bypass")

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        return await self.client.call_tool(
            name,
            dict(arguments),
            read_timeout_seconds=self.client.read_timeout_seconds,
        )

    async def close(self) -> None:
        if self._entered:
            self._entered = False
            await self.client.__aexit__(None, None, None)


def build_stdio_server(filtered: FilteredMcpServer) -> Server[None]:
    async def list_tools(
        _context: object, _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(filtered.list_tools()))

    async def call_tool(_context: object, params: CallToolRequestParams) -> CallToolResult:
        return await filtered.call_tool(params.name, dict(params.arguments or {}))

    return Server(
        "agentbot-mcp-filter",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio_filter(filtered: FilteredMcpServer) -> None:
    await filtered.start()
    server = build_stdio_server(filtered)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await filtered.close()


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentbot-mcp-filter")
    parser.add_argument("--catalog-id", required=True)
    args = parser.parse_args(argv)
    paths = default_paths(root)
    contract = load_filter_contract(
        paths.root / "mcp" / "contracts" / f"{args.catalog_id}.filter.json",
        args.catalog_id,
    )
    asyncio.run(run_stdio_filter(FilteredMcpServer(contract, SdkRemoteUpstream(contract))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
