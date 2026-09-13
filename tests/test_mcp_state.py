from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.mcp_models import McpManagedRecord, McpState
from src.mcp_state import McpStateStore


class McpStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "config" / "agentbot" / "mcp.json"
        self.store = McpStateStore(self.path)

    def record(self, catalog_id: str = "docs", client: str = "claude") -> McpManagedRecord:
        return McpManagedRecord(
            catalog_id=catalog_id,
            client=client,
            scope="user",
            destination=str(self.root / f".{client}" / "config.json"),
            name=f"agentbot_{catalog_id}",
            rendered_fingerprint="sha256:" + "a" * 64,
            catalog_version=1,
            reviewed_on="2026-09-13",
            operation_id="20260913T120000Z-1234abcd",
            updated_at="2026-09-13T12:00:00Z",
        )

    def write_raw(self, value: object, *, parent_mode: int = 0o700, file_mode: int = 0o600) -> None:
        self.path.parent.mkdir(parents=True)
        os.chmod(self.path.parent, parent_mode)
        self.path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.path, file_mode)

    def test_missing_state_means_no_managed_entry(self) -> None:
        self.assertEqual(McpState(version=1, managed=()), self.store.load())

    def test_replace_round_trips_in_stable_order(self) -> None:
        later = self.record("zeta", "cursor")
        earlier = self.record("alpha", "claude")
        self.store.replace(McpState(version=1, managed=(later, earlier)))
        self.assertEqual((earlier, later), self.store.load().managed)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(["alpha", "zeta"], [item["catalog_id"] for item in stored["managed"]])

    def test_duplicate_record_key_fails_closed(self) -> None:
        record = self.record()
        with self.assertRaisesRegex(ValueError, "duplicate managed MCP key"):
            self.store.replace(McpState(version=1, managed=(record, record)))

    def test_unsupported_version_fails_closed(self) -> None:
        self.write_raw({"version": 2, "managed": []})
        with self.assertRaisesRegex(ValueError, "unsupported MCP state version: 2"):
            self.store.load()

    def test_unknown_key_and_fake_secret_fail_closed(self) -> None:
        raw = self.record().to_dict()
        raw["token"] = "fake-secret"
        self.write_raw({"version": 1, "managed": [raw]})
        with self.assertRaisesRegex(ValueError, "unknown managed MCP key: token"):
            self.store.load()

    def test_state_file_symlink_is_rejected(self) -> None:
        self.path.parent.mkdir(parents=True)
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "MCP state file must not be a symlink"):
            self.store.load()

    def test_unsafe_parent_mode_is_rejected_on_load(self) -> None:
        self.write_raw({"version": 1, "managed": []}, parent_mode=0o755)
        with self.assertRaisesRegex(ValueError, "MCP state directory must have mode 700"):
            self.store.load()

    def test_unsafe_file_mode_is_rejected_on_load(self) -> None:
        self.write_raw({"version": 1, "managed": []}, file_mode=0o644)
        with self.assertRaisesRegex(ValueError, "MCP state file must have mode 600"):
            self.store.load()

    def test_state_file_is_private(self) -> None:
        self.store.replace(McpState(version=1, managed=()))
        self.assertEqual(0o700, stat.S_IMODE(self.path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_failed_atomic_replace_preserves_original_and_removes_temporary(self) -> None:
        self.store.replace(McpState(version=1, managed=()))
        original = self.path.read_bytes()
        with patch("src.mcp_state.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(OSError, "injected"):
                self.store.replace(McpState(version=1, managed=(self.record(),)))
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([], list(self.path.parent.glob(".mcp.json.agentbot-*")))


if __name__ == "__main__":
    unittest.main()
