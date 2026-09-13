from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .mcp_models import McpCatalog, McpCatalogEntry, McpClientContract, McpCredential

_CATALOG_KEYS = frozenset({"version", "entries"})
_ENTRY_KEYS = frozenset(
    {
        "id",
        "name",
        "publisher",
        "documentation_url",
        "license",
        "terms_url",
        "reviewed_on",
        "eligible",
        "transport",
        "origin",
        "entrypoint",
        "clients",
        "credential",
        "allowed_tools",
        "descriptor_fingerprints",
        "read_only_authority",
        "limitations",
        "timeout_seconds",
        "max_response_bytes",
        "doctor_rules",
    }
)
_REQUIRED_ENTRY_KEYS = _ENTRY_KEYS - {"origin", "entrypoint"}
_CLIENT_KEYS = frozenset({"enabled", "url", "command", "args", "environment", "headers"})
_CREDENTIAL_KEYS = frozenset({"mode", "environment"})
_CLIENTS = ("claude", "codex", "cursor")
_TRANSPORTS = frozenset({"remote_http", "local_stdio"})
_CREDENTIAL_MODES = frozenset({"none", "bearer_env", "header_env", "oauth_native"})
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_PATTERN = re.compile(r"^agentbot_[a-z][a-z0-9_]*$")
_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_mcp_catalog(path: Path) -> McpCatalog:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read MCP catalog: {error}") from error
    data = _mapping(raw, "catalog")
    _reject_unknown(data, _CATALOG_KEYS, "catalog")
    _require_keys(data, _CATALOG_KEYS, "catalog")
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("catalog version must be an integer")
    if version != 1:
        raise ValueError(f"unsupported catalog version: {version}")
    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError("catalog entries must be an array")
    entries = tuple(_decode_entry(value) for value in raw_entries)
    _reject_duplicates((entry.id for entry in entries), "catalog id")
    _reject_duplicates((entry.name for entry in entries), "rendered name")
    return McpCatalog(version=version, entries=entries)


def _decode_entry(raw: object) -> McpCatalogEntry:
    data = _mapping(raw, "catalog entry")
    _reject_unknown(data, _ENTRY_KEYS, "catalog entry")
    _require_keys(data, _REQUIRED_ENTRY_KEYS, "catalog entry")

    entry_id = _string(data["id"], "id")
    if not _ID_PATTERN.fullmatch(entry_id):
        raise ValueError("id must use lower snake case")
    name = _string(data["name"], "name")
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError("name must start with agentbot_ and use lower snake case")
    publisher = _string(data["publisher"], "publisher")
    documentation_url = _https_url(data["documentation_url"], "documentation_url")
    license_name = _optional_string(data["license"], "license")
    terms_url = _optional_https_url(data["terms_url"], "terms_url")
    reviewed_on = _date_string(data["reviewed_on"])
    eligible = _boolean(data["eligible"], "eligible")
    transport = _string(data["transport"], "transport")
    if transport not in _TRANSPORTS:
        raise ValueError(f"unknown transport: {transport}")

    origin = _optional_https_url(data.get("origin"), "origin", remote_origin=True)
    entrypoint = _string_tuple(data.get("entrypoint", []), "entrypoint")
    if transport == "remote_http":
        if origin is None:
            raise ValueError("remote_http catalog entry must define origin")
        if entrypoint:
            raise ValueError("remote_http catalog entry must not define entrypoint")
    else:
        if not entrypoint:
            raise ValueError("local_stdio catalog entry must define entrypoint")
        if origin is not None:
            raise ValueError("local_stdio catalog entry must not define origin")

    clients = _decode_clients(data["clients"])
    if eligible and {contract.client for contract in clients} != set(_CLIENTS):
        raise ValueError(f"eligible catalog entry {entry_id} must define claude, codex, and cursor")
    if eligible and not all(contract.enabled for contract in clients):
        raise ValueError(f"eligible catalog entry {entry_id} must enable every client")

    credential = _decode_credential(data["credential"])
    allowed_tools = _unique_string_tuple(data["allowed_tools"], "allowed_tools")
    fingerprints = _unique_string_tuple(data["descriptor_fingerprints"], "descriptor_fingerprints")
    if any(not _FINGERPRINT_PATTERN.fullmatch(value) for value in fingerprints):
        raise ValueError(
            "descriptor fingerprint must be sha256 followed by 64 lowercase hex digits"
        )

    return McpCatalogEntry(
        id=entry_id,
        name=name,
        publisher=publisher,
        documentation_url=documentation_url,
        license=license_name,
        terms_url=terms_url,
        reviewed_on=reviewed_on,
        eligible=eligible,
        transport=transport,
        origin=origin,
        entrypoint=entrypoint,
        clients=clients,
        credential=credential,
        allowed_tools=allowed_tools,
        descriptor_fingerprints=fingerprints,
        read_only_authority=_string(data["read_only_authority"], "read_only_authority"),
        limitations=_string_tuple(data["limitations"], "limitations"),
        timeout_seconds=_positive_integer(data["timeout_seconds"], "timeout_seconds"),
        max_response_bytes=_positive_integer(data["max_response_bytes"], "max_response_bytes"),
        doctor_rules=_unique_string_tuple(data["doctor_rules"], "doctor_rules"),
    )


