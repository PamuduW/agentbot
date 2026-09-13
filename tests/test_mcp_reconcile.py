from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.mcp_models import (
    McpCatalog,
    McpCatalogEntry,
    McpClientContract,
    McpCredential,
)
from src.mcp_reconcile import (
    McpReconcileContext,
    apply_mcp_plan,
    plan_mcp,
    plan_mcp_off,
    restore_mcp_operation,
)
from src.mcp_state import McpStateStore
from src.paths import AgentbotPaths

OPERATION_ID = "20260913T120000Z-1234abcd"
NOW = "2026-09-13T12:00:00Z"


def catalog_entry(entry_id: str = "github", *, eligible: bool = True) -> McpCatalogEntry:
    return McpCatalogEntry(
        id=entry_id,
        name=f"agentbot_{entry_id}",
        publisher="Example",
        documentation_url="https://example.test/docs",
        license="MIT",
        terms_url="https://example.test/terms",
        reviewed_on="2026-09-13",
        eligible=eligible,
        transport="remote_http",
        origin=f"https://example.test/{entry_id}",
        entrypoint=(),
        clients=tuple(
            McpClientContract(client=client, enabled=True)
            for client in ("claude", "codex", "cursor")
        ),
        credential=McpCredential(mode="none", environment=()),
        allowed_tools=("search",),
        descriptor_fingerprints=("sha256:" + "a" * 64,),
        read_only_authority="read-only server mode",
        limitations=("publisher behavior can change",),
        timeout_seconds=30,
        max_response_bytes=1000000,
        doctor_rules=("origin_matches",),
    )


class McpReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = AgentbotPaths(
            root=self.root / "agentbot",
            codex_home=self.root / ".codex",
            claude_home=self.root / ".claude",
            cursor_home=self.root / ".cursor",
            config_home=self.root / ".config" / "agentbot",
            agents_home=self.root / ".agents",
        )
        self.catalog = McpCatalog(version=1, entries=(catalog_entry(), catalog_entry("docs")))
        self.context = self.make_context()

    def make_context(self, *, fault=None, catalog: McpCatalog | None = None) -> McpReconcileContext:
        return McpReconcileContext(
            paths=self.paths,
            catalog=catalog or self.catalog,
            state_store=McpStateStore(self.paths.mcp_state_file),
            fault=fault,
        )

    def destinations(self) -> dict[str, Path]:
        return {
            "claude": self.root / ".claude.json",
            "codex": self.root / ".codex" / "config.toml",
            "cursor": self.root / ".cursor" / "mcp.json",
        }

    def write_manual_configs(self) -> dict[Path, tuple[bytes, int]]:
        sources = {
            "claude": '{"mcpServers":{"manual":{"command":"one"}}}\n',
            "codex": '[mcp_servers.manual]\ncommand = "two"\n',
            "cursor": '{"mcpServers":{"manual":{"command":"three"}}}\n',
        }
        originals = {}
        for index, (client, path) in enumerate(self.destinations().items()):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sources[client], encoding="utf-8")
            mode = 0o640 - index * 0o20
            os.chmod(path, mode)
            originals[path] = (path.read_bytes(), mode)
        return originals

    def test_absent_plan_is_sorted_and_read_only(self) -> None:
        plan = plan_mcp(self.context, ("github", "docs"), ("cursor", "claude"))
        self.assertTrue(plan.can_apply)
        self.assertEqual(
            [("claude", "docs"), ("claude", "github"), ("cursor", "docs"), ("cursor", "github")],
            [(item.client, item.catalog_id) for item in plan.items],
        )
        self.assertEqual({"absent"}, {item.state for item in plan.items})
        self.assertFalse(self.paths.mcp_backup_home.exists())
        self.assertFalse(self.paths.mcp_state_file.exists())
        self.assertTrue(all(not path.exists() for path in self.destinations().values()))

    def test_unmanaged_same_name_blocks_every_target(self) -> None:
        path = self.destinations()["claude"]
        path.write_text(
            '{"mcpServers":{"agentbot_github":{"url":"https://manual.test"}}}',
            encoding="utf-8",
        )
        plan = plan_mcp(self.context, ("github",), ("claude", "codex"))
        self.assertFalse(plan.can_apply)
        self.assertIn("unmanaged-conflict", {item.state for item in plan.items})
        self.assertFalse(self.paths.mcp_backup_home.exists())

    def test_apply_then_plan_classifies_owned_and_manual_change_as_drift(self) -> None:
        plan = plan_mcp(self.context, ("github",), ("claude",))
        apply_mcp_plan(self.context, plan, operation_id=OPERATION_ID, now=NOW)
        owned = plan_mcp(self.context, ("github",), ("claude",))
        self.assertEqual("owned", owned.items[0].state)
        path = self.destinations()["claude"]
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcpServers"]["agentbot_github"]["url"] = "https://changed.test"
        path.write_text(json.dumps(document), encoding="utf-8")
        drifted = plan_mcp(self.context, ("github",), ("claude",))
        self.assertFalse(drifted.can_apply)
        self.assertEqual("managed-drift", drifted.items[0].state)

    def test_empty_unknown_and_ineligible_selections_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "select at least one MCP server"):
            plan_mcp(self.context, (), ("claude",))
        with self.assertRaisesRegex(ValueError, "unknown MCP catalog id: missing"):
            plan_mcp(self.context, ("missing",), ("claude",))
        blocked = McpCatalog(version=1, entries=(catalog_entry("blocked", eligible=False),))
        with self.assertRaisesRegex(ValueError, "MCP catalog entry is not eligible: blocked"):
            plan_mcp(self.make_context(catalog=blocked), ("blocked",), ("claude",))

    def test_failure_at_each_transaction_stage_restores_all_originals(self) -> None:
        stages = (
            "write:claude",
            "write:codex",
            "write:cursor",
            "post-parse:claude",
            "post-parse:codex",
            "post-parse:cursor",
            "state-write",
            "directory-fsync",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = AgentbotPaths(
                    root=root / "agentbot",
                    codex_home=root / ".codex",
                    claude_home=root / ".claude",
                    cursor_home=root / ".cursor",
                    config_home=root / ".config" / "agentbot",
                    agents_home=root / ".agents",
                )
                context = McpReconcileContext(
                    paths=paths,
                    catalog=McpCatalog(version=1, entries=(catalog_entry(),)),
                    state_store=McpStateStore(paths.mcp_state_file),
                    fault=lambda current, expected=stage: (
                        (_ for _ in ()).throw(RuntimeError(f"injected {expected}"))
                        if current == expected
                        else None
                    ),
                )
                destinations = {
                    "claude": root / ".claude.json",
                    "codex": root / ".codex" / "config.toml",
                    "cursor": root / ".cursor" / "mcp.json",
                }
                originals = {}
                for client, path in destinations.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        "{}\n" if client != "codex" else "# original\n", encoding="utf-8"
                    )
                    os.chmod(path, 0o640)
                    originals[path] = path.read_bytes()
                plan = plan_mcp(context, ("github",), ("claude", "codex", "cursor"))
                with self.assertRaisesRegex(RuntimeError, f"injected {stage}"):
                    apply_mcp_plan(context, plan, operation_id=OPERATION_ID, now=NOW)
                for path, original in originals.items():
                    self.assertEqual(original, path.read_bytes())
                    self.assertEqual(0o640, stat.S_IMODE(path.stat().st_mode))
                self.assertFalse(paths.mcp_state_file.exists())
                backup = paths.mcp_backup_home / OPERATION_ID
                self.assertTrue(backup.is_dir())
                self.assertEqual(0o700, stat.S_IMODE(backup.stat().st_mode))
                self.assertTrue(
                    all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in backup.iterdir())
                )

    def test_failed_apply_removes_destinations_that_were_initially_absent(self) -> None:
        context = self.make_context(
            fault=lambda stage: (
                (_ for _ in ()).throw(RuntimeError("injected")) if stage == "state-write" else None
            )
        )
        plan = plan_mcp(context, ("github",), ("claude", "codex", "cursor"))
        with self.assertRaisesRegex(RuntimeError, "injected"):
            apply_mcp_plan(context, plan, operation_id=OPERATION_ID, now=NOW)
        self.assertTrue(all(not path.exists() for path in self.destinations().values()))

    def test_restore_refuses_when_current_destination_changed(self) -> None:
        plan = plan_mcp(self.context, ("github",), ("claude",))
        apply_mcp_plan(self.context, plan, operation_id=OPERATION_ID, now=NOW)
        path = self.destinations()["claude"]
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "destination changed since operation"):
            restore_mcp_operation(self.context, OPERATION_ID)

    def test_restore_success_returns_all_original_bytes_and_modes(self) -> None:
        originals = self.write_manual_configs()
        plan = plan_mcp(self.context, ("github",), ("claude", "codex", "cursor"))
        apply_mcp_plan(self.context, plan, operation_id=OPERATION_ID, now=NOW)
        restore_mcp_operation(self.context, OPERATION_ID)
        for path, (content, mode) in originals.items():
            self.assertEqual(content, path.read_bytes())
            self.assertEqual(mode, stat.S_IMODE(path.stat().st_mode))
        self.assertFalse(self.paths.mcp_state_file.exists())

    def test_restore_rejects_manifest_destination_outside_managed_files(self) -> None:
        plan = plan_mcp(self.context, ("github",), ("claude",))
        apply_mcp_plan(self.context, plan, operation_id=OPERATION_ID, now=NOW)
        outside = self.root / "outside.txt"
        outside.write_text("do not remove", encoding="utf-8")
        manifest_path = self.paths.mcp_backup_home / OPERATION_ID / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0].update(
            {
                "path": str(outside),
                "original_present": False,
                "original_mode": None,
                "original_digest": None,
                "backup_name": None,
                "post_present": True,
                "post_digest": "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        with self.assertRaisesRegex(ValueError, "outside managed destinations"):
            restore_mcp_operation(self.context, OPERATION_ID)
        self.assertEqual("do not remove", outside.read_text(encoding="utf-8"))

    def test_off_plan_never_removes_an_unowned_entry(self) -> None:
        path = self.destinations()["claude"]
        path.write_text(
            '{"mcpServers":{"agentbot_github":{"url":"https://manual.test"}}}',
            encoding="utf-8",
        )
        plan = plan_mcp_off(self.context, ("github",), ("claude",))
        self.assertFalse(plan.can_apply)
        self.assertEqual("unmanaged-conflict", plan.items[0].state)

    def test_off_removes_owned_entry_and_preserves_manual_entry(self) -> None:
        path = self.destinations()["claude"]
        path.write_text('{"mcpServers":{"manual":{"command":"x"}}}', encoding="utf-8")
        apply_mcp_plan(
            self.context,
            plan_mcp(self.context, ("github",), ("claude",)),
            operation_id=OPERATION_ID,
            now=NOW,
        )
        off = plan_mcp_off(self.context, ("github",), ("claude",))
        apply_mcp_plan(
            self.context,
            off,
            operation_id="20260913T120001Z-1234abcd",
            now="2026-09-13T12:00:01Z",
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual({"manual": {"command": "x"}}, document["mcpServers"])
        self.assertEqual((), self.context.state_store.load().managed)


if __name__ == "__main__":
    unittest.main()
