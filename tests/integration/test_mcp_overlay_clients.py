from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.mcp_catalog import load_mcp_catalog
from src.mcp_render import remove_owned_entry, render_mcp_config

CLIENT_COMMANDS = ("claude", "codex", "cursor-agent")
CANDIDATES = ("context7", "aws_knowledge", "microsoft_learn")


class McpOverlayClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("AGENTBOT_TEST_MCP_CLIENTS") != "1":
            raise unittest.SkipTest(
                "set AGENTBOT_TEST_MCP_CLIENTS=1 for installed CLI acceptance"
            )
        missing = [command for command in CLIENT_COMMANDS if shutil.which(command) is None]
        if missing:
            raise unittest.SkipTest(f"missing installed clients: {', '.join(missing)}")
        cls.root = Path(__file__).resolve().parents[2]
        cls.entries = {
            entry.id: entry
            for entry in load_mcp_catalog(cls.root / "mcp" / "catalog.json").entries
        }

    def run_client(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"{' '.join(command)} failed:\n{result.stdout}\n{result.stderr}",
        )
        return result

    def test_each_overlay_starts_in_all_clients_and_disables_cleanly(self) -> None:
        for candidate in CANDIDATES:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory(
                prefix="agentbot-mcp-client-"
            ) as raw_home:
                home = Path(raw_home)
                (home / ".cursor").mkdir()
                (home / ".codex").mkdir()
                entry = replace(self.entries[candidate], eligible=True)
                claude_path = home / ".claude.json"
                codex_path = home / ".codex" / "config.toml"
                cursor_path = home / ".cursor" / "mcp.json"
                claude_path.write_text(
                    render_mcp_config("claude", "{}", (entry,)), encoding="utf-8"
                )
                codex_path.write_text(
                    render_mcp_config("codex", "", (entry,)), encoding="utf-8"
                )
                cursor_path.write_text(
                    render_mcp_config("cursor", "{}", (entry,)), encoding="utf-8"
                )
                environment = {
                    "AGENTBOT_HOME": str(self.root),
                    "CODEX_HOME": str(home / ".codex"),
                    "HOME": raw_home,
                    "PATH": os.pathsep.join(
                        (
                            str(self.root / "bin"),
                            str(self.root / ".venv" / "bin"),
                            os.environ["PATH"],
                        )
                    ),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                }

                claude = self.run_client(
                    ["claude", "mcp", "list"], environment=environment
                )
                self.assertIn(f"{entry.name}:", claude.stdout)
                self.assertIn("Connected", claude.stdout)

                codex = self.run_client(
                    ["codex", "mcp", "get", entry.name, "--json"],
                    environment=environment,
                )
                codex_entry = json.loads(codex.stdout)
                self.assertEqual("agentbot", codex_entry["transport"]["command"])
                self.assertEqual(list(entry.entrypoint[1:]), codex_entry["transport"]["args"])

                cursor = self.run_client(
                    ["cursor-agent", "--approve-mcps", "mcp", "list-tools", entry.name],
                    environment=environment,
                )
                visible = {
                    match.group(1)
                    for line in cursor.stdout.splitlines()
                    if (match := re.match(r"^- ([^ ]+) ", line))
                }
                self.assertEqual(set(entry.allowed_tools), visible)

                claude_path.write_text(
                    remove_owned_entry("claude", claude_path.read_text(), entry.name),
                    encoding="utf-8",
                )
                codex_path.write_text(
                    remove_owned_entry("codex", codex_path.read_text(), entry.name),
                    encoding="utf-8",
                )
                cursor_path.write_text(
                    remove_owned_entry("cursor", cursor_path.read_text(), entry.name),
                    encoding="utf-8",
                )
                claude_disabled = self.run_client(
                    ["claude", "mcp", "list"], environment=environment
                )
                self.assertIn("No MCP servers configured", claude_disabled.stdout)
                codex_disabled = self.run_client(
                    ["codex", "mcp", "list", "--json"], environment=environment
                )
                self.assertEqual([], json.loads(codex_disabled.stdout))
                cursor_disabled = self.run_client(
                    ["cursor-agent", "mcp", "list"], environment=environment
                )
                self.assertNotIn(entry.name, cursor_disabled.stdout)


if __name__ == "__main__":
    unittest.main()