def _decode_clients(raw: object) -> tuple[McpClientContract, ...]:
    data = _mapping(raw, "clients")
    unknown = sorted(set(data) - set(_CLIENTS))
    if unknown:
        raise ValueError(f"unknown client: {unknown[0]}")
    contracts = []
    for client in _CLIENTS:
        if client not in data:
            continue
        value = _mapping(data[client], f"{client} client contract")
        _reject_unknown(value, _CLIENT_KEYS, f"{client} client contract")
        if "enabled" not in value:
            raise ValueError(f"missing {client} client contract key: enabled")
        contracts.append(
            McpClientContract(
                client=client,
                enabled=_boolean(value["enabled"], f"{client} enabled"),
                url=_optional_string(value.get("url"), f"{client} url"),
                command=_optional_string(value.get("command"), f"{client} command"),
                args=_string_tuple(value.get("args", []), f"{client} args"),
                environment=_string_mapping(value.get("environment", {}), f"{client} environment"),
                headers=_string_mapping(value.get("headers", {}), f"{client} headers"),
            )
        )
    return tuple(contracts)


def _decode_credential(raw: object) -> McpCredential:
    data = _mapping(raw, "credential")
    _reject_unknown(data, _CREDENTIAL_KEYS, "credential")
    _require_keys(data, _CREDENTIAL_KEYS, "credential")
    mode = _string(data["mode"], "credential mode")
    if mode not in _CREDENTIAL_MODES:
        raise ValueError(f"unknown credential mode: {mode}")
    environment = _unique_string_tuple(data["environment"], "credential environment")
    if any(not _ENV_PATTERN.fullmatch(value) for value in environment):
        raise ValueError("credential environment names must use upper snake case")
    if mode == "none" and environment:
        raise ValueError("credential mode none must not name environment variables")
    if mode in {"bearer_env", "header_env"} and not environment:
        raise ValueError(f"credential mode {mode} must name an environment variable")
    return McpCredential(mode=mode, environment=environment)


def _mapping(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be an object")
    return raw


def _reject_unknown(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown {label} key: {unknown[0]}")


def _require_keys(data: dict[str, Any], required: frozenset[str], label: str) -> None:
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"missing {label} key: {missing[0]}")


def _string(raw: object, label: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be a string")
    if not raw.strip():
        raise ValueError(f"{label} must not be blank")
    return raw


def _optional_string(raw: object, label: str) -> str | None:
    if raw is None:
        return None
    return _string(raw, label)


def _boolean(raw: object, label: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{label} must be a boolean")
    return raw


def _positive_integer(raw: object, label: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return raw


def _string_tuple(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be an array")
    return tuple(_string(value, f"{label} value") for value in raw)


def _unique_string_tuple(raw: object, label: str) -> tuple[str, ...]:
    values = _string_tuple(raw, label)
    _reject_duplicates(values, label.rstrip("s"))
    return values


def _string_mapping(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    data = _mapping(raw, label)
    return tuple(
        sorted(
            (_string(key, f"{label} key"), _string(value, f"{label} value"))
            for key, value in data.items()
        )
    )


def _date_string(raw: object) -> str:
    value = _string(raw, "reviewed_on")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("reviewed_on must be an ISO date") from error
    return value


def _https_url(raw: object, label: str) -> str:
    value = _string(raw, label)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        if label == "origin":
            raise ValueError("remote origin must use https")
        raise ValueError(f"{label} must be an https URL without user information")
    return value


def _optional_https_url(raw: object, label: str, *, remote_origin: bool = False) -> str | None:
    if raw is None:
        return None
    try:
        return _https_url(raw, label)
    except ValueError as error:
        if remote_origin:
            raise ValueError("remote origin must use https") from error
        raise


def _reject_duplicates(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)
