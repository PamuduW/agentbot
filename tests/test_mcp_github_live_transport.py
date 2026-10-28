from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from tests.integration import test_mcp_github_live as live


class GitHubLiveTransportTests(unittest.TestCase):
    def session(self):
        return live.GitHubRemoteSession(
            {"endpoint": "https://api.githubcopilot.com/mcp/", "headers": {}},
            "synthetic-unused-token",
        )

    def error(self, status, content_type, body):
        headers = Message()
        headers["Content-Type"] = content_type
        return urllib.error.HTTPError(
            "https://api.githubcopilot.com/mcp/",
            status,
            "request failed",
            headers,
            io.BytesIO(body),
        )

    def test_http_errors_keep_the_status_without_exposing_response_bodies(self):
        canary = "synthetic-private-response"
        for status, content_type, body in (
            (401, "text/plain", canary.encode()),
            (403, "text/html", f"<html>{canary}</html>".encode()),
            (502, "text/plain", b""),
            (401, "application/json", json.dumps({"message": canary}).encode()),
        ):
            with self.subTest(status=status, content_type=content_type):
                with patch(
                    "urllib.request.urlopen", side_effect=self.error(status, content_type, body)
                ):
                    with self.assertRaisesRegex(AssertionError, f"HTTP {status}") as raised:
                        self.session().request("initialize", {})
                self.assertNotIn(canary, str(raised.exception))

    def test_protocol_rejections_remain_available_to_the_live_contract_tests(self):
        response = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "not allowed"}}
        with patch(
            "urllib.request.urlopen",
            side_effect=self.error(403, "application/json", json.dumps(response).encode()),
        ):
            self.assertEqual(response, self.session().request("tools/call", {}))

    def test_oversized_http_error_still_fails_closed(self):
        with patch(
            "urllib.request.urlopen", side_effect=self.error(502, "text/plain", b"x" * 65_537)
        ):
            with self.assertRaisesRegex(AssertionError, "exceeded the contract limit"):
                self.session().request("initialize", {})
