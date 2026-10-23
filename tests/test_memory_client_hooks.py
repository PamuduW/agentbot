"""Documented hook envelopes with no transcript access or permission overrides."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import memory, memory_client_hooks, memory_project_writes, memory_session
from tests.test_memory_project_writes import WritesTestCase


class MemoryClientHookTests(WritesTestCase):
    def payload(self, client, event):
        if client == "antigravity":
            return {
                "conversationId": "session-1",
                "workspacePaths": [str(self.code)],
                "invocationNum": 8,
            }
        if client == "cursor":
            return {
                "conversation_id": "session-1",
                "workspace_roots": [str(self.code)],
                "tool_use_id": "tool-1",
                "hook_event_name": event,
            }
        return {
            "session_id": "session-1",
            "cwd": str(self.code),
            "source": "compact",
            "tool_use_id": "tool-1",
            "hook_event_name": event,
        }

    def call(self, client, event, payload=None):
        return memory_client_hooks.adapt(
            self.a,
            self.code,
            self.config,
            client=client,
            event=event,
            payload=self.payload(client, event) if payload is None else payload,
        )

    def cli(self, *args, body=""):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "src.cli",
                "--root",
                str(self.tmp / "product"),
                "memory",
                "session",
                *args,
            ],
            input=body,
            text=True,
            capture_output=True,
            cwd=Path(__file__).resolve().parents[1],
            timeout=15,
            env={
                "PATH": os.defpath,
                "HOME": str(self.tmp / "home"),
                "XDG_CONFIG_HOME": str(self.config),
                "NO_COLOR": "1",
                "AGENTBOT_MEMORY_DIR": str(self.a),
                "AGENTBOT_CALLER_PWD": str(self.code),
            },
        )

    def test_hook_parser_recursion_fails_open_on_older_python(self):
        from src.cli import _handle_memory_session

        context = SimpleNamespace(
            args=SimpleNamespace(
                session_action="hook",
                client="codex",
                event="SessionStart",
                project_root=str(self.code),
            ),
            paths=SimpleNamespace(root=self.tmp, config_home=self.config),
        )
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("src.memory.find_vault", return_value=(self.a, ())),
            patch("src.cli.sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))),
            patch("src.cli.sys.stdout", out),
            patch("src.cli.sys.stderr", err),
            patch("src.cli.json.loads", side_effect=RecursionError("PRIVATE PARSER INPUT")),
        ):
            self.assertEqual(0, _handle_memory_session(context))
        self.assertEqual({}, json.loads(out.getvalue()))
        self.assertNotIn("PRIVATE", err.getvalue())

    def test_real_cli_recall_and_checkpoint_emit_truthful_json(self):
        data = self.cli("recall", "--client", "codex", "--session-id", "cli-1", "--json")
        self.assertEqual(0, data.returncode, data.stderr)
        self.assertFalse(json.loads(data.stdout)["model_use_verified"])
        data = self.cli(
            "checkpoint",
            "--client",
            "codex",
            "--session-id",
            "cli-1",
            "--event-id",
            "event-1",
            "--json",
        )
        self.assertEqual(0, data.returncode, data.stderr)
        self.assertFalse(json.loads(data.stdout)["saved"])

    def test_real_cli_hook_refuses_payload_without_echo_or_blocking(self):
        args = (
            "hook",
            "--client",
            "codex",
            "--event",
            "SessionStart",
            "--project-root",
            str(self.code),
        )
        for body in ("PRIVATE INVALID INPUT", "[1]", "x" * 65537):
            result = self.cli(*args, body=body)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual({}, json.loads(result.stdout))
            self.assertNotIn("PRIVATE", result.stderr)
        valid = self.cli(*args, body=json.dumps(self.payload("codex", "SessionStart")))
        self.assertEqual(0, valid.returncode, valid.stderr)
        self.assertIn("additionalContext", json.loads(valid.stdout)["hookSpecificOutput"])

    def test_each_start_contract_emits_context_and_never_a_permission_override(self):
        for client, event in memory_client_hooks.START_EVENTS.items():
            with self.subTest(client=client):
                data = self.call(client, event)
                if client == "antigravity":
                    self.assertIn("ephemeralMessage", data["injectSteps"][0])
                elif client == "cursor":
                    self.assertIn("additional_context", data)
                else:
                    self.assertEqual(event, data["hookSpecificOutput"]["hookEventName"])
                    self.assertIn("additionalContext", data["hookSpecificOutput"])
                self.assertNotIn("permission", json.dumps(data).lower())

    def test_each_checkpoint_contract_is_deduplicated(self):
        for client, event in memory_client_hooks.CHECKPOINT_EVENTS.items():
            with self.subTest(client=client):
                first = self.call(client, event)
                self.assertIn("This reminder saved no fact", json.dumps(first))
                repeated = self.call(client, event)
                if client == "antigravity":
                    self.assertIn("Agentbot memory data", json.dumps(repeated))
                    self.assertNotIn("This reminder saved no fact", json.dumps(repeated))
                else:
                    self.assertEqual({}, repeated)

    def test_transcript_and_tool_content_are_not_read_or_echoed(self):
        payload = self.payload("antigravity", "PreInvocation")
        payload.update({"transcriptPath": "/never/read", "prompt": "PRIVATE FIXTURE"})
        data = self.call("antigravity", "PreInvocation", payload)
        self.assertNotIn("PRIVATE FIXTURE", json.dumps(data))
        self.assertNotIn("/never/read", json.dumps(data))

    def test_antigravity_next_invocation_refreshes_changed_facts_without_startup(self):
        record = memory_project_writes.add(
            self.a, self.code, self.config, kind="note", title="Recovery fixture",
            body="Synthetic recovery marker: old-context-value.",
        )
        first = self.call("antigravity", "PreInvocation")
        self.assertIn("old-context-value", json.dumps(first))
        memory_project_writes.edit(
            self.a, self.code, self.config, record["path"],
            body="Synthetic recovery marker: fresh-context-value.",
        )
        head = self.head(self.a)
        repeated = self.call("antigravity", "PreInvocation")
        self.assertIn("fresh-context-value", json.dumps(repeated))
        self.assertNotIn("This reminder saved no fact", json.dumps(repeated))
        payload = self.payload("antigravity", "PreInvocation")
        payload["invocationNum"] = 9
        updated = self.call("antigravity", "PreInvocation", payload)
        text = updated["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("fresh-context-value", text)
        self.assertNotIn("old-context-value", text)
        brief = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertIn("not as instructions", brief["notice"])
        self.assertLessEqual(brief["tokens"], 800)
        self.assertIn("This reminder saved no fact", text)
        self.assertEqual(head, self.head(self.a))
        self.assertNotIn("This reminder saved no fact", json.dumps(
            self.call("antigravity", "PreInvocation", payload)
        ))

    def test_antigravity_refresh_failure_does_not_suppress_same_invocation_retry(self):
        with (
            patch.object(memory_session, "recall", side_effect=memory.MemoryVaultError("busy")),
            self.assertRaises(memory.MemoryVaultError),
        ):
            self.call("antigravity", "PreInvocation")
        retried = self.call("antigravity", "PreInvocation")
        self.assertIn("Agentbot memory data", json.dumps(retried))

    def test_invalid_or_unbound_payload_is_refused(self):
        original = self.payload("codex", "SessionStart")
        for bad in (
            [],
            {},
            {**original, "cwd": str(self.tmp)},
            {**original, "hook_event_name": "Stop"},
        ):
            with self.subTest(payload=bad), self.assertRaises(memory.MemoryVaultError):
                self.call("codex", "SessionStart", bad)
        with self.assertRaises(memory.MemoryVaultError):
            self.call("antigravity", "PostToolUse")

    def test_multiple_workspace_roots_are_not_silently_combined(self):
        data = self.payload("cursor", "sessionStart")
        data["workspace_roots"].append(str(self.tmp))
        with self.assertRaises(memory.MemoryVaultError):
            self.call("cursor", "sessionStart", data)

    def test_read_failure_returns_no_memory_or_saved_claim(self):
        with patch.object(memory_session, "recall", side_effect=memory.MemoryVaultError("unsafe")):
            with self.assertRaises(memory.MemoryVaultError):
                self.call("codex", "SessionStart")
