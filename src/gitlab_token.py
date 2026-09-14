"""The saved GitLab read credential.

The GitHub token lives in a Bash helper that Dotfiles and Agentbot hold
byte-identical, and its private-file primitives are written around one filename,
one environment key, and one shape check. Generalising them would mean editing a
security-critical file both products share under a drift test, so this store is
written here instead, against the same discipline the MCP state already follows:
a mode-0700 directory, a mode-0600 file, one assignment, and a shape check
before anything is returned.

What it holds is a `read_api` token for the Agentbot-owned GitLab facade. There
is deliberately no project, group, or instance configuration alongside it: every
facade tool takes `project_id` as a call argument, so what a caller may read is
decided by the token's own access, not by anything stored here.
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

#: The key inside the file, matching the environment variable the facade and its
#: admission test both read, so a saved credential can be exported unchanged.
TOKEN_KEY = "GITLAB_MCP_READ_TOKEN"

#: GitLab personal, project, and group tokens all carry this prefix. The looser
#: second form covers a self-managed instance configured with its own prefix;
#: both require enough length that a truncated paste is refused rather than
#: saved and discovered later against a live endpoint.
_PREFIXED = re.compile(r"^glpat-[A-Za-z0-9_-]{20,}$")
_GENERIC = re.compile(r"^[A-Za-z0-9_-]{20,}$")

DEFAULT_ORIGIN = "https://gitlab.com"
VERIFY_TIMEOUT_SECONDS = 10


class TokenError(Exception):
    """A refusal that names the path, so a caller can show the operator why."""


@dataclass(frozen=True)
class VerifyResult:
    """Whether GitLab accepts the credential, and what it is allowed to do.

    `accepted` is None when the question could not be asked -- no network, or an
    answer that decides nothing. That is not a refusal, and callers must not
    treat it as one.
    """

    accepted: bool | None
    detail: str
    scopes: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        """True when every granted scope is a read scope.

        `read_api` is the contract. A token carrying `api` can write, which the
        facade's typed GET surface would never use but the credential would
        still permit somewhere else.
        """
        return bool(self.scopes) and all(scope.startswith("read_") for scope in self.scopes)


def token_file(config_home: Path) -> Path:
    return config_home / "gitlab.env"


def is_valid(token: str) -> bool:
    return bool(_PREFIXED.fullmatch(token) or _GENERIC.fullmatch(token))


def fingerprint(token: str) -> str:
    """`…cd34 (sha256:9f86d081)` -- enough to tell two tokens apart, never enough
    to reconstruct one. The same shape the GitHub helper prints."""
    if not is_valid(token):
        raise TokenError("token value is invalid")
    digest = sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"…{token[-4:]} (sha256:{digest})"


def _private_directory(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    return stat.S_IMODE(path.stat().st_mode) == 0o700


def read(config_home: Path) -> str:
    """The saved token, or an empty string.

    Every refusal returns empty rather than raising: a caller asking "is one
    saved" wants an answer, and an unsafe file is not a saved token. `inspect`
    is what says why.
    """
    try:
        return _read_checked(token_file(config_home))
    except TokenError:
        return ""


def inspect(config_home: Path) -> str | None:
    """None when the saved state is usable, otherwise why it is not."""
    try:
        _read_checked(token_file(config_home))
    except TokenError as error:
        return str(error)
    return None


def _read_checked(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return ""
    if path.is_symlink() or not path.is_file():
        raise TokenError(f"{path} is not a regular file")
    if not _private_directory(path.parent):
        raise TokenError(f"{path.parent} must be a private directory with mode 700")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise TokenError(f"{path} must have mode 600")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise TokenError(f"{path} must contain exactly one assignment")
    line = lines[0]
    if not line.startswith(f"{TOKEN_KEY}="):
        raise TokenError(f"{path} has an invalid key")
    token = line[len(TOKEN_KEY) + 1 :]
    if not is_valid(token):
        raise TokenError(f"{path} contains an invalid token")
    return token


def write(config_home: Path, token: str) -> Path:
    """Save atomically, creating the private directory if it is absent.

    Written through a mode-0600 temporary file in the same directory and then
    renamed, so a reader never sees a partial file and the secret is never
    briefly world-readable.
    """
    if not is_valid(token):
        raise TokenError("token value is invalid")
    path = token_file(config_home)
    directory = path.parent
    if directory.exists() or directory.is_symlink():
        if not _private_directory(directory):
            raise TokenError(f"{directory} is not a private directory")
    else:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    if (path.exists() or path.is_symlink()) and (
        path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise TokenError(f"{path} is not safe to replace")
    temporary = directory / f".gitlab.env.{os.getpid()}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{TOKEN_KEY}={token}\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def remove(config_home: Path) -> bool:
    """Delete the saved token. False when there was nothing to delete.

    A symlink, or a file in a directory anyone else can write, is not ours to
    delete -- following it would remove whatever it points at.
    """
    path = token_file(config_home)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or not _private_directory(path.parent):
        raise TokenError(f"{path} is a symlink or sits in a directory that is not private")
    path.unlink()
    return True


def verify(token: str, *, origin: str = DEFAULT_ORIGIN) -> VerifyResult:
    """Ask GitLab whether it accepts this credential, and for what.

    The token goes in a header, never in a URL or an argument vector: an argv is
    world-readable in /proc, and a query string lands in access logs. No
    response body is echoed -- only the scope names, which are not secret and
    are the thing worth showing.
    """
    if not is_valid(token):
        return VerifyResult(False, "token value is invalid")
    base = origin.rstrip("/")
    try:
        identity = _get(f"{base}/api/v4/user", token)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            return VerifyResult(False, f"GitLab rejected the token (HTTP {error.code})")
        return VerifyResult(None, f"GitLab answered HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return VerifyResult(None, f"could not reach GitLab: {error.__class__.__name__}")

    username = identity.get("username") if isinstance(identity, dict) else None
    detail = f"accepted for {username}" if username else "accepted"
    # Self-inspection is a personal-access-token endpoint. A project or group
    # token answers 401 or 404 here while working perfectly for reads, so its
    # absence is not a failure and the scopes are simply unknown.
    try:
        record = _get(f"{base}/api/v4/personal_access_tokens/self", token)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return VerifyResult(True, detail)
    scopes = tuple(record.get("scopes", ())) if isinstance(record, dict) else ()
    return VerifyResult(True, detail, scopes)


def _get(url: str, token: str) -> object:
    request = urllib.request.Request(url, method="GET")
    request.add_header("PRIVATE-TOKEN", token)
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))
