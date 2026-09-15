from __future__ import annotations

import json
import unittest
from pathlib import Path

from mcp import Client, MCPError
from src.gitlab_read_client import GitLabReadError
from src.gitlab_read_mcp import GitLabReadMcp, build_gitlab_stdio_server
from src.mcp_catalog import load_mcp_catalog
from src.mcp_contract import descriptor_fingerprint
from src.mcp_render import parse_mcp_config, render_mcp_config

EXPECTED_TOOLS = (
    "gitlab_project",
    "gitlab_repository_tree",
    "gitlab_repository_file",
    "gitlab_commits",
    "gitlab_refs",
    "gitlab_issues",
    "gitlab_merge_requests",
    "gitlab_merge_request_diff",
    "gitlab_notes",
    "gitlab_members",
    "gitlab_releases",
    "gitlab_pipelines",
    "gitlab_jobs",
    "gitlab_job_log",
    "gitlab_search",
)


class FakeGitLabClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str):
        def call(*args: object, **kwargs: object) -> dict[str, object]:
            self.calls.append((name, args, kwargs))
            return {
                "source": {"origin": "https://gitlab.example.com", "url": "/api/v4"},
                "untrusted": True,
                "data": {"operation": name},
            }

        return call


class GitLabReadMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = FakeGitLabClient()
        self.facade = GitLabReadMcp(self.client)

    def test_frozen_contract_and_admitted_catalog_entry_match_the_facade(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract = json.loads((root / "mcp" / "contracts" / "gitlab_read.json").read_text())
        tools = self.facade.tools()
        self.assertEqual(EXPECTED_TOOLS, tuple(tool.name for tool in tools))
        self.assertEqual(
            [tool["descriptor_fingerprint"] for tool in contract["tools"]],
            [descriptor_fingerprint(tool) for tool in tools],
        )
        entry = next(
            item
            for item in load_mcp_catalog(root / "mcp" / "catalog.json").entries
            if item.id == "gitlab_read"
        )
        # Admitted on 2026-09-14, once the live safe-project gate passed: all
        # fifteen reads against a real project, and every write-shaped call
        # proven to make no upstream request.
        self.assertTrue(entry.eligible)
        self.assertEqual(EXPECTED_TOOLS, entry.allowed_tools)
        self.assertEqual(("GITLAB_MCP_READ_TOKEN",), entry.credential.environment)
        rendered = render_mcp_config("codex", "", (entry,))
        native = parse_mcp_config("codex", rendered)["mcp_servers"][entry.name]
        self.assertEqual(["GITLAB_MCP_READ_TOKEN"], native["env_vars"])

    async def test_exact_fifteen_tool_surface_and_minimal_capabilities(self) -> None:
        self.assertEqual(EXPECTED_TOOLS, self.facade.tool_names())
        server = build_gitlab_stdio_server(self.facade)
        capabilities = server.get_capabilities()
        self.assertIsNotNone(capabilities.tools)
        self.assertIsNone(capabilities.prompts)
        self.assertIsNone(capabilities.resources)
        self.assertIsNone(capabilities.logging)
        async with Client(server, raise_exceptions=True) as protocol:
            listed = await protocol.list_tools()
            self.assertEqual(set(EXPECTED_TOOLS), {tool.name for tool in listed.tools})

    async def test_pipeline_mutations_and_generic_bypasses_are_uncallable(self) -> None:
        prohibited = (
            "save_pipeline",
            "manage_pipeline",
            "create_pipeline",
            "retry_job",
            "cancel_job",
            "pipeline_variables",
            "raw_request",
            "graphql",
            "artifacts",
            "packages",
            "registries",
            "agentbot_unknown_tool",
        )
        for name in prohibited:
            with self.subTest(name=name), self.assertRaisesRegex(MCPError, "unknown tool"):
                await self.facade.call_tool(name, {"project_id": "1"})
        self.assertEqual([], self.client.calls)

    async def test_every_tool_dispatches_to_one_typed_client_method(self) -> None:
        arguments = {
            "gitlab_project": {"project_id": "group/project"},
            "gitlab_repository_tree": {"project_id": "1"},
            "gitlab_repository_file": {
                "project_id": "1",
                "file_path": "README.md",
                "ref": "main",
            },
            "gitlab_commits": {"project_id": "1"},
            "gitlab_refs": {"project_id": "1", "kind": "branches"},
            "gitlab_issues": {"project_id": "1"},
            "gitlab_merge_requests": {"project_id": "1"},
            "gitlab_merge_request_diff": {"project_id": "1", "iid": 2},
            "gitlab_notes": {"project_id": "1", "kind": "issues", "iid": 2},
            "gitlab_members": {"project_id": "1"},
            "gitlab_releases": {"project_id": "1"},
            "gitlab_pipelines": {"project_id": "1"},
            "gitlab_jobs": {"project_id": "1"},
            "gitlab_job_log": {"project_id": "1", "job_id": 3},
            "gitlab_search": {"project_id": "1", "scope": "blobs", "search": "x"},
        }
        for tool_name in EXPECTED_TOOLS:
            result = await self.facade.call_tool(tool_name, arguments[tool_name])
            payload = json.loads(result.content[0].text)
            self.assertTrue(payload["untrusted"])
        self.assertEqual(
            [name.removeprefix("gitlab_") for name in EXPECTED_TOOLS],
            [call[0] for call in self.client.calls],
        )

    async def test_client_errors_are_sanitized_as_mcp_errors(self) -> None:
        class FailingClient(FakeGitLabClient):
            def project(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                raise GitLabReadError("GitLab returned HTTP 403")

        facade = GitLabReadMcp(FailingClient())
        with self.assertRaisesRegex(MCPError, "GitLab returned HTTP 403"):
            await facade.call_tool("gitlab_project", {"project_id": "1"})


if __name__ == "__main__":
    unittest.main()


class RedactionTests(unittest.TestCase):
    """Credential values must not leave the facade.

    Found by an audit, not by a test: `gitlab_project` returned GitLab's project
    payload verbatim, and `runners_token` -- a live runner registration token,
    which is effectively arbitrary code execution in that project's CI -- was in
    it. Read-only access constrains mutation, not disclosure. Every probe up to
    that point had asked whether writes were refused; none had asked what the
    reads returned.
    """

    def test_a_token_value_is_replaced(self) -> None:
        from src.gitlab_read_mcp import REDACTED, redact_secrets

        cleaned = redact_secrets({"id": 1, "runners_token": "GR1348941abcdefghij"})

        self.assertEqual({"id": 1, "runners_token": REDACTED}, cleaned)

    def test_policy_fields_named_after_tokens_survive(self) -> None:
        """GitLab names plenty of harmless settings after tokens. Redacting a
        boolean protects nothing and destroys useful metadata."""
        from src.gitlab_read_mcp import redact_secrets

        payload = {
            "ci_job_token_scope_enabled": False,
            "ci_push_repository_for_job_token_allowed": False,
            "runner_token_expiration_interval": None,
            "ci_id_token_sub_claim_components": ["project_path", "ref_type"],
        }

        self.assertEqual(payload, redact_secrets(payload))

    def test_every_spelling_of_a_credential_is_caught(self) -> None:
        from src.gitlab_read_mcp import REDACTED, redact_secrets

        payload = {
            "runners_token": "a",
            "private_token": "b",
            "access_key": "c",
            "api_key": "d",
            "apiKey": "e",
            "client_secret": "f",
            "password": "g",
            "passwd": "h",
            "authorization": "i",
            "private_key": "j",
            "some_credential": "k",
        }

        cleaned = redact_secrets(payload)

        self.assertEqual({REDACTED}, set(cleaned.values()))
        self.assertEqual(set(payload), set(cleaned))

    def test_nested_and_listed_values_are_reached(self) -> None:
        """A single top-level pass would miss a token inside a member list or a
        nested settings object."""
        from src.gitlab_read_mcp import REDACTED, redact_secrets

        cleaned = redact_secrets(
            {
                "members": [{"name": "a", "access_token": "secret-one"}],
                "settings": {"nested": {"runners_token": "secret-two"}},
            }
        )

        self.assertEqual(REDACTED, cleaned["members"][0]["access_token"])
        self.assertEqual(REDACTED, cleaned["settings"]["nested"]["runners_token"])
        self.assertEqual("a", cleaned["members"][0]["name"])

    def test_an_empty_value_is_left_alone(self) -> None:
        """An empty string says the field is unset, which is information. The
        marker would imply something was withheld."""
        from src.gitlab_read_mcp import redact_secrets

        self.assertEqual({"runners_token": ""}, redact_secrets({"runners_token": ""}))

    def test_the_facade_redacts_before_the_response_leaves(self) -> None:
        """The property that matters: not that the helper works, but that every
        tool's payload passes through it."""
        import asyncio
        import json as _json

        from src.gitlab_read_mcp import REDACTED, GitLabReadMcp

        class LeakyClient:
            def project(self, **_: object) -> dict[str, object]:
                return {"id": 7, "runners_token": "GR1348941leakyvalue"}

        facade = GitLabReadMcp(LeakyClient())
        result = asyncio.run(facade.call_tool("gitlab_project", {"project_id": "x/y"}))
        text = result.content[0].text

        self.assertNotIn("GR1348941leakyvalue", text)
        self.assertEqual(REDACTED, _json.loads(text)["runners_token"])
