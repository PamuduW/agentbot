from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.types import Tool

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class McpFilterContract:
    catalog_id: str
    origin: str
    allowed_tools: tuple[str, ...]
    descriptor_fingerprints: dict[str, str]
    timeout_seconds: float
    max_response_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_id, str) or not _ID_PATTERN.fullmatch(self.catalog_id):
            raise ValueError("filter catalog_id must use lower snake case")
        if not isinstance(self.origin, str):
            raise ValueError("filter origin must be a fixed https URL")
        parsed = urlparse(self.origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("filter origin must be a fixed https URL")
        if not self.allowed_tools or any(
            not isinstance(name, str) or not name for name in self.allowed_tools
        ):
            raise ValueError("filter must allow at least one tool")
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("filter allowed tools must be unique")
        if set(self.descriptor_fingerprints) != set(self.allowed_tools):
            raise ValueError("filter fingerprints must match allowed tools exactly")
        if any(
            not _FINGERPRINT_PATTERN.fullmatch(value)
            for value in self.descriptor_fingerprints.values()
        ):
            raise ValueError("filter descriptor fingerprint must be a sha256 digest")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("filter timeout must be positive")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
        ):
            raise ValueError("filter response limit must be a positive integer")


def normalize_tool_descriptor(tool: Tool | dict[str, Any]) -> dict[str, Any]:
    if isinstance(tool, Tool):
        value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(tool, dict):
        value = tool
    else:
        raise TypeError("tool descriptor must be an MCP Tool or mapping")
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def descriptor_fingerprint(tool: Tool | dict[str, Any]) -> str:
    encoded = json.dumps(
        normalize_tool_descriptor(tool),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_filter_contract(path: Path, expected_id: str) -> McpFilterContract:
    if not _ID_PATTERN.fullmatch(expected_id):
        raise ValueError("filter catalog_id must use lower snake case")
    try:
        outer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read MCP filter contract: {error}") from error
    if not isinstance(outer, dict) or "filter" not in outer:
        raise ValueError("MCP filter contract must contain filter")
    raw = outer["filter"]
    required = {
        "catalog_id",
        "origin",
        "tools",
        "timeout_seconds",
        "max_response_bytes",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("MCP filter contract has invalid keys")
    if raw["catalog_id"] != expected_id:
        raise ValueError("MCP filter contract identity does not match its catalog ID")
    tool_values = raw["tools"]
    if not isinstance(tool_values, list):
        raise ValueError("MCP filter contract tools must be an array")
    names: list[str] = []
    fingerprints: dict[str, str] = {}
    for tool_value in tool_values:
        if not isinstance(tool_value, dict) or set(tool_value) != {
            "name",
            "descriptor_fingerprint",
        }:
            raise ValueError("MCP filter tool has invalid keys")
        name = tool_value["name"]
        fingerprint = tool_value["descriptor_fingerprint"]
        if not isinstance(name, str) or not isinstance(fingerprint, str):
            raise ValueError("MCP filter tool name and fingerprint must be strings")
        names.append(name)
        fingerprints[name] = fingerprint
    try:
        return McpFilterContract(
            catalog_id=expected_id,
            origin=raw["origin"],
            allowed_tools=tuple(names),
            descriptor_fingerprints=fingerprints,
            timeout_seconds=raw["timeout_seconds"],
            max_response_bytes=raw["max_response_bytes"],
        )
    except TypeError as error:
        raise ValueError(f"invalid MCP filter contract: {error}") from error
