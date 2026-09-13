from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .mcp_models import McpManagedRecord, McpState

MCP_STATE_VERSION = 1
_STATE_KEYS = frozenset({"version", "managed"})


class McpStateStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = Path(state_file).expanduser()

    def load(self) -> McpState:
        if self.state_file.is_symlink():
            raise ValueError("MCP state file must not be a symlink")
        if not self.state_file.exists():
            return McpState(version=MCP_STATE_VERSION, managed=())
        self._validate_parent()
        self._validate_existing_file()
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid MCP state: {self.state_file}: {error}") from error
        return self._decode(raw)

    def replace(self, state: McpState) -> None:
        if not isinstance(state, McpState):
            raise ValueError("MCP state must be an MCP state object")
        if isinstance(state.version, bool) or state.version != MCP_STATE_VERSION:
            raise ValueError(f"unsupported MCP state version: {state.version}")
        managed = self._ordered(state.managed)
        self._prepare_parent()
        self._validate_existing_file()
        encoded = (
            json.dumps(
                {
                    "version": MCP_STATE_VERSION,
                    "managed": [record.to_dict() for record in managed],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".mcp.json.agentbot-",
            dir=self.state_file.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
            directory_fd = os.open(self.state_file.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _decode(self, raw: Any) -> McpState:
        if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
            raise ValueError("invalid MCP state: expected an object")
        unknown = sorted(set(raw) - _STATE_KEYS)
        if unknown:
            raise ValueError(f"unknown MCP state key: {unknown[0]}")
        missing = sorted(_STATE_KEYS - set(raw))
        if missing:
            raise ValueError(f"missing MCP state key: {missing[0]}")
        version = raw["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("MCP state version must be an integer")
        if version != MCP_STATE_VERSION:
            raise ValueError(f"unsupported MCP state version: {version}")
        managed = raw["managed"]
        if not isinstance(managed, list):
            raise ValueError("managed MCP state must be an array")
        records = tuple(McpManagedRecord.from_dict(value) for value in managed)
        return McpState(version=version, managed=self._ordered(records))

    def _ordered(self, records: tuple[McpManagedRecord, ...]) -> tuple[McpManagedRecord, ...]:
        by_key: dict[tuple[str, str, str], McpManagedRecord] = {}
        for record in records:
            if not isinstance(record, McpManagedRecord):
                raise ValueError("managed MCP state contains an invalid record")
            if record.key in by_key:
                raise ValueError("duplicate managed MCP key: " + "/".join(record.key))
            by_key[record.key] = record
        return tuple(by_key[key] for key in sorted(by_key))

    def _prepare_parent(self) -> None:
        parent = self.state_file.parent
        if parent.is_symlink():
            raise ValueError(f"MCP state directory must not be a symlink: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise ValueError(f"MCP state path is not a directory: {parent}")
        os.chmod(parent, 0o700)

    def _validate_parent(self) -> None:
        parent = self.state_file.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"MCP state directory is unsafe: {parent}")
        if stat.S_IMODE(parent.stat().st_mode) != 0o700:
            raise ValueError(f"MCP state directory must have mode 700: {parent}")

    def _validate_existing_file(self) -> None:
        if not self.state_file.exists():
            return
        if self.state_file.is_symlink():
            raise ValueError("MCP state file must not be a symlink")
        if not self.state_file.is_file():
            raise ValueError(f"MCP state path is not a regular file: {self.state_file}")
        if stat.S_IMODE(self.state_file.stat().st_mode) != 0o600:
            raise ValueError(f"MCP state file must have mode 600: {self.state_file}")
