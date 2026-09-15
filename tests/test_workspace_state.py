from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.workspace_state import WorkspaceRecord, WorkspaceStore


class WorkspaceStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_file = self.root / "config" / "workspaces.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_then_load_round_trips_sorted_records(self) -> None:
        store = WorkspaceStore(self.state_file)
        store.upsert(
            WorkspaceRecord(
                path="/work/b",
                kind="directory",
                policy_mode="custom",
                profile="safe-default",
                targets=("agents", "claude"),
                enabled=True,
                last_commit=None,
                last_rendered_at="2026-07-18T00:00:00Z",
            )
        )
        store.upsert(
            WorkspaceRecord(
                path="/work/a",
                kind="git",
                policy_mode="managed",
                profile="safe-default",
                targets=("agents", "claude", "cursor"),
                enabled=True,
                last_commit="a1b2c3d",
                last_rendered_at="2026-07-18T00:00:00Z",
            )
        )

        self.assertEqual(["/work/a", "/work/b"], [item.path for item in store.load()])
        self.assertEqual(0o700, stat.S_IMODE(self.state_file.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.state_file.stat().st_mode))

    def test_malformed_state_fails_without_replacing_the_file(self) -> None:
        self.state_file.parent.mkdir(parents=True)
        os.chmod(self.state_file.parent, 0o700)
        self.state_file.write_text("not json", encoding="utf-8")
        os.chmod(self.state_file, 0o600)

        with self.assertRaisesRegex(ValueError, "invalid workspace state"):
            WorkspaceStore(self.state_file).load()
        self.assertEqual("not json", self.state_file.read_text(encoding="utf-8"))

    def test_state_file_symlink_is_rejected(self) -> None:
        self.state_file.parent.mkdir(parents=True)
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o600)
        self.state_file.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "state file must not be a symlink"):
            WorkspaceStore(self.state_file).load()

    def test_invalid_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "targets must include agents"):
            WorkspaceRecord(
                path="/work/project",
                kind="directory",
                policy_mode="managed",
                profile="safe-default",
                targets=("claude",),
                enabled=True,
                last_commit=None,
                last_rendered_at=None,
            )

    def test_json_state_is_versioned_and_deterministic(self) -> None:
        store = WorkspaceStore(self.state_file)
        record = WorkspaceRecord(
            path="/work/project",
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        store.replace([record])

        document = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(1, document["version"])
        self.assertEqual(["agents"], document["workspaces"][0]["targets"])

    def test_remove_returns_record_and_preserves_other_records(self) -> None:
        store = WorkspaceStore(self.state_file)
        first = WorkspaceRecord(
            path="/work/a",
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        second = WorkspaceRecord(
            path="/missing/b",
            kind="directory",
            policy_mode="custom",
            profile="safe-default",
            targets=("agents", "claude"),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        store.replace((first, second))

        removed = store.remove(Path("/missing/b"))

        self.assertEqual(second, removed)
        self.assertEqual((first,), store.load())
        self.assertEqual(0o600, stat.S_IMODE(self.state_file.stat().st_mode))

    def test_remove_unknown_path_does_not_rewrite_state(self) -> None:
        store = WorkspaceStore(self.state_file)
        record = WorkspaceRecord(
            path="/work/a",
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        store.replace((record,))
        before = self.state_file.read_bytes()
        before_stat = self.state_file.stat()

        removed = store.remove(Path("/unregistered"))

        self.assertIsNone(removed)
        self.assertEqual(before, self.state_file.read_bytes())
        self.assertEqual(before_stat.st_ino, self.state_file.stat().st_ino)
        self.assertEqual(before_stat.st_mtime_ns, self.state_file.stat().st_mtime_ns)

    def test_remove_canonicalizes_missing_path_without_requiring_it_to_exist(self) -> None:
        store = WorkspaceStore(self.state_file)
        record = WorkspaceRecord(
            path="/work/a",
            kind="directory",
            policy_mode="managed",
            profile="safe-default",
            targets=("agents",),
            enabled=True,
            last_commit=None,
            last_rendered_at=None,
        )
        store.replace((record,))

        removed = store.remove(Path("/work/a/../a"))

        self.assertEqual(record, removed)
        self.assertEqual((), store.load())

    def test_a_stored_retired_target_is_migrated_not_rejected(self) -> None:
        # Dropping a target must not make the whole registry unreadable and take
        # `resync --all` down with it.
        from src.workspace_state import WorkspaceStore

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "workspaces.json"
            state.parent.chmod(0o700)
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "workspaces": [
                            {
                                "path": temporary,
                                "kind": "directory",
                                "policy_mode": "managed",
                                "profile": "safe-default",
                                "targets": ["agents", "copilot", "cursor"],
                                "enabled": True,
                                "last_commit": None,
                                "last_rendered_at": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)

            records = WorkspaceStore(state).load()

            self.assertEqual(1, len(records))
            self.assertEqual(("agents", "cursor"), records[0].targets)

    def test_a_record_of_only_retired_targets_falls_back_to_agents(self) -> None:
        from src.workspace_state import WorkspaceStore

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "workspaces.json"
            state.parent.chmod(0o700)
            state.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "workspaces": [
                            {
                                "path": temporary,
                                "kind": "directory",
                                "policy_mode": "managed",
                                "profile": "safe-default",
                                "targets": ["copilot"],
                                "enabled": True,
                                "last_commit": None,
                                "last_rendered_at": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state.chmod(0o600)

            records = WorkspaceStore(state).load()

            self.assertEqual(("agents",), records[0].targets)

    def test_copilot_is_not_a_supported_target(self) -> None:
        from src.workspace_state import RETIRED_WORKSPACE_TARGETS, WORKSPACE_TARGETS

        self.assertNotIn("copilot", WORKSPACE_TARGETS)
        self.assertIn("copilot", RETIRED_WORKSPACE_TARGETS)


if __name__ == "__main__":
    unittest.main()
