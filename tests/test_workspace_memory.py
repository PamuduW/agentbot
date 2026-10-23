"""Boot enrollment and update ownership use real, disposable repositories."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src import memory, workspace_memory
from src.atomic_io import write_text_atomic
from src.paths import AgentbotPaths
from src.workspace_service import WorkspaceService
from tests.test_memory_project_writes import WritesTestCase


class WorkspaceMemoryTests(WritesTestCase):
    def setUp(self):
        super().setUp()
        self.paths = AgentbotPaths(
            root=Path(__file__).resolve().parents[1], codex_home=self.tmp / "home/.codex",
            claude_home=self.tmp / "home/.claude", cursor_home=self.tmp / "home/.cursor",
            config_home=self.tmp / "settings/agentbot", agents_home=self.tmp / "home/.agents",
        )
        self.service = WorkspaceService(self.paths)
        self.service.apply(self.code, profile_name=None, targets=("agents",), register=True)

    def prepare(self, clients=("cursor",), previous=None, paths=None):
        return workspace_memory.prepare(
            self.a, self.code, paths or self.paths, clients=clients, previous=previous or {},
        )

    def apply(self, plan):
        record = self.service.store.load()[0]
        workspace_memory.apply(plan, self.service.store, record)
        return self.service.store.load()[0]

    def test_discovery_uses_executables_in_current_path_not_machine_or_editor(self):
        binaries = self.tmp / "binaries"
        binaries.mkdir()
        for name in ("claude", "cursor"):
            (binaries / name).write_text("#!/bin/sh\nexit 0\n")
            (binaries / name).chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(binaries)}):
            self.assertEqual(("claude",), workspace_memory.discover_clients())
            (binaries / "cursor-agent").write_text("#!/bin/sh\nexit 0\n")
            (binaries / "cursor-agent").chmod(0o755)
            self.assertEqual(("claude", "cursor"), workspace_memory.discover_clients())

    def test_preview_then_apply_enrolls_and_locally_excludes_machine_files(self):
        plan = self.prepare()
        self.assertFalse((self.code / ".cursor").exists())
        self.assertFalse(self.service.store.load()[0].memory_bindings)
        record = self.apply(plan)
        self.assertEqual({"cursor"}, set(record.memory_bindings))
        for config in plan.configs:
            self.assertTrue(config.path.exists())
            ignored = subprocess.run(
                ["git", "-C", str(self.code), "check-ignore", str(config.path)],
                capture_output=True, timeout=10,
            )
            self.assertEqual(0, ignored.returncode)
        repeat = self.prepare(previous=record.memory_bindings)
        self.assertTrue(all(config.same for config in repeat.configs))
        self.assertEqual(record.memory_bindings, repeat.bindings)

    def test_late_conflict_prevents_all_earlier_surface_writes(self):
        path = self.code / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text('{"mcpServers":{"agentbot_memory":{"command":"personal"}}}')
        before = path.read_bytes()
        with self.assertRaises(memory.MemoryVaultError):
            self.prepare(clients=("claude", "cursor"))
        self.assertFalse((self.code / ".claude").exists())
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.service.store.load()[0].memory_bindings)

    def test_tracked_config_is_a_conflict_not_an_edit_or_an_ineffective_ignore(self):
        path = self.code / ".cursor/hooks.json"
        path.parent.mkdir()
        path.write_text("{}\n")
        subprocess.run(["git", "-C", str(self.code), "add", str(path)], check=True)
        with self.assertRaisesRegex(memory.MemoryVaultError, "tracked"):
            self.prepare()
        self.assertEqual("{}\n", path.read_text())

    def test_changed_backup_symlink_after_preview_is_refused_without_writing(self):
        plan = self.prepare()
        plan.configs[0].path.parent.mkdir(parents=True)
        target = self.tmp / "unrelated"
        target.write_text("leave alone")
        backup = plan.configs[0].path.with_name(plan.configs[0].path.name + ".agentbot-backup")
        backup.symlink_to(target)
        read_bytes = Path.read_bytes
        def guarded_read(path):
            if path == backup:
                raise AssertionError("snapshot followed an unowned backup symlink")
            return read_bytes(path)
        with patch.object(Path, "read_bytes", guarded_read), self.assertRaises(memory.MemoryVaultError):
            self.apply(plan)
        self.assertEqual("leave alone", target.read_text())
        self.assertTrue(backup.is_symlink())
        self.assertFalse(plan.configs[0].path.exists())

    def test_changed_configuration_after_preview_is_preserved(self):
        plan = self.prepare()
        path = plan.configs[-1].path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"personal": true}')
        with self.assertRaises(memory.MemoryVaultError):
            self.apply(plan)
        self.assertEqual('{"personal": true}', path.read_text())
        self.assertFalse(plan.configs[0].path.exists())

    def test_rollback_preserves_a_later_file_edited_during_an_earlier_write(self):
        plan = self.prepare()
        later = plan.configs[-1].path
        from src.memory_session_config import apply_prepared
        def race(config):
            apply_prepared(config)
            if config == plan.configs[0]:
                later.write_text('{"concurrent": true}')
        with patch("src.workspace_memory.memory_session_config.apply_prepared", side_effect=race):
            with self.assertRaises(memory.MemoryVaultError):
                self.apply(plan)
        self.assertEqual('{"concurrent": true}', later.read_text())
        self.assertFalse(plan.configs[0].path.exists())

    def test_tracked_backup_prevents_all_integration_writes(self):
        path = self.code / ".cursor/hooks.json"
        path.parent.mkdir()
        path.write_text("{}\n")
        backup = path.with_name(path.name + ".agentbot-backup")
        backup.write_text("personal tracked backup")
        subprocess.run(["git", "-C", str(self.code), "add", str(backup)], check=True)
        with self.assertRaisesRegex(memory.MemoryVaultError, "tracked"):
            self.prepare()
        self.assertEqual("personal tracked backup", backup.read_text())

    def test_config_tracked_after_preview_is_not_modified(self):
        path = self.code / ".cursor/hooks.json"
        path.parent.mkdir()
        path.write_text("{}\n")
        plan = self.prepare()
        subprocess.run(["git", "-C", str(self.code), "add", str(path)], check=True)
        with self.assertRaisesRegex(memory.MemoryVaultError, "tracked"):
            self.apply(plan)
        self.assertEqual("{}\n", path.read_text())

    def test_update_snapshot_covers_configs_backups_excludes_and_registry(self):
        from types import SimpleNamespace

        from src.lifecycle import Lifecycle

        self.apply(self.prepare())
        with patch.dict(os.environ, {"AGENTBOT_MEMORY_DIR": str(self.a)}):
            report = self.service.resync(apply=False)
            paths = Lifecycle(self.paths)._update_transaction_paths(SimpleNamespace(workspace_report=report))
        for relative in (".cursor/hooks.json", ".cursor/hooks.json.agentbot-backup",
                         ".cursor/mcp.json", ".cursor/mcp.json.agentbot-backup", ".git/info/exclude"):
            self.assertIn(self.code / relative, paths)
        self.assertIn(self.paths.workspace_state_file, paths)

    def test_symlinked_git_info_is_refused_without_touching_external_excludes(self):
        info = self.code / ".git/info"
        outside = self.tmp / "external-info"
        info.rename(outside)
        info.symlink_to(outside, target_is_directory=True)
        before = (outside / "exclude").read_bytes()
        with self.assertRaises(memory.MemoryVaultError):
            self.prepare()
        self.assertEqual(before, (outside / "exclude").read_bytes())

    def test_linked_worktree_uses_common_local_excludes(self):
        linked = self.tmp / "linked"
        from tests.test_memory_sync import git

        git(self.code, "commit", "--allow-empty", "-qm", "Synthetic worktree fixture")
        subprocess.run(["git", "-C", str(self.code), "worktree", "add", "--detach", str(linked)],
                       check=True, capture_output=True)
        plan = workspace_memory.prepare(self.a, linked, self.paths, clients=("cursor",), previous={})
        self.assertEqual(self.code / ".git/info/exclude", plan.exclude)

    def test_late_write_failure_restores_config_backups_excludes_and_registry(self):
        plan = self.prepare(clients=("claude", "cursor"))
        before = self.paths.workspace_state_file.read_bytes()
        calls = 0
        def fail_once(path, text, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic disk failure")
            return write_text_atomic(path, text, **kwargs)
        with patch("src.memory_session_config.write_text_atomic", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertEqual(before, self.paths.workspace_state_file.read_bytes())
        self.assertTrue(all(not config.path.exists() for config in plan.configs))
        self.assertTrue(all(not config.path.with_name(config.path.name + ".agentbot-backup").exists()
                            for config in plan.configs))

    def test_resync_refreshes_saved_bindings_without_enrolling_new_clients(self):
        record = self.apply(self.prepare())
        path = self.code / ".cursor/hooks.json"
        data = json.loads(path.read_text())
        data["personal"] = {"keep": True}
        path.write_text(json.dumps(data))
        alternate = self.tmp / "moved-product"
        (alternate / "bin").mkdir(parents=True)
        (alternate / "bin/agentbot").touch()
        plan = self.prepare(previous=record.memory_bindings, paths=replace(self.paths, root=alternate))
        self.apply(plan)
        self.assertTrue(json.loads(path.read_text())["personal"]["keep"])
        self.assertIn(str(alternate), path.read_text())
        self.assertFalse((self.code / ".claude").exists())

    def test_real_resync_restores_missing_memory_surface_and_preserves_enrollment(self):
        record = self.apply(self.prepare())
        path = self.code / ".cursor/mcp.json"
        path.unlink()
        with patch.dict(os.environ, {"AGENTBOT_MEMORY_DIR": str(self.a)}):
            preview = self.service.resync(apply=False)
            self.assertFalse(path.exists())
            self.assertTrue(any(action.relative_path == ".cursor/mcp.json"
                                for result in preview.results for action in result.actions))
            report = self.service.resync(apply=True)
        self.assertTrue(path.exists())
        self.assertTrue(all(result.status == "applied" for result in report.results))
        self.assertEqual(record.memory_bindings, self.service.store.load()[0].memory_bindings)
        self.service.apply(self.code, profile_name=None, targets=("agents",), register=True)
        self.assertEqual(record.memory_bindings, self.service.store.load()[0].memory_bindings)
