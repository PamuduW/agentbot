from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

from .atlassian_read_client import AtlassianReadClient, AtlassianReadError

KEYS = (
    "ATLASSIAN_MCP_CLOUD_ID",
    "ATLASSIAN_MCP_EMAIL",
    "JIRA_MCP_READ_TOKEN",
    "CONFLUENCE_MCP_READ_TOKEN",
)
MAX_CONFIG_BYTES = 32_768


class TokenError(ValueError):
    pass


def validate(values: object) -> dict[str, str]:
    if not isinstance(values, dict) or set(values) != set(KEYS):
        raise TokenError(
            "Atlassian settings require Cloud ID, email, Jira token and Confluence token"
        )
    if any(not isinstance(value, str) for value in values.values()):
        raise TokenError("Atlassian settings must be strings")
    try:
        AtlassianReadClient(*(values[key] for key in KEYS))
    except (AtlassianReadError, UnicodeError):
        raise TokenError("Invalid Atlassian Cloud ID, email or token; nothing was saved") from None
    return dict(values)


def _check_path(config_home: Path) -> Path:
    path = config_home / "atlassian.json"
    try:
        if config_home.exists() or config_home.is_symlink():
            if (
                config_home.is_symlink()
                or not config_home.is_dir()
                or stat.S_IMODE(config_home.stat().st_mode) != 0o700
            ):
                raise TokenError("Atlassian settings directory must be private with mode 700")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise TokenError("Atlassian settings must be a regular private file with mode 600")
    except OSError:
        raise TokenError("Cannot inspect private Atlassian settings") from None
    return path


def read(config_home: Path) -> dict[str, str]:
    path = _check_path(config_home)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise TokenError("Atlassian settings file is too large")
        return validate(json.loads(data))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise TokenError("Saved Atlassian settings are invalid or unreadable") from None


def write(config_home: Path, values: object) -> Path:
    settings = validate(values)
    payload = json.dumps(settings, ensure_ascii=True, sort_keys=True) + "\n"
    if len(payload.encode()) > MAX_CONFIG_BYTES:
        raise TokenError("Atlassian settings are too large")
    path = _check_path(config_home)
    temporary: Path | None = None
    try:
        config_home.mkdir(parents=True, mode=0o700, exist_ok=True)
        _check_path(config_home)
        descriptor, name = tempfile.mkstemp(prefix=".atlassian-", dir=config_home)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise TokenError("Could not save Atlassian settings") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def resolve(config_home: Path, environ: Mapping[str, str]) -> dict[str, str]:
    supplied = {key: environ[key] for key in KEYS if environ.get(key)}
    if len(supplied) == len(KEYS):
        return validate(supplied)
    values = {**read(config_home), **supplied}
    if set(values) != set(KEYS):
        raise TokenError(
            "Configure Atlassian in Token Config or supply all four environment references"
        )
    return validate(values)


def fingerprint(token: str) -> str:
    return f"…{token[-4:]} (sha256:{sha256(token.encode()).hexdigest()[:8]})"


def remove(config_home: Path) -> bool:
    path = _check_path(config_home)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        raise TokenError("Could not remove saved Atlassian settings") from None
    return True
