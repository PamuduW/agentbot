"""Project-local integration previews preserve unowned configuration and trust."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src import memory, memory_session_config
from src.paths import AgentbotPaths
from tests.test_memory_project_writes import WritesTestCase


class SessionConfigTests(WritesTestCase):
    def setUp(self):
        super().setUp()
        self.paths = AgentbotPaths(
            root=Path(__file__).resolve().parents[1], codex_home=self.tmp / "home/.codex",
            claude_home=self.tmp / "home/.claude", cursor_home=self.tmp / "home/.cursor",
            config_home=self.tmp / "settings/agentbot", agents_home=self.tmp / "home/.agents",
        )

    def configure(self, client="codex", surface="hooks", apply=False):
        return memory_session_config.configure(
            self.a, self.code, self.paths, client=client, surface=surface, apply=apply,
        )

    def test_all_clients_preview_apply_and_reapply_without_global_changes(self):
        for client in memory_session_config.CLIENT_FILES:
            for surface in ("hooks", "mcp"):
                with self.subTest(client=client, surface=surface):
                    preview = self.configure(client, surface)
                    path = Path(preview["path"])
                    self.assertFalse(path.exists())
                    self.assertEqual("preview", preview["state"])
                    applied = self.configure(client, surface, True)
                    self.assertEqual("applied", applied["state"])
                    text = path.read_text()
                    self.assertIn(str(self.code), text)
                    self.assertIn(str(self.a), text)
                    self.assertEqual("unchanged", self.configure(client, surface, True)["state"])
                    self.assertEqual(text, path.read_text())
        self.assertFalse(self.paths.codex_home.exists())
        self.assertFalse(self.paths.claude_home.exists())

    def test_preparation_is_read_only_and_saved_entries_authorize_only_owned_refresh(self):
        alternate = self.tmp / "relocated product"
        (alternate / "bin").mkdir(parents=True)
        (alternate / "bin/agentbot").touch()
        updated = replace(self.paths, root=alternate)
        for client in memory_session_config.CLIENT_FILES:
            for surface in ("hooks", "mcp"):
                with self.subTest(client=client, surface=surface):
                    old = self.configure(client, surface, True)
                    path = Path(old["path"])
                    before = path.read_text()
                    plan = memory_session_config.prepare(
                        self.a, self.code, updated, client=client, surface=surface,
                        previous=old["managed_entries"],
                    )
                    self.assertEqual(before, path.read_text())
                    self.assertIn(str(alternate), plan.after)
                    self.assertNotIn(str(self.paths.root), plan.after)
                    memory_session_config.apply_prepared(plan)
                    self.assertEqual(plan.after, path.read_text())
                    self.assertEqual(before, path.with_name(path.name + ".agentbot-backup").read_text())

    def test_saved_ownership_does_not_authorize_overwriting_an_edited_server(self):
        old = self.configure("cursor", "mcp", True)
        path = Path(old["path"])
        data = json.loads(path.read_text())
        data["mcpServers"]["agentbot_memory"]["command"] = "personal-server"
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        with self.assertRaises(memory.MemoryVaultError):
            memory_session_config.prepare(
                self.a, self.code, self.paths, client="cursor", surface="mcp",
                previous=old["managed_entries"],
            )
        self.assertEqual(before, path.read_bytes())

    def test_disabled_owned_hook_is_a_conflict_not_a_second_executable_handler(self):
        old = self.configure("cursor", "hooks", True)
        path = Path(old["path"])
        data = json.loads(path.read_text())
        data["hooks"]["sessionStart"][0]["command"] = "echo disabled"
        path.write_text(json.dumps(data))
        before = path.read_bytes()
        with self.assertRaises(memory.MemoryVaultError):
            memory_session_config.prepare(
                self.a, self.code, self.paths, client="cursor", surface="hooks",
                previous=old["managed_entries"],
            )
        self.assertEqual(before, path.read_bytes())

    def test_preserves_boost_hooks_and_unowned_settings(self):
        directory = self.code / ".claude"
        directory.mkdir()
        path = directory / "settings.local.json"
        boost = {"hooks": [{"type": "command", "command": "boost hook"}]}
        path.write_text(json.dumps({"unowned": "keep", "hooks": {"SessionStart": [boost]}}))
        original = path.read_text()
        preview = self.configure("claude", "hooks")
        self.assertIn("memory session hook", json.dumps(preview["managed_entries"]))
        self.assertNotIn("unowned", json.dumps(preview["managed_entries"]))
        self.configure("claude", "hooks", True)
        result = json.loads(path.read_text())
        self.assertEqual("keep", result["unowned"])
        self.assertEqual(boost, result["hooks"]["SessionStart"][0])
        self.assertEqual(original, path.with_name(path.name + ".agentbot-backup").read_text())

    def test_conflicting_server_and_memory_hook_refused_without_writes(self):
        for surface, name, payload in (
            ("mcp", "mcp.json", {"mcpServers": {"agentbot_memory": {"command": "unowned"}}}),
            ("hooks", "hooks.json", {"hooks": {"sessionStart": [
                {"command": "other-agentbot memory session hook --client cursor"}
            ]}}),
        ):
            path = self.code / ".cursor" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(payload))
            original = path.read_bytes()
            with self.assertRaises(memory.MemoryVaultError):
                self.configure("cursor", surface, True)
            self.assertEqual(original, path.read_bytes())

    def test_symlinked_parent_and_malformed_config_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.code / ".cursor").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(memory.MemoryVaultError):
            self.configure("cursor", "hooks", True)
        self.assertEqual([], list(outside.iterdir()))
        (self.code / ".cursor").unlink()
        (self.code / ".cursor").mkdir()
        (self.code / ".cursor/hooks.json").write_text('{"secret": "DO NOT PRINT",')
        with self.assertRaises(memory.MemoryVaultError) as raised:
            self.configure("cursor", "hooks", True)
        self.assertNotIn("DO NOT PRINT", str(raised.exception))

    def test_foreign_checkout_and_denied_write_are_not_success(self):
        with patch("src.memory_session_config.memory_projects.require_project",
                   side_effect=memory.MemoryVaultError("foreign")):
            with self.assertRaises(memory.MemoryVaultError):
                self.configure(apply=True)
        with patch("src.memory_session_config.write_text_atomic", side_effect=PermissionError()):
            with self.assertRaises(OSError):
                self.configure(apply=True)
        self.assertFalse((self.code / ".codex/hooks.json").exists())

    def test_codex_toml_preserves_unowned_server_and_comments(self):
        path = self.code / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text('# Keep this comment\n[mcp_servers.other]\ncommand = "keep"\n')
        self.configure("codex", "mcp", True)
        self.assertIn('# Keep this comment', path.read_text())
        self.assertIn('command = "keep"', path.read_text())

    def test_backup_symlink_refused_without_modifying_its_target(self):
        path = self.code / ".cursor/hooks.json"
        path.parent.mkdir()
        path.write_text('{}\n')
        target = self.tmp / "keep"
        target.write_text("preserve")
        path.with_name(path.name + ".agentbot-backup").symlink_to(target)
        with self.assertRaises(memory.MemoryVaultError):
            self.configure("cursor", "hooks", True)
        self.assertEqual("preserve", target.read_text())

    def test_antigravity_does_not_duplicate_another_memory_group(self):
        path = self.code / ".agents/hooks.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"custom-memory": {"SessionStart": [
            {"command": "other-agentbot memory session hook --client antigravity"}
        ]}}))
        with self.assertRaises(memory.MemoryVaultError):
            self.configure("antigravity", "hooks", True)

    def test_exact_owned_hook_does_not_hide_an_additional_memory_hook(self):
        for client in memory_session_config.CLIENT_FILES:
            with self.subTest(client=client):
                result = self.configure(client, "hooks", True)
                path = Path(result["path"])
                data = json.loads(path.read_text())
                if client == "antigravity":
                    data["another"] = {"SessionStart": [{"command": "agentbot memory session hook other"}]}
                else:
                    event = "sessionStart" if client == "cursor" else "SessionStart"
                    data["hooks"][event].append({"command": "agentbot memory session hook other"})
                path.write_text(json.dumps(data))
                before = path.read_bytes()
                with self.assertRaises(memory.MemoryVaultError):
                    self.configure(client, "hooks", True)
                self.assertEqual(before, path.read_bytes())

    def test_duplicate_json_keys_are_not_silently_discarded(self):
        for surface, name in (("hooks", "hooks.json"), ("mcp", "mcp.json")):
            path = self.code / ".cursor" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"unowned":1,"unowned":2}')
            with self.assertRaises(memory.MemoryVaultError):
                self.configure("cursor", surface, True)
            self.assertEqual('{"unowned":1,"unowned":2}', path.read_text())

    def test_present_null_memory_entries_are_conflicts(self):
        path = self.code / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text('{"mcpServers":{"agentbot_memory":null}}')
        with self.assertRaises(memory.MemoryVaultError):
            self.configure("cursor", "mcp", True)

    def test_configuration_never_copies_credentials_or_changes_native_memory(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-sensitive-value"}):
            result = self.configure("claude", "hooks", True)
        text = Path(result["path"]).read_text()
        self.assertNotIn("synthetic-sensitive-value", text)
        self.assertNotIn("autoMemoryEnabled", text)
        self.assertNotIn("permission", text)

    def test_cli_preview_then_apply_reports_no_model_verification(self):
        command = [sys.executable, "-m", "src.cli", "--root", str(self.paths.root),
                   "memory", "session", "configure", "--client", "cursor",
                   "--surface", "mcp", "--project-root", str(self.code), "--json"]
        environment = {**os.environ, "HOME": str(self.tmp / "home"),
                       "AGENTBOT_MEMORY_DIR": str(self.a),
                       "XDG_CONFIG_HOME": str(self.paths.config_home.parent)}
        for extra, state in (([], "preview"), (["--yes"], "applied"), (["--yes"], "unchanged")):
            result = subprocess.run(command + extra, env=environment, cwd=self.paths.root,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(0, result.returncode, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(state, data["state"])
            self.assertFalse(data["model_use_verified"])


if __name__ == "__main__":
    import unittest

    unittest.main()
