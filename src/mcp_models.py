from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

_CLIENTS = frozenset({"claude", "codex", "cursor"})
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_PATTERN = re.compile(r"^agentbot_[a-z][a-z0-9_]*$")
_OPERATION_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_MANAGED_RECORD_KEYS = frozenset(
    {
        "catalog_id",
        "client",
        "scope",
        "destination",
        "name",
        "rendered_fingerprint",
        "catalog_version",
        "reviewed_on",
        "operation_id",
        "updated_at",
    }
)


@dataclass(frozen=True)
class McpCredential:
    mode: str
    environment: tuple[str, ...]


@dataclass(frozen=True)
class McpClientContract:
    client: str
    enabled: bool
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class McpCatalogEntry:
    id: str
    name: str
    publisher: str
    documentation_url: str
    license: str | None
    terms_url: str | None
    reviewed_on: str
    eligible: bool
    transport: str
    origin: str | None
    entrypoint: tuple[str, ...]
    clients: tuple[McpClientContract, ...]
    credential: McpCredential
    allowed_tools: tuple[str, ...]
    descriptor_fingerprints: tuple[str, ...]
    read_only_authority: str
    limitations: tuple[str, ...]
    timeout_seconds: int
    max_response_bytes: int
    doctor_rules: tuple[str, ...]


@dataclass(frozen=True)
class McpCatalog:
    version: int
    entries: tuple[McpCatalogEntry, ...]


@dataclass(frozen=True)
class McpManagedRecord:
    catalog_id: str
    client: str
    scope: str
    destination: str
    name: str
    rendered_fingerprint: str
    catalog_version: int
    reviewed_on: str
    operation_id: str
    updated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_id, str) or not _ID_PATTERN.fullmatch(self.catalog_id):
            raise ValueError("managed MCP catalog_id must use lower snake case")
        if self.client not in _CLIENTS:
            raise ValueError(f"unsupported managed MCP client: {self.client}")
        if self.scope != "user":
            raise ValueError(f"unsupported managed MCP scope: {self.scope}")
        if not isinstance(self.destination, str) or not self.destination:
            raise ValueError("managed MCP destination must be a non-empty string")
        destination = Path(self.destination).expanduser()
        resolved = destination.resolve(strict=False)
        if not destination.is_absolute() or str(destination) != str(resolved):
            raise ValueError("managed MCP destination must be an absolute canonical path")
        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError("managed MCP name must start with agentbot_")
        if not isinstance(self.rendered_fingerprint, str) or not _FINGERPRINT_PATTERN.fullmatch(
            self.rendered_fingerprint
        ):
            raise ValueError("managed MCP rendered_fingerprint must be a sha256 digest")
        if (
            isinstance(self.catalog_version, bool)
            or not isinstance(self.catalog_version, int)
            or self.catalog_version <= 0
        ):
            raise ValueError("managed MCP catalog_version must be a positive integer")
        _validate_date(self.reviewed_on, "managed MCP reviewed_on")
        if not isinstance(self.operation_id, str) or not _OPERATION_PATTERN.fullmatch(
            self.operation_id
        ):
            raise ValueError("managed MCP operation_id is invalid")
        _validate_timestamp(self.updated_at, "managed MCP updated_at")
        object.__setattr__(self, "destination", str(resolved))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.catalog_id, self.client, self.name)

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_id": self.catalog_id,
            "client": self.client,
            "scope": self.scope,
            "destination": self.destination,
            "name": self.name,
            "rendered_fingerprint": self.rendered_fingerprint,
            "catalog_version": self.catalog_version,
            "reviewed_on": self.reviewed_on,
            "operation_id": self.operation_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> McpManagedRecord:
        if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
            raise ValueError("managed MCP record must be an object")
        unknown = sorted(set(raw) - _MANAGED_RECORD_KEYS)
        if unknown:
            raise ValueError(f"unknown managed MCP key: {unknown[0]}")
        missing = sorted(_MANAGED_RECORD_KEYS - set(raw))
        if missing:
            raise ValueError(f"missing managed MCP key: {missing[0]}")
        try:
            return cls(**{key: raw[key] for key in _MANAGED_RECORD_KEYS})
        except TypeError as error:
            raise ValueError(f"invalid managed MCP record: {error}") from error


@dataclass(frozen=True)
class McpState:
    version: int
    managed: tuple[McpManagedRecord, ...]


@dataclass(frozen=True)
class McpPlanItem:
    action: Literal["setup", "off"]
    catalog_id: str
    client: str
    destination: str
    name: str
    state: Literal["absent", "owned", "unmanaged-conflict", "managed-drift"]


@dataclass(frozen=True)
class McpPlan:
    action: Literal["setup", "off"]
    selection: tuple[str, ...]
    targets: tuple[str, ...]
    items: tuple[McpPlanItem, ...]

    @property
    def can_apply(self) -> bool:
        return all(item.state in {"absent", "owned"} for item in self.items)


def _validate_date(raw: object, label: str) -> None:
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be an ISO date")
    try:
        date.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO date") from error


def _validate_timestamp(raw: object, label: str) -> None:
    if not isinstance(raw, str) or not raw.endswith("Z"):
        raise ValueError(f"{label} must be an ISO UTC timestamp")
    try:
        datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO UTC timestamp") from error
