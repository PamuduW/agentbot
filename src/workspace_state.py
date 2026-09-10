from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

WORKSPACE_STATE_VERSION = 1
WORKSPACE_TARGETS = frozenset({"agents", "claude", "cursor"})
# Targets that used to be supported. A stored record naming one is migrated on
# load rather than rejected: refusing would make the whole registry unreadable
# and take `resync --all` down with it.
RETIRED_WORKSPACE_TARGETS = frozenset({"copilot"})
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{1,40}$")


@dataclass(frozen=True)
class WorkspaceRecord:
    path: str
    kind: Literal["git", "directory"]
    policy_mode: Literal["managed", "custom"]
    profile: str
    targets: tuple[str, ...]
    enabled: bool
    last_commit: str | None
    last_rendered_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("workspace path must be a non-empty string")
        path = Path(self.path).expanduser()
        resolved = path.resolve(strict=False)
        if not path.is_absolute() or str(path) != str(resolved):
            raise ValueError("workspace path must be an absolute canonical path")
        if self.kind not in {"git", "directory"}:
            raise ValueError(f"invalid workspace kind: {self.kind}")
        if self.policy_mode not in {"managed", "custom"}:
            raise ValueError(f"invalid workspace policy mode: {self.policy_mode}")
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("workspace profile must be a non-empty string")
        if not isinstance(self.enabled, bool):
            raise ValueError("workspace enabled must be a boolean")

        targets = tuple(self.targets)
        if not targets:
            raise ValueError("workspace targets must not be empty")
        if len(set(targets)) != len(targets):
            raise ValueError("workspace targets must be unique")
        if any(target not in WORKSPACE_TARGETS for target in targets):
            raise ValueError("workspace targets contain an unsupported target")
        if "agents" not in targets:
            raise ValueError("workspace targets must include agents")

        if self.last_commit is not None:
            if self.kind == "directory":
                raise ValueError("directory workspace last_commit must be null")
            if not isinstance(self.last_commit, str) or not COMMIT_PATTERN.fullmatch(self.last_commit):
                raise ValueError("workspace last_commit must be a short hexadecimal commit")
        if self.last_rendered_at is not None and (
            not isinstance(self.last_rendered_at, str) or not self.last_rendered_at.strip()
        ):
            raise ValueError("workspace last_rendered_at must be a non-empty string or null")

        object.__setattr__(self, "path", str(resolved))
        object.__setattr__(self, "targets", targets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "policy_mode": self.policy_mode,
            "profile": self.profile,
            "targets": list(self.targets),
            "enabled": self.enabled,
            "last_commit": self.last_commit,
            "last_rendered_at": self.last_rendered_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WorkspaceRecord:
        if not isinstance(raw, dict):
            raise ValueError("workspace record must be a mapping")
        try:
            return cls(
                path=raw["path"],
                kind=raw["kind"],
                policy_mode=raw["policy_mode"],
                profile=raw["profile"],
                targets=tuple(raw["targets"]),
                enabled=raw["enabled"],
                last_commit=raw.get("last_commit"),
                last_rendered_at=raw.get("last_rendered_at"),
            )
        except (KeyError, TypeError) as error:
            raise ValueError(f"invalid workspace record: {error}") from error


class WorkspaceStore:
    def __init__(self, state_file: Path) -> None:
        self.state_file = Path(state_file).expanduser()

    def load(self) -> tuple[WorkspaceRecord, ...]:
        if self.state_file.is_symlink():
            raise ValueError("workspace state file must not be a symlink")
        if not self.state_file.exists():
            return ()
        self._validate_parent()
        self._validate_existing_state_file()
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise ValueError(f"invalid workspace state: {self.state_file}: {error}") from error
        return self._decode(raw)

    def upsert(self, record: WorkspaceRecord) -> tuple[WorkspaceRecord, ...]:
        records = {item.path: item for item in self.load()}
        records[record.path] = record
        ordered = tuple(records[path] for path in sorted(records))
        self.replace(ordered)
        return ordered

    def remove(self, path: Path) -> WorkspaceRecord | None:
        canonical = str(Path(path).expanduser().resolve(strict=False))
        records = self.load()
        removed = next((record for record in records if record.path == canonical), None)
        if removed is None:
            return None
        self.replace(record for record in records if record.path != canonical)
        return removed

    def replace(self, records: Iterable[WorkspaceRecord]) -> None:
        normalized = tuple(records)
        by_path: dict[str, WorkspaceRecord] = {}
        for record in normalized:
            if record.path in by_path:
                raise ValueError(f"duplicate workspace path: {record.path}")
            by_path[record.path] = record
        ordered = tuple(by_path[path] for path in sorted(by_path))

        self._prepare_parent()
        self._validate_existing_state_file()
        payload = {
            "version": WORKSPACE_STATE_VERSION,
            "workspaces": [record.to_dict() for record in ordered],
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        fd, temporary_name = tempfile.mkstemp(
            prefix=".workspaces.json.agentbot-",
            dir=self.state_file.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
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
            if temporary.exists():
                temporary.unlink()

    def _decode(self, raw: Any) -> tuple[WorkspaceRecord, ...]:
        if not isinstance(raw, dict) or raw.get("version") != WORKSPACE_STATE_VERSION:
            raise ValueError(f"invalid workspace state: unsupported version in {self.state_file}")
        workspaces = raw.get("workspaces")
        if not isinstance(workspaces, list):
            raise ValueError("invalid workspace state: workspaces must be a list")
        records = tuple(_migrate_retired_targets(item) for item in workspaces)
        records = tuple(WorkspaceRecord.from_dict(item) for item in records)
        if len({record.path for record in records}) != len(records):
            raise ValueError("invalid workspace state: duplicate workspace path")
        return tuple(sorted(records, key=lambda record: record.path))

    def _prepare_parent(self) -> None:
        parent = self.state_file.parent
        if parent.is_symlink():
            raise ValueError(f"workspace state directory must not be a symlink: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise ValueError(f"workspace state path is not a directory: {parent}")
        os.chmod(parent, 0o700)

    def _validate_parent(self) -> None:
        parent = self.state_file.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"workspace state directory is unsafe: {parent}")
        if stat.S_IMODE(parent.stat().st_mode) != 0o700:
            raise ValueError(f"workspace state directory must have mode 700: {parent}")

    def _validate_existing_state_file(self) -> None:
        if not self.state_file.exists():
            return
        if self.state_file.is_symlink():
            raise ValueError("workspace state file must not be a symlink")
        if not self.state_file.is_file():
            raise ValueError(f"workspace state path is not a regular file: {self.state_file}")
        if stat.S_IMODE(self.state_file.stat().st_mode) != 0o600:
            raise ValueError(f"workspace state file must have mode 600: {self.state_file}")


def _migrate_retired_targets(item: Any) -> Any:
    """Drop retired targets from a stored record before it is validated."""
    if not isinstance(item, dict):
        return item
    targets = item.get("targets")
    if not isinstance(targets, list):
        return item
    kept = [target for target in targets if target not in RETIRED_WORKSPACE_TARGETS]
    if len(kept) == len(targets):
        return item
    migrated = dict(item)
    # "agents" is always rendered, so a record cannot end up empty.
    migrated["targets"] = kept or ["agents"]
    return migrated
