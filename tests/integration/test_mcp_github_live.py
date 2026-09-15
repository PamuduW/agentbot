from __future__ import annotations

import hashlib
import json
import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tests.test_mcp_github_contract import EXPECTED_TOOLS


class GitHubRemoteSession:
    def __init__(self, contract: dict[str, Any], token: str) -> None:
        self.endpoint = contract["endpoint"]
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **contract["headers"],
        }
        self.session_id: str | None = None
        self.request_id = 0

    def initialize(self) -> None:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "agentbot-contract-test", "version": "1"},
            },
        )
        if "result" not in response:
            raise AssertionError("GitHub MCP initialize did not return a result")
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        return self._send(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            }
        )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params}, empty_ok=True)

    def _send(self, payload: dict[str, Any], *, empty_ok: bool = False) -> dict[str, Any]:
        headers = dict(self.headers)
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                body = response.read(4_194_305)
                if len(body) > 4_194_304:
                    raise AssertionError("GitHub MCP response exceeded the contract limit")
                if not body and empty_ok:
                    return {}
                return _decode_mcp_body(body, response.headers.get_content_type())
        except urllib.error.HTTPError as error:
            body = error.read(65_537)
            if len(body) > 65_536:
                raise AssertionError("GitHub MCP error exceeded the contract limit") from error
            decoded = _decode_mcp_body(body, error.headers.get_content_type())
            if decoded:
                return decoded
            raise AssertionError(f"GitHub MCP returned HTTP {error.code}") from error


def _decode_mcp_body(body: bytes, content_type: str) -> dict[str, Any]:
    text = body.decode("utf-8")
    if content_type == "text/event-stream":
        data_lines = [line[5:].lstrip() for line in text.splitlines() if line.startswith("data:")]
        if not data_lines:
            return {}
        text = "\n".join(data_lines)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise AssertionError("GitHub MCP returned a non-object JSON-RPC response")
    return value


def _descriptor_fingerprint(tool: dict[str, Any]) -> str:
    encoded = json.dumps(
        tool,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class GitHubMcpLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("AGENTBOT_TEST_GITHUB_MCP") != "1":
            raise unittest.SkipTest("set AGENTBOT_TEST_GITHUB_MCP=1 for the GitHub MCP gate")
        token = os.environ.get("GITHUB_MCP_TOKEN")
        if not token:
            raise unittest.SkipTest("set GITHUB_MCP_TOKEN for the GitHub MCP gate")
        root = Path(__file__).resolve().parents[2]
        cls.contract = json.loads(
            (root / "mcp" / "contracts" / "github.json").read_text(encoding="utf-8")
        )
        cls.session = GitHubRemoteSession(cls.contract, token)
        cls.session.initialize()

    def test_tool_list_and_descriptors_match_the_frozen_contract(self) -> None:
        response = self.session.request("tools/list", {})
        tools = response.get("result", {}).get("tools", [])
        by_name = {tool["name"]: tool for tool in tools}
        self.assertEqual(set(EXPECTED_TOOLS), set(by_name))
        expected_fingerprints = {
            tool["name"]: tool["descriptor_fingerprint"] for tool in self.contract["tools"]
        }
        self.assertEqual(
            expected_fingerprints,
            {name: _descriptor_fingerprint(tool) for name, tool in by_name.items()},
        )

    def test_write_alias_and_unknown_tools_are_rejected_before_arguments(self) -> None:
        for name in ("actions_run_trigger", "run_workflow", "agentbot_unknown_tool"):
            with self.subTest(name=name):
                response = self.session.request(
                    "tools/call",
                    {"name": name, "arguments": {}},
                )
                text = json.dumps(response, sort_keys=True).lower()
                self.assertTrue(
                    any(
                        marker in text for marker in ("unknown tool", "not found", "does not exist")
                    ),
                    f"{name} reached argument validation instead of failing closed",
                )


if __name__ == "__main__":
    unittest.main()
