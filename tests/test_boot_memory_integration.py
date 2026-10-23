"""The public boot command offers complete memory enrollment in one invocation."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from src import memory_projects
from src.workspace_state import WorkspaceStore
from tests.support import run_cli_main
from tests.test_memory_project_writes import WritesTestCase


class BootMemoryIntegrationTests(WritesTestCase):
    def boot(self, *args, interactive=False, answers=(), vault=True):
        environment = {**os.environ, "HOME": str(self.tmp / "home"),
                       "XDG_CONFIG_HOME": str(self.tmp / "settings"),
                       "AGENTBOT_MEMORY_DIR": str(self.a) if vault else "",
                       "AGENTBOT_CALLER_PWD": str(self.code)}
        with (
            patch.dict(os.environ, environment),
            patch("src.cli._at_a_terminal", return_value=interactive),
            patch("src.workspace_memory.discover_clients", return_value=("claude", "cursor")),
            patch("builtins.input", side_effect=answers),
        ):
            return run_cli_main(["agentbot", "--root", str(Path(__file__).resolve().parents[1]),
                                 "boot", *args])

    def test_interactive_boot_selects_previews_and_applies_without_another_command(self):
        code, output, error = self.boot(interactive=True, answers=("cursor", "y"))
        self.assertEqual(0, code, output + error)
        self.assertIn("Memory integrations", output)
        self.assertIn(".cursor/mcp.json", output)
        self.assertIn(str(self.a), output)
        self.assertTrue((self.code / ".cursor/hooks.json").exists())
        self.assertFalse((self.code / ".claude/settings.local.json").exists())
        record = WorkspaceStore(self.tmp / "settings/agentbot/workspaces.json").load()[0]
        self.assertEqual({"cursor"}, set(record.memory_bindings))

    def test_noninteractive_boot_requires_explicit_enrollment_confirmation(self):
        code, output, error = self.boot("--memory-clients", "cursor")
        self.assertEqual(1, code, output + error)
        self.assertFalse((self.code / ".cursor/hooks.json").exists())
        code, output, error = self.boot("--memory-clients", "cursor", "--yes")
        self.assertEqual(0, code, output + error)
        self.assertTrue((self.code / ".cursor/hooks.json").exists())

    def test_dry_run_does_not_render_register_or_write_memory(self):
        self.assertFalse((self.code / "AGENTS.md").exists())
        head = self.head(self.a)
        code, output, error = self.boot("--memory-clients", "cursor", "--dry-run")
        self.assertEqual(0, code, output + error)
        self.assertFalse((self.code / "AGENTS.md").exists())
        self.assertFalse((self.tmp / "settings/agentbot/workspaces.json").exists())
        self.assertFalse((self.code / ".cursor").exists())
        self.assertEqual(head, self.head(self.a))

    def test_no_memory_skips_registration_and_all_integrations(self):
        head = self.head(self.a)
        code, output, error = self.boot("--no-memory")
        self.assertEqual(0, code, output + error)
        self.assertEqual(head, self.head(self.a))
        self.assertFalse((self.code / ".cursor/hooks.json").exists())

    def test_second_boot_preserves_saved_clients_and_personal_mcp_entries(self):
        self.boot("--memory-clients", "cursor", "--yes")
        path = self.code / ".cursor/mcp.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["personal"] = {"command": "keep-this"}
        path.write_text(json.dumps(data))
        code, output, error = self.boot()
        self.assertEqual(0, code, output + error)
        self.assertEqual("keep-this", json.loads(path.read_text())["mcpServers"]["personal"]["command"])
        self.assertFalse((self.code / ".claude/settings.local.json").exists())

    def test_memory_conflict_is_not_a_successful_complete_boot(self):
        path = self.code / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text('{"mcpServers":{"agentbot_memory":{"command":"personal"}}}')
        code, output, error = self.boot("--memory-clients", "cursor", "--yes")
        self.assertEqual(1, code, output + error)
        self.assertIn("review", output.lower())
        self.assertFalse((self.code / ".cursor/hooks.json").exists())

    def test_no_vault_is_optional_and_does_not_create_a_new_one(self):
        with patch("src.memory.find_vault", return_value=(None, ())):
            code, output, error = self.boot(vault=False)
        self.assertEqual(0, code, output + error)
        self.assertIn("no memory vault", output)
        self.assertFalse((self.code / ".cursor/hooks.json").exists())

    def test_enrolled_memory_preparation_failure_is_not_a_successful_boot(self):
        self.boot("--memory-clients", "cursor", "--yes")
        (self.a / ".meta/vault.json").write_text("invalid JSON")
        code, output, error = self.boot()
        self.assertEqual(1, code, output + error)

    def test_unregistered_project_dry_run_does_not_register_it(self):
        # The project registry exists, but this different origin has no entry yet.
        from tests.test_memory_projects import repo
        beta = repo(self.tmp / "beta", "git@github.com:example/boot-beta.git")
        code, output, error = self.boot(str(beta), "--dry-run", "--memory-clients", "cursor")
        self.assertEqual(0, code, output + error)
        self.assertEqual("unregistered", memory_projects.resolve(
            self.a, beta, self.tmp / "settings/agentbot"
        ).state)
