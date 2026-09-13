from __future__ import annotations

from dataclasses import dataclass


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
