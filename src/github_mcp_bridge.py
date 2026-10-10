"""Local stdio bridge to GitHub's read-only MCP endpoint.

For a client that cannot put an environment credential in a header (Antigravity
sends ``${NAME}`` literally). The bridge inherits ``GITHUB_MCP_TOKEN`` from the
client's environment, sends it to the catalog origin with the same read-only
control headers the other clients use, and serves only the catalog's allowed
tools whose descriptors still match their pinned fingerprints.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping

from mcp.types import CallToolResult, Tool

from .mcp_catalog import load_mcp_catalog
from .mcp_contract import McpFilterContract
from .mcp_filter import FilteredMcpServer, SdkRemoteUpstream, run_stdio_filter
from .mcp_models import McpCatalogEntry
from .paths import AgentbotPaths, default_paths

CATALOG_ID = "github"
TOKEN_ENVIRONMENT = "GITHUB_MCP_TOKEN"


def github_entry(paths: AgentbotPaths) -> McpCatalogEntry:
    catalog = load_mcp_catalog(paths.mcp_catalog_file)
    entry = next((entry for entry in catalog.entries if entry.id == CATALOG_ID), None)
    if entry is None or not entry.eligible:
        raise ValueError("the GitHub MCP catalog entry is missing or not eligible")
    if entry.origin is None or entry.credential.mode != "bearer_env":
        raise ValueError("the GitHub MCP catalog entry is not a bearer-token remote server")
    return entry


def github_filter_contract(entry: McpCatalogEntry) -> McpFilterContract:
    assert entry.origin is not None
    if len(entry.allowed_tools) != len(entry.descriptor_fingerprints):
        raise ValueError("GitHub allowed tools and descriptor fingerprints do not pair up")
    pins = dict(zip(entry.allowed_tools, entry.descriptor_fingerprints, strict=True))
    return McpFilterContract(
        catalog_id=entry.id,
        origin=entry.origin,
        allowed_tools=entry.allowed_tools,
        descriptor_fingerprints=pins,
        timeout_seconds=entry.timeout_seconds,
        max_response_bytes=entry.max_response_bytes,
    )


def github_headers(entry: McpCatalogEntry, environ: Mapping[str, str]) -> dict[str, str]:
    token = environ.get(TOKEN_ENVIRONMENT, "")
    if not token:
        raise ValueError(f"{TOKEN_ENVIRONMENT} is not set in this environment")
    # The read-only controls are the ones every client contract sends; Claude's
    # is the reference copy, and the catalog test keeps the copies identical.
    claude = next(contract for contract in entry.clients if contract.client == "claude")
    return {**dict(claude.static_headers), "Authorization": f"Bearer {token}"}


class GithubBridgeServer(FilteredMcpServer):
    """The filter, adapting GitHub's replies to what the MCP models accept.

    Pins are checked against the upstream descriptors as received, outputSchema
    included. GitHub's schemas use top-level types the MCP Tool model rejects
    (["null", "array"], oneOf), and several tools return a JSON array as
    structuredContent; passed on, either fails the whole reply. The same data
    stays in each result's text content.
    """

    def list_tools(self) -> tuple[Tool, ...]:
        return tuple(
            tool.model_copy(update={"output_schema": None}) for tool in super().list_tools()
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        result = await super().call_tool(name, arguments)
        if result.structured_content is not None and not isinstance(
            result.structured_content, dict
        ):
            return result.model_copy(update={"structured_content": None})
        return result


def main(*, paths: AgentbotPaths | None = None, environ: Mapping[str, str] | None = None) -> int:
    paths = paths or default_paths()
    try:
        entry = github_entry(paths)
        contract = github_filter_contract(entry)
        headers = github_headers(entry, os.environ if environ is None else environ)
    except ValueError as error:
        print(f"  agentbot github bridge: {error}", file=sys.stderr)
        return 1
    upstream = SdkRemoteUpstream(contract, headers=headers)
    asyncio.run(run_stdio_filter(GithubBridgeServer(contract, upstream)))
    return 0
