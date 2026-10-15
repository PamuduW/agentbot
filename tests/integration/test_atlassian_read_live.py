from __future__ import annotations

import os
import unittest

from src.atlassian_read_client import AtlassianReadClient
from src.atlassian_read_mcp import AtlassianReadMcp


class AtlassianReadLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ten_cloud_reads(self) -> None:
        if os.environ.get("AGENTBOT_TEST_ATLASSIAN_MCP") != "1":
            self.skipTest("set AGENTBOT_TEST_ATLASSIAN_MCP=1 for live Cloud reads")
        required = (
            "ATLASSIAN_MCP_CLOUD_ID",
            "ATLASSIAN_MCP_EMAIL",
            "JIRA_MCP_READ_TOKEN",
            "CONFLUENCE_MCP_READ_TOKEN",
            "ATLASSIAN_MCP_TEST_ISSUE",
            "ATLASSIAN_MCP_TEST_PAGE_ID",
            "ATLASSIAN_MCP_TEST_BOARD_ID",
        )
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            self.skipTest("missing live test references: " + ", ".join(missing))
        facade = AtlassianReadMcp(
            AtlassianReadClient(
                os.environ["ATLASSIAN_MCP_CLOUD_ID"],
                os.environ["ATLASSIAN_MCP_EMAIL"],
                os.environ["JIRA_MCP_READ_TOKEN"],
                os.environ["CONFLUENCE_MCP_READ_TOKEN"],
            )
        )
        issue = os.environ["ATLASSIAN_MCP_TEST_ISSUE"]
        page = os.environ["ATLASSIAN_MCP_TEST_PAGE_ID"]
        cases = {
            "jira_issue": {"issue_id": issue},
            "jira_search": {"jql": f"key = {issue}", "limit": 1},
            "jira_comments": {"issue_id": issue, "limit": 1},
            "jira_projects": {"limit": 1},
            "jira_boards": {"limit": 1},
            "jira_sprints": {
                "board_id": int(os.environ["ATLASSIAN_MCP_TEST_BOARD_ID"]),
                "limit": 1,
            },
            "confluence_page": {"page_id": page},
            "confluence_search": {"cql": f"type = page AND id = {page}", "limit": 1},
            "confluence_spaces": {"limit": 1},
            "confluence_comments": {"page_id": page, "limit": 1},
        }
        self.assertEqual(set(cases), {tool.name for tool in facade.tools()})
        for name in cases:
            with self.subTest(tool=name):
                result = await facade.call_tool(name, cases[name])
                self.assertFalse(result.is_error, "Cloud read failed")
        result = await facade.call_tool(
            "confluence_comments",
            {
                "page_id": page,
                "kind": "inline",
                "limit": 1,
            },
        )
        self.assertFalse(result.is_error, "Cloud inline-comment read failed")


if __name__ == "__main__":
    unittest.main()
