from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

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

from .gitlab_read_client import GitLabReadClient, GitLabReadError


class GitLabClient(Protocol):
    def project(self, **arguments: object) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GitLabToolSpec:
    name: str
    method: str
    description: str
    input_schema: dict[str, Any]


def _schema(properties: dict[str, dict[str, object]], *required: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


PROJECT: dict[str, dict[str, object]] = {
    "project_id": {
        "type": ["string", "integer"],
        "description": "Project ID or full path",
    }
}
PAGE: dict[str, dict[str, object]] = {
    "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    "max_pages": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
}


GITLAB_TOOL_SPECS = (
    GitLabToolSpec(
        "gitlab_project", "project", "Read project metadata.", _schema(PROJECT, "project_id")
    ),
    GitLabToolSpec(
        "gitlab_repository_tree",
        "repository_tree",
        "List a bounded repository tree.",
        _schema(
            {**PROJECT, "path": {"type": "string"}, "ref": {"type": "string"}, **PAGE}, "project_id"
        ),
    ),
    GitLabToolSpec(
        "gitlab_repository_file",
        "repository_file",
        "Read bounded repository file content.",
        _schema(
            {
                **PROJECT,
                "file_path": {"type": "string"},
                "ref": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 2_097_152},
            },
            "project_id",
            "file_path",
            "ref",
        ),
    ),
    GitLabToolSpec(
        "gitlab_commits",
        "commits",
        "List commits or read one commit and its bounded diff.",
        _schema(
            {
                **PROJECT,
                "sha": {"type": "string"},
                "include_diff": {"type": "boolean", "default": False},
                **PAGE,
            },
            "project_id",
        ),
    ),
    GitLabToolSpec(
        "gitlab_refs",
        "refs",
        "List or read branches and tags.",
        _schema(
            {
                **PROJECT,
                "kind": {"type": "string", "enum": ["branches", "tags"]},
                "ref": {"type": "string"},
                **PAGE,
            },
            "project_id",
            "kind",
        ),
    ),
    GitLabToolSpec(
        "gitlab_issues",
        "issues",
        "List issues or read one issue.",
        _schema({**PROJECT, "iid": {"type": "integer", "minimum": 1}, **PAGE}, "project_id"),
    ),
    GitLabToolSpec(
        "gitlab_merge_requests",
        "merge_requests",
        "List merge requests or read one merge request.",
        _schema({**PROJECT, "iid": {"type": "integer", "minimum": 1}, **PAGE}, "project_id"),
    ),
    GitLabToolSpec(
        "gitlab_merge_request_diff",
        "merge_request_diff",
        "Read a bounded merge-request diff.",
        _schema(
            {
                **PROJECT,
                "iid": {"type": "integer", "minimum": 1},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 4_194_304},
            },
            "project_id",
            "iid",
        ),
    ),
    GitLabToolSpec(
        "gitlab_notes",
        "notes",
        "List or read issue and merge-request notes.",
        _schema(
            {
                **PROJECT,
                "kind": {"type": "string", "enum": ["issues", "merge_requests"]},
                "iid": {"type": "integer", "minimum": 1},
                "note_id": {"type": "integer", "minimum": 1},
                **PAGE,
            },
            "project_id",
            "kind",
            "iid",
        ),
    ),
    GitLabToolSpec(
        "gitlab_members",
        "members",
        "List direct or inherited project members.",
        _schema(
            {**PROJECT, "include_inherited": {"type": "boolean"}, **PAGE},
            "project_id",
        ),
    ),
    GitLabToolSpec(
        "gitlab_releases",
        "releases",
        "List releases or read one release.",
        _schema({**PROJECT, "tag_name": {"type": "string"}, **PAGE}, "project_id"),
    ),
    GitLabToolSpec(
        "gitlab_pipelines",
        "pipelines",
        "List pipelines or read one pipeline without variables.",
        _schema(
            {**PROJECT, "pipeline_id": {"type": "integer", "minimum": 1}, **PAGE}, "project_id"
        ),
    ),
    GitLabToolSpec(
        "gitlab_jobs",
        "jobs",
        "List jobs or read one job without actions.",
        _schema(
            {
                **PROJECT,
                "pipeline_id": {"type": "integer", "minimum": 1},
                "job_id": {"type": "integer", "minimum": 1},
                **PAGE,
            },
            "project_id",
        ),
    ),
    GitLabToolSpec(
        "gitlab_job_log",
        "job_log",
        "Read the final 2,000 lines of a bounded job trace.",
        _schema({**PROJECT, "job_id": {"type": "integer", "minimum": 1}}, "project_id", "job_id"),
    ),
    GitLabToolSpec(
        "gitlab_search",
        "search",
        "Search a project within a fixed read-only scope.",
        _schema(
            {
                **PROJECT,
                "scope": {
                    "type": "string",
                    "enum": ["blobs", "commits", "issues", "merge_requests", "notes"],
                },
                "search": {"type": "string", "minLength": 1, "maxLength": 256},
                **PAGE,
            },
            "project_id",
            "scope",
            "search",
        ),
    ),
)


class GitLabReadMcp:
    def __init__(self, client: object) -> None:
        self.client = client
        self._specs = {spec.name: spec for spec in GITLAB_TOOL_SPECS}

    def tool_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in GITLAB_TOOL_SPECS)

    def tools(self) -> tuple[Tool, ...]:
        annotations = ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        )
        return tuple(
            Tool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
                annotations=annotations,
            )
            for spec in GITLAB_TOOL_SPECS
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        spec = self._specs.get(name)
        if spec is None:
            raise MCPError(-32601, f"unknown tool: {name}")
        self._validate_arguments(spec, arguments)
        method = getattr(self.client, spec.method)
        try:
            payload = method(**arguments)
        except GitLabReadError as error:
            raise MCPError(-32000, str(error)) from error
        except TypeError as error:
            raise MCPError(-32602, f"invalid arguments for {name}") from error
        except Exception as error:
            raise MCPError(-32603, f"GitLab tool failed: {name}") from error
        return CallToolResult(
            content=[TextContent(text=json.dumps(payload, ensure_ascii=False, sort_keys=True))]
        )

    @staticmethod
    def _validate_arguments(spec: GitLabToolSpec, arguments: dict[str, object]) -> None:
        if not isinstance(arguments, dict):
            raise MCPError(-32602, f"invalid arguments for {spec.name}")
        properties = spec.input_schema["properties"]
        required = set(spec.input_schema["required"])
        if not isinstance(properties, dict) or not isinstance(required, set):
            raise MCPError(-32603, "invalid internal GitLab tool schema")
        unknown = set(arguments) - set(properties)
        missing = required - set(arguments)
        if unknown or missing:
            raise MCPError(-32602, f"invalid arguments for {spec.name}")


def build_gitlab_stdio_server(facade: GitLabReadMcp) -> Server[None]:
    async def list_tools(
        _context: object, _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=list(facade.tools()))

    async def call_tool(_context: object, params: CallToolRequestParams) -> CallToolResult:
        return await facade.call_tool(params.name, dict(params.arguments or {}))

    return Server(
        "agentbot-gitlab-read",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_gitlab_stdio(facade: GitLabReadMcp) -> None:
    server = build_gitlab_stdio_server(facade)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentbot-gitlab-read")
    parser.add_argument(
        "--origin", default=os.environ.get("GITLAB_MCP_ORIGIN", "https://gitlab.com")
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITLAB_MCP_READ_TOKEN", "")
    client = GitLabReadClient(args.origin, token)
    asyncio.run(run_gitlab_stdio(GitLabReadMcp(client)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
