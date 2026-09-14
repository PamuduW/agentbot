from __future__ import annotations

import os
import unittest

from mcp import MCPError
from src.gitlab_read_client import GitLabReadClient
from src.gitlab_read_mcp import GitLabReadMcp
from tests.test_gitlab_read_mcp import EXPECTED_TOOLS


class GitLabReadLiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("AGENTBOT_TEST_GITLAB_MCP") != "1":
            raise unittest.SkipTest("set AGENTBOT_TEST_GITLAB_MCP=1 for the GitLab admission gate")
        token = os.environ.get("GITLAB_MCP_READ_TOKEN")
        project_id = os.environ.get("GITLAB_MCP_TEST_PROJECT")
        if not token or not project_id:
            raise unittest.SkipTest("set GITLAB_MCP_READ_TOKEN and GITLAB_MCP_TEST_PROJECT")
        origin = os.environ.get("GITLAB_MCP_ORIGIN", "https://gitlab.com")
        cls.project_id = project_id
        cls.facade = GitLabReadMcp(GitLabReadClient(origin, token))

    async def test_project_read_and_exact_tool_surface(self) -> None:
        self.assertEqual(EXPECTED_TOOLS, self.facade.tool_names())
        result = await self.facade.call_tool("gitlab_project", {"project_id": self.project_id})
        self.assertFalse(result.is_error)

    async def test_prohibited_calls_make_no_http_request(self) -> None:
        for name in (
            "save_pipeline",
            "manage_pipeline",
            "pipeline_variables",
            "raw_request",
            "graphql",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(MCPError, "unknown tool"):
                await self.facade.call_tool(name, {"project_id": self.project_id})


if __name__ == "__main__":
    unittest.main()
