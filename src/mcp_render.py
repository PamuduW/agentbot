from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import Table

from .mcp_models import McpCatalogEntry, McpClientContract

_CLIENTS = frozenset({"claude", "codex", "cursor"})
_ENV_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def parse_mcp_config(client: str, text: str) -> Any:
    _validate_client(client)
    if client == "codex":
        try:
            document = tomlkit.parse(text) if text.strip() else tomlkit.document()
        except Exception as error:
            raise ValueError(f"invalid codex MCP configuration: {error}") from error
        servers = document.get("mcp_servers")
        if servers is not None and not isinstance(servers, (Table, Container, dict)):
            raise ValueError("invalid codex MCP configuration: mcp_servers must be a table")
        return document

    if not text.strip():
        return {}
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"invalid {client} MCP configuration: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"invalid {client} MCP configuration: top level must be an object")
    servers = document.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ValueError(f"invalid {client} MCP configuration: mcpServers must be an object")
    return document


def render_mcp_config(client: str, text: str, entries: Sequence[McpCatalogEntry]) -> str:
    document = parse_mcp_config(client, text)
    names: set[str] = set()
    native_entries: list[tuple[str, dict[str, object]]] = []
    for entry in entries:
        if entry.name in names:
            raise ValueError(f"duplicate rendered MCP name: {entry.name}")
        names.add(entry.name)
        native_entries.append((entry.name, _native_entry(client, entry)))

    if client == "codex":
        servers = document.get("mcp_servers")
        if servers is None:
            servers = tomlkit.table()
            document["mcp_servers"] = servers
        for name, native in native_entries:
            server = tomlkit.table()
            for key, value in native.items():
                server.add(key, value)
            servers[name] = server
        return tomlkit.dumps(document)

    servers = document.setdefault("mcpServers", {})
    for name, native in native_entries:
        servers[name] = native
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def remove_owned_entry(client: str, text: str, name: str) -> str:
    if not isinstance(name, str) or not name.startswith("agentbot_"):
        raise ValueError("owned MCP name must start with agentbot_")
    document = parse_mcp_config(client, text)
    key = "mcp_servers" if client == "codex" else "mcpServers"
    servers = document.get(key)
    if servers is not None:
        servers.pop(name, None)
    if client == "codex":
        return tomlkit.dumps(document)
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def entry_fingerprint(entry: object) -> str:
    encoded = json.dumps(
        _plain(entry),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _native_entry(client: str, entry: McpCatalogEntry) -> dict[str, object]:
    if not entry.eligible:
        raise ValueError(f"MCP catalog entry is not eligible: {entry.id}")
    contract = _contract(entry, client)
    if not contract.enabled:
        raise ValueError(f"MCP catalog entry {entry.id} does not enable {client}")
    environment = _credential_environment(entry)

    if entry.transport == "remote_http":
        url = contract.url or entry.origin
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError(f"MCP catalog entry {entry.id} has no secure remote URL")
        native: dict[str, object] = {"url": url}
        if client != "codex":
            native["type"] = "http"
        _add_remote_auth(native, client, entry.credential.mode, environment)
        _add_header_references(native, client, contract.headers)
        return native

    if entry.transport != "local_stdio":
        raise ValueError(f"unsupported MCP transport: {entry.transport}")
    command = contract.command or (entry.entrypoint[0] if entry.entrypoint else None)
    if not command:
        raise ValueError(f"MCP catalog entry {entry.id} has no local command")
    args = contract.args or entry.entrypoint[1:]
    native = {"command": command}
    if args:
        native["args"] = list(args)
    referenced = tuple(dict.fromkeys((*environment, *(value for _, value in contract.environment))))
    _add_stdio_environment(native, client, referenced)
    return native


def _contract(entry: McpCatalogEntry, client: str) -> McpClientContract:
    matches = tuple(contract for contract in entry.clients if contract.client == client)
    if len(matches) != 1:
        raise ValueError(f"MCP catalog entry {entry.id} must define one {client} contract")
    return matches[0]


def _credential_environment(entry: McpCatalogEntry) -> tuple[str, ...]:
    environment = entry.credential.environment
    if any(not isinstance(name, str) or not _ENV_PATTERN.fullmatch(name) for name in environment):
        raise ValueError("credential environment name must use upper snake case")
    if entry.credential.mode in {"bearer_env", "header_env"} and not environment:
        raise ValueError(f"credential mode {entry.credential.mode} needs an environment name")
    if entry.credential.mode == "bearer_env" and len(environment) != 1:
        raise ValueError("bearer_env credential mode needs exactly one environment name")
    return environment


def _add_remote_auth(
    native: dict[str, object], client: str, mode: str, environment: tuple[str, ...]
) -> None:
    if mode in {"none", "oauth_native"}:
        return
    if mode == "bearer_env":
        name = environment[0]
        if client == "codex":
            native["bearer_token_env_var"] = name
        else:
            expression = f"${{env:{name}}}" if client == "cursor" else f"${{{name}}}"
            native["headers"] = {"Authorization": f"Bearer {expression}"}
        return
    if mode != "header_env":
        raise ValueError(f"unsupported credential mode: {mode}")


def _add_header_references(
    native: dict[str, object], client: str, headers: tuple[tuple[str, str], ...]
) -> None:
    if not headers:
        return
    native_key = "env_http_headers" if client == "codex" else "headers"
    existing = native.get(native_key)
    if existing is None:
        rendered_headers: dict[str, str] = {}
        native[native_key] = rendered_headers
    elif isinstance(existing, dict):
        rendered_headers = existing
    else:
        raise ValueError(f"native MCP {native_key} must be a mapping")
    for header, environment in headers:
        if not _ENV_PATTERN.fullmatch(environment):
            raise ValueError("header credential environment name must use upper snake case")
        if client == "codex":
            rendered_headers[header] = environment
        else:
            expression = f"${{env:{environment}}}" if client == "cursor" else f"${{{environment}}}"
            rendered_headers[header] = expression


def _add_stdio_environment(
    native: dict[str, object], client: str, environment: tuple[str, ...]
) -> None:
    if not environment:
        return
    if any(not _ENV_PATTERN.fullmatch(name) for name in environment):
        raise ValueError("stdio environment name must use upper snake case")
    if client == "codex":
        native["env_vars"] = list(environment)
        return
    native["env"] = {
        name: (f"${{env:{name}}}" if client == "cursor" else f"${{{name}}}") for name in environment
    }


def _plain(value: object) -> object:
    if hasattr(value, "unwrap"):
        return _plain(value.unwrap())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"cannot fingerprint MCP entry value of type {type(value).__name__}")


def _validate_client(client: str) -> None:
    if client not in _CLIENTS:
        raise ValueError(f"unsupported MCP client: {client}")
