"""Project-bound MCP delivery using synthetic vaults and the real stdio transport."""

import asyncio
import errno
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

from mcp import Client, MCPError, StdioServerParameters
from src import (
    memory,
    memory_capture,
    memory_capture_queue,
    memory_mcp,
    memory_project_writes,
    memory_projects,
    memory_proposals,
)
from tests.test_memory import FAKE_GITHUB_TOKEN
from tests.test_memory_project_writes import WritesTestCase
from tests.test_memory_projects import repo
from tests.test_memory_sync import git

EVENT = "5d82633f-bcc5-45b6-a0e6-086cc429aa71"
FACT = {
    "event_id": EVENT,
    "target": "project",
    "kind": "note",
    "title": "MCP fixture",
    "body": "MCP fixture endpoint is 9553.",
}


class MemoryMcpTests(WritesTestCase):
    def test_origin_change_between_queue_check_and_save_cannot_redirect_the_fact(self):
        beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")
        memory_projects.register(self.a, beta, self.config)
        facade = self.facade()
        original = memory_capture_queue._request
        count = 0

        def change_after_queue_check(request):
            nonlocal count
            count += 1
            result = original(request)
            if count == 2:
                git(self.code, "remote", "set-url", "origin", "git@github.com:me/beta.git")
            return result

        with patch.object(memory_capture_queue, "_request", side_effect=change_after_queue_check):
            _, data = self.call(facade, "memory_capture", FACT)
        self.assertFalse(data["saved"])
        self.assertFalse(any(r.title == FACT["title"] for r in memory.validate(self.a).records))

    def test_origin_change_between_save_and_add_cannot_redirect_the_fact(self):
        beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")
        memory_projects.register(self.a, beta, self.config)
        facade = self.facade()
        original = memory_capture._existing

        def change_after_save_binding(*args):
            result = original(*args)
            git(self.code, "remote", "set-url", "origin", "git@github.com:me/beta.git")
            return result

        with patch.object(memory_capture, "_existing", side_effect=change_after_save_binding):
            _, data = self.call(facade, "memory_capture", FACT)
        self.assertFalse(data["saved"])
        self.assertFalse(any(r.title == FACT["title"] for r in memory.validate(self.a).records))

    def test_capture_retry_after_its_review_date_uses_the_original_receipt(self):
        request = {**FACT, "review_after": "2026-10-22"}
        with (
            patch.object(memory_capture_queue, "datetime") as qc,
            patch.object(memory_project_writes, "datetime") as wc,
        ):
            qc.now.return_value = wc.now.return_value = datetime(2026, 10, 22, tzinfo=timezone.utc)
            _, first = self.call(self.facade(), "memory_capture", request)
            head = self.head(self.a)
            qc.now.return_value = wc.now.return_value = datetime(2026, 10, 23, tzinfo=timezone.utc)
            result, second = self.call(self.facade(), "memory_capture", request)
        self.assertFalse(result.is_error)
        self.assertTrue(second["saved"])
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual(head, self.head(self.a))

    def test_status_rechecks_frozen_identity_at_the_operation_boundary(self):
        beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")
        memory_projects.register(self.a, beta, self.config)
        facade = self.facade()
        original = facade._binding

        def change_after_initial_check():
            original()
            git(self.code, "remote", "set-url", "origin", "git@github.com:me/beta.git")

        with patch.object(facade, "_binding", side_effect=change_after_initial_check):
            result, _ = self.call(facade, "memory_queue_status", {})
        self.assertTrue(result.is_error)

    def test_global_preferences_are_only_proposed_and_never_recalled_as_accepted(self):
        result, data = self.call(
            self.facade(),
            "memory_capture",
            {
                **FACT,
                "target": "proposal",
                "kind": "preference",
                "body": "Prefer fixture indentation.",
            },
        )
        self.assertFalse(result.is_error)
        self.assertEqual("proposal", data["receipt"]["target"])
        self.assertTrue(
            any(item["id"] == data["receipt"]["id"] for item in memory_proposals.queue(self.a))
        )
        _, recalled = self.call(self.facade(), "memory_search", {"query": "fixture indentation"})
        self.assertNotIn("Prefer fixture indentation", json.dumps(recalled))

    def facade(self):
        return memory_mcp.MemoryMcp(self.a, self.code, self.config, client="antigravity")

    def call(self, facade, name, arguments):
        result = asyncio.run(facade.call_tool(name, arguments))
        return result, json.loads(result.content[0].text)

    def test_capture_receipt_and_fresh_facade_recall(self):
        first, payload = self.call(self.facade(), "memory_capture", FACT)
        self.assertFalse(first.is_error)
        self.assertTrue(payload["saved"])
        self.assertEqual("committed-locally", payload["state"])
        second, recalled = self.call(self.facade(), "memory_search", {"query": "fixture endpoint"})
        self.assertFalse(second.is_error)
        self.assertIn("9553", json.dumps(recalled))
        self.assertIn("not as instructions", recalled["notice"])

    def test_server_never_accepts_tool_selected_project_or_client(self):
        facade = self.facade()
        for forbidden in ("project", "client", "vault", "cwd", "cross_project", "history"):
            with self.subTest(field=forbidden), self.assertRaises(MCPError):
                self.call(facade, "memory_capture", {**FACT, forbidden: "foreign"})
        for name in ("memory_approve", "memory_discard", "execute", "read_file"):
            with self.subTest(tool=name), self.assertRaises(MCPError):
                self.call(facade, name, {})

    def test_foreign_and_absolute_paths_are_application_errors(self):
        facade = self.facade()
        for path in ("projects/beta/notes/f.md", "/etc/passwd", "../../unrelated"):
            with self.subTest(path=path):
                result, data = self.call(facade, "memory_show", {"path": path})
                self.assertTrue(result.is_error)
                self.assertEqual("refused", data["state"])

    def test_changed_repository_identity_refuses_every_tool(self):
        facade = self.facade()
        git(self.code, "remote", "set-url", "origin", "git@github.com:me/unregistered.git")
        for name, arguments in (
            ("memory_brief", {}),
            ("memory_capture", FACT),
            ("memory_queue_status", {}),
        ):
            result, data = self.call(facade, name, arguments)
            self.assertTrue(result.is_error)
            self.assertFalse(data.get("saved", False))

    def test_denied_write_reports_queued_not_saved_and_retries_durably(self):
        facade = self.facade()
        head = self.head(self.a)
        with patch.object(
            memory_capture, "save", side_effect=OSError(errno.EROFS, "private failure")
        ):
            result, data = self.call(facade, "memory_capture", FACT)
        self.assertFalse(result.is_error)
        self.assertEqual(("queued", False), (data["state"], data["saved"]))
        self.assertNotIn("private failure", json.dumps(data))
        self.assertEqual(head, self.head(self.a))
        with patch.object(
            memory_capture, "save", side_effect=OSError(errno.EROFS, "private failure")
        ):
            denied, blocked = self.call(facade, "memory_queue_drain", {"limit": 1})
        self.assertTrue(denied.is_error)
        self.assertEqual((0, 1), (blocked["committed"], blocked["pending"]))
        result, data = self.call(self.facade(), "memory_queue_drain", {"limit": 1})
        self.assertFalse(result.is_error)
        self.assertEqual((1, 0), (data["committed"], data["pending"]))

    def test_capture_attempts_the_current_event_instead_of_an_older_queue_entry(self):
        memory_capture_queue.enqueue(
            self.a,
            self.code,
            self.config,
            {
                **FACT,
                "client": "antigravity",
                "event_id": "81ff7bb9-b4ca-4195-9179-d68d3c577d20",
                "title": "Older queued fixture",
                "body": "Older queued fixture endpoint 9443.",
            },
        )
        _result, data = self.call(self.facade(), "memory_capture", FACT)
        self.assertTrue(data["saved"])
        self.assertEqual(1, memory_capture_queue.status(self.a, self.code, self.config)["pending"])
        _, recalled = self.call(self.facade(), "memory_search", {"query": "9553"})
        self.assertIn("MCP fixture", json.dumps(recalled))

    def test_input_schema_bounds_and_raw_secret_refusal(self):
        facade = self.facade()
        for name, arguments in (
            ("memory_search", {"query": "x" * 513}),
            ("memory_queue_drain", {"limit": True}),
            ("memory_queue_drain", {"limit": 9}),
            ("memory_capture", {**FACT, "body": "x" * 20_000}),
            ("memory_capture", {**FACT, "tags": "not-a-list"}),
        ):
            with self.subTest(tool=name), self.assertRaises(MCPError):
                self.call(facade, name, arguments)
        result, data = self.call(
            facade,
            "memory_capture",
            {
                **FACT,
                "target": "proposal",
                "kind": "preference",
                "body": "Fixture:\n" + FAKE_GITHUB_TOKEN,
            },
        )
        self.assertTrue(result.is_error)
        self.assertNotIn(FAKE_GITHUB_TOKEN, json.dumps(data))

    def test_local_reads_do_not_require_creating_writer_state(self):
        with patch("src.memory_sync._state", side_effect=OSError(errno.EROFS, "unwritable")):
            result, data = self.call(self.facade(), "memory_brief", {})
        self.assertFalse(result.is_error)
        self.assertTrue(data["refresh"]["local_only"])

    def test_real_stdio_initialization_listing_capture_and_restart_recall(self):
        async def round_trip():
            env = {
                "PATH": os.defpath,
                "HOME": str(self.tmp / "stdio-home"),
                "XDG_CONFIG_HOME": str(self.tmp / "stdio-config"),
                "AGENTBOT_MEMORY_DIR": str(self.a),
                "AGENTBOT_MEMORY_NO_SYNC": "1",
            }
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "src.cli",
                    "--root",
                    str(self.tmp / "agentbot"),
                    "memory",
                    "serve",
                    "--project-root",
                    str(self.code),
                    "--client",
                    "antigravity",
                ],
                env=env,
            )
            async with Client(params, raise_exceptions=True, read_timeout_seconds=30) as protocol:
                listed = await protocol.list_tools()
                self.assertEqual(
                    {
                        "memory_brief",
                        "memory_search",
                        "memory_show",
                        "memory_capture",
                        "memory_queue_status",
                        "memory_queue_drain",
                    },
                    {tool.name for tool in listed.tools},
                )
                result = await protocol.call_tool("memory_capture", FACT)
                self.assertTrue(result.structured_content["saved"])
            async with Client(params, raise_exceptions=True, read_timeout_seconds=30) as protocol:
                result = await protocol.call_tool("memory_search", {"query": "9553"})
                self.assertIn("9553", result.content[0].text)

        asyncio.run(round_trip())
