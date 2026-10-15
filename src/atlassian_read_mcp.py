from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from mcp import MCPError

from .atlassian_read_client import MAX_RESPONSE_BYTES, AtlassianReadClient, AtlassianReadError
from .atlassian_token import KEYS, TokenError, resolve
from .paths import default_paths


@dataclass(frozen=True)
class AtlassianToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


def _schema(properties: dict[str, dict[str, object]], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_LIMIT = {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}
_OFFSET = {
    **_LIMIT,
    "start_at": {"type": "integer", "minimum": 0, "maximum": 2_147_483_647, "default": 0},
}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 8192}
_CURSOR = {**_LIMIT, "cursor": _TEXT}
_ISSUE = {
    "issue_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 255,
        "description": "Issue key (DEMO-1) or numeric ID",
    }
}
_PAGE = {
    "page_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 255,
        "description": "Numeric Confluence page ID",
    }
}

ATLASSIAN_TOOL_SPECS = (
    AtlassianToolSpec(
        "jira_search",
        "Search one bounded page of Jira issues using JQL.",
        _schema({"jql": _TEXT, **_LIMIT, "next_page_token": _TEXT}, "jql"),
    ),
    AtlassianToolSpec("jira_issue", "Read a Jira issue.", _schema(_ISSUE, "issue_id")),
    AtlassianToolSpec(
        "jira_comments",
        "Read one bounded page of issue comments.",
        _schema({**_ISSUE, **_OFFSET}, "issue_id"),
    ),
    AtlassianToolSpec(
        "jira_projects", "Read one bounded page of accessible Jira projects.", _schema(_OFFSET)
    ),
    AtlassianToolSpec(
        "jira_boards", "Read one bounded page of accessible Jira boards.", _schema(_OFFSET)
    ),
    AtlassianToolSpec(
        "jira_sprints",
        "Read one bounded page of sprints on a Scrum board.",
        _schema(
            {"board_id": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647}, **_OFFSET},
            "board_id",
        ),
    ),
    AtlassianToolSpec(
        "confluence_search",
        "Search one bounded page of Confluence using CQL.",
        _schema({"cql": _TEXT, **_CURSOR}, "cql"),
    ),
    AtlassianToolSpec(
        "confluence_page",
        "Read a Confluence page and its storage-format body.",
        _schema(_PAGE, "page_id"),
    ),
    AtlassianToolSpec(
        "confluence_spaces", "Read one bounded page of accessible spaces.", _schema(_CURSOR)
    ),
    AtlassianToolSpec(
        "confluence_comments",
        "Read one bounded page of footer or inline comments.",
        _schema(
            {
                **_PAGE,
                **_CURSOR,
                "kind": {"type": "string", "enum": ["footer", "inline"], "default": "footer"},
            },
            "page_id",
        ),
    ),
)


class AtlassianReadMcp:
    def __init__(self, client: AtlassianReadClient) -> None:
        self.client = client
        self._specs = {spec.name: spec for spec in ATLASSIAN_TOOL_SPECS}

    def tools(self) -> tuple[Tool, ...]:
        return tuple(
            Tool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
                annotations=ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=True,
                ),
            )
            for spec in ATLASSIAN_TOOL_SPECS
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        spec = self._specs.get(name)
        if spec is None:
            raise MCPError(-32601, "unknown Atlassian read tool")
        if not isinstance(arguments, dict):
            raise MCPError(-32602, "invalid Atlassian tool arguments")
        properties = spec.input_schema["properties"]
        if set(arguments) - set(properties) or set(spec.input_schema["required"]) - set(arguments):
            raise MCPError(-32602, "invalid Atlassian tool arguments")
        for key, value in arguments.items():
            expected = properties[key]["type"]
            if (expected == "string" and not isinstance(value, str)) or (
                expected == "integer" and (isinstance(value, bool) or not isinstance(value, int))
            ):
                raise MCPError(-32602, "invalid Atlassian tool arguments")
        try:
            payload = getattr(self.client, spec.name)(**arguments)
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            size = len(encoded.encode("utf-8"))
        except AtlassianReadError as error:
            raise MCPError(-32000, str(error)) from None
        except TypeError:
            raise MCPError(-32602, "invalid Atlassian tool arguments") from None
        except Exception:
            raise MCPError(-32603, "Atlassian read tool failed") from None
        if size > MAX_RESPONSE_BYTES:
            raise MCPError(-32004, "Atlassian response limit exceeded")
        return CallToolResult(content=[TextContent(text=encoded)])


def build_atlassian_stdio_server(facade: AtlassianReadMcp) -> Server[None]:
    async def list_tools(
        _context: object, _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(facade.tools()))

    async def call_tool(_context: object, params: CallToolRequestParams) -> CallToolResult:
        return await facade.call_tool(params.name, dict(params.arguments or {}))

    return Server(
        "agentbot-atlassian-read", version="1", on_list_tools=list_tools, on_call_tool=call_tool
    )


async def run_atlassian_stdio(facade: AtlassianReadMcp) -> None:
    server = build_atlassian_stdio_server(facade)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None, *, config_home: Path | None = None) -> int:
    argparse.ArgumentParser(prog="agentbot-atlassian-read").parse_args(argv)
    try:
        settings = resolve(config_home or default_paths().config_home, os.environ)
        client = AtlassianReadClient(*(settings[key] for key in KEYS))
    except (AtlassianReadError, TokenError) as error:
        print(str(error), file=sys.stderr)
        return 1
    asyncio.run(run_atlassian_stdio(AtlassianReadMcp(client)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
