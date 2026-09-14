from __future__ import annotations

import json
import os
import time
import unittest
from typing import Any, ClassVar, cast

from mcp.types import TextContent

from mcp import MCPError
from src.gitlab_read_client import GitLabReadClient
from src.gitlab_read_mcp import GitLabReadMcp

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


class GitLabReadLiveTests(unittest.IsolatedAsyncioTestCase):
    project_id: ClassVar[str]
    instance_version: ClassVar[str]
    tier: ClassVar[str]
    token_kind: ClassVar[str]
    role: ClassVar[str]
    facade: ClassVar[GitLabReadMcp]
    timings: ClassVar[dict[str, float]]

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("AGENTBOT_TEST_GITLAB_MCP") != "1":
            raise unittest.SkipTest("set AGENTBOT_TEST_GITLAB_MCP=1 for the GitLab admission gate")

        required = (
            "GITLAB_MCP_READ_TOKEN",
            "GITLAB_MCP_TEST_PROJECT",
            "GITLAB_MCP_INSTANCE_VERSION",
            "GITLAB_MCP_TEST_TIER",
            "GITLAB_MCP_TOKEN_KIND",
            "GITLAB_MCP_TEST_ROLE",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise unittest.SkipTest(f"set the GitLab admission variables: {', '.join(missing)}")

        cls.project_id = os.environ["GITLAB_MCP_TEST_PROJECT"]
        cls.instance_version = os.environ["GITLAB_MCP_INSTANCE_VERSION"]
        cls.tier = os.environ["GITLAB_MCP_TEST_TIER"].lower()
        cls.token_kind = os.environ["GITLAB_MCP_TOKEN_KIND"].lower()
        cls.role = os.environ["GITLAB_MCP_TEST_ROLE"]
        origin = os.environ.get("GITLAB_MCP_ORIGIN", "https://gitlab.com")
        cls.facade = GitLabReadMcp(GitLabReadClient(origin, os.environ["GITLAB_MCP_READ_TOKEN"]))
        cls.timings = {}

    async def _call(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        started = time.perf_counter()
        result = await self.facade.call_tool(name, arguments)
        self.timings[name] = time.perf_counter() - started
        self.assertFalse(result.is_error)
        self.assertEqual(1, len(result.content))
        content = result.content[0]
        self.assertIsInstance(content, TextContent)
        content = cast(TextContent, content)
        payload = json.loads(content.text)
        self.assertTrue(payload["untrusted"])
        self.assertIn("source", payload)
        return payload

    @staticmethod
    def _data(payload: dict[str, Any]) -> Any:
        return payload["data"]

    async def test_all_fifteen_read_operations(self) -> None:
        self.assertEqual(EXPECTED_TOOLS, self.facade.tool_names())
        self.assertIn(self.tier, {"free", "premium", "ultimate"})
        self.assertIn(self.token_kind, {"project", "group", "personal"})
        self.assertTrue(self.instance_version.strip())
        self.assertTrue(self.role.strip())

        project = self._data(await self._call("gitlab_project", {"project_id": self.project_id}))
        default_branch = project.get("default_branch")
        self.assertIsInstance(default_branch, str, "test project needs a default branch")

        tree = self._data(
            await self._call(
                "gitlab_repository_tree",
                {
                    "project_id": self.project_id,
                    "ref": default_branch,
                    "per_page": 20,
                    "max_pages": 1,
                },
            )
        )
        first_blob = next((entry for entry in tree if entry.get("type") == "blob"), None)
        self.assertIsNotNone(first_blob, "test project needs at least one repository file")
        first_blob = cast(dict[str, Any], first_blob)
        await self._call(
            "gitlab_repository_file",
            {
                "project_id": self.project_id,
                "file_path": first_blob["path"],
                "ref": default_branch,
                "max_bytes": 524_288,
            },
        )

        await self._call(
            "gitlab_commits", {"project_id": self.project_id, "per_page": 20, "max_pages": 1}
        )
        await self._call(
            "gitlab_refs",
            {"project_id": self.project_id, "kind": "branches", "per_page": 20, "max_pages": 1},
        )

        issues = self._data(
            await self._call(
                "gitlab_issues", {"project_id": self.project_id, "per_page": 20, "max_pages": 1}
            )
        )
        merge_requests = self._data(
            await self._call(
                "gitlab_merge_requests",
                {"project_id": self.project_id, "per_page": 20, "max_pages": 1},
            )
        )
        self.assertTrue(merge_requests, "test project needs at least one merge request")
        merge_request_iid = merge_requests[0]["iid"]
        await self._call(
            "gitlab_merge_request_diff",
            {"project_id": self.project_id, "iid": merge_request_iid, "max_bytes": 4_194_304},
        )

        if issues:
            note_kind, note_iid = "issues", issues[0]["iid"]
        else:
            note_kind, note_iid = "merge_requests", merge_request_iid
        await self._call(
            "gitlab_notes",
            {
                "project_id": self.project_id,
                "kind": note_kind,
                "iid": note_iid,
                "per_page": 20,
                "max_pages": 1,
            },
        )

        await self._call(
            "gitlab_members", {"project_id": self.project_id, "per_page": 20, "max_pages": 1}
        )
        await self._call(
            "gitlab_releases", {"project_id": self.project_id, "per_page": 20, "max_pages": 1}
        )
        await self._call(
            "gitlab_pipelines", {"project_id": self.project_id, "per_page": 20, "max_pages": 1}
        )
        jobs = self._data(
            await self._call(
                "gitlab_jobs", {"project_id": self.project_id, "per_page": 20, "max_pages": 1}
            )
        )
        self.assertTrue(jobs, "test project needs at least one CI job")
        await self._call("gitlab_job_log", {"project_id": self.project_id, "job_id": jobs[0]["id"]})
        await self._call(
            "gitlab_search",
            {
                "project_id": self.project_id,
                "scope": "blobs",
                "search": "README",
                "per_page": 20,
                "max_pages": 1,
            },
        )

        self.assertEqual(set(EXPECTED_TOOLS), set(self.timings))
        for name, elapsed in self.timings.items():
            with self.subTest(tool=name):
                self.assertLessEqual(elapsed, 90.0)

    async def test_prohibited_calls_make_no_http_request(self) -> None:
        for name in (
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
        ):
            with self.subTest(name=name), self.assertRaisesRegex(MCPError, "unknown tool"):
                await self.facade.call_tool(name, {"project_id": self.project_id})


if __name__ == "__main__":
    unittest.main()
