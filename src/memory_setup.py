"""Explicit memory vault setup (ADR-0009, ticket M2).

Agentbot no longer searches for a vault. The operator names one per machine:

- ``--path``   an existing local checkout;
- ``--clone``  a Git URL, cloned to a new destination;
- ``--new``    a new, empty vault, optionally with a remote to push to later.

The choice is stored in private configuration,
``${XDG_CONFIG_HOME:-~/.config}/agentbot/memory.json`` (directory ``0700``,
file ``0600``). It holds the absolute path, the vault's identity (its root
commits), the sanitized remote, and the branch. It never holds credentials;
Git's own credential helpers and SSH keys authenticate. A configured path that
later holds a different repository fails closed.

Removing the configuration never touches the vault.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic_io import write_text_atomic
from .memory import MARKER, REGISTRY, SUPPORTED_SCHEMA, MemoryVaultError, read_marker

CONFIG_NAME = "memory.json"
CONFIG_VERSION = 1
# A vault under a volatile root is lost on reboot.
VOLATILE_ROOTS: tuple[str, ...] = ("/tmp", "/var/tmp")
WSL_DRIVE = re.compile(r"^/mnt/[a-z](/|$)")
SCP_URL = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$")
NEW_VAULT_GITIGNORE = "exports/*\n!exports/README.md\n"
NEW_VAULT_READMES = {
    ".meta/README.md": "# Vault metadata\n\nManaged by Agentbot.\n",
    "core/README.md": "# Core\n\nUser-approved records. Agents propose; the user approves.\n",
    "projects/README.md": "# Projects\n\nOne folder per registered project. Agents write here.\n",
    "proposals/README.md": "# Proposals\n\nCore records waiting for the user's review.\n",
}


def config_path(config_home: Path) -> Path:
    return config_home / CONFIG_NAME


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MemoryVaultError("git is unavailable or did not respond") from error


def identity(vault: Path) -> list[str]:
    """The vault's root commits: stable across clones and machines."""
    result = _git("-C", str(vault), "rev-list", "--max-parents=0", "HEAD")
    if result.returncode != 0 or not result.stdout.split():
        raise MemoryVaultError(f"{vault} has no commits to identify it by")
    return sorted(result.stdout.split())


# --- Configuration ------------------------------------------------------------


def read_config(config_home: Path) -> dict[str, Any] | None:
    path = config_path(config_home)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise MemoryVaultError(f"{path} is a symlink; refusing to read it")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryVaultError(f"{path} is unreadable") from error
    if not isinstance(data, dict) or data.get("version") != CONFIG_VERSION or "vault" not in data:
        raise MemoryVaultError(f"{path} has an unsupported format")
    return data


def _write_config(
    config_home: Path, vault: dict[str, Any], backup: dict[str, str] | None = None
) -> Path:
    # Keep what this file holds besides the vault, such as the sync mode and
    # interval: rewriting only the vault silently turned manual sync back to
    # auto. A recorded backup belongs to one vault, so it goes with it.
    data: dict[str, Any] = dict(read_config(config_home) or {})
    if data and data["vault"].get("identity") != vault.get("identity"):
        data.pop("backup", None)
    data.update(version=CONFIG_VERSION, vault=vault)
    if backup is not None:
        data["backup"] = backup
    config_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config_home, 0o700)
    path = config_path(config_home)
    if path.is_symlink():
        raise MemoryVaultError(f"{path} is a symlink; refusing to write it")
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


def recorded_backup(config_home: Path) -> dict[str, str] | None:
    """The backup this machine refreshes on every update, if one was made."""
    data = read_config(config_home)
    backup = data.get("backup") if data else None
    if not isinstance(backup, dict) or not isinstance(backup.get("destination"), str):
        return None
    return {"destination": backup["destination"], "assurance": backup.get("assurance", "unknown")}


def record_backup(config_home: Path, destination: Path, assurance: str) -> None:
    data = read_config(config_home)
    if data is None:
        return  # an environment-named vault has no machine config to record in
    backup = {"destination": str(destination), "assurance": assurance}
    _write_config(config_home, data["vault"], backup)


def configured_vault(config_home: Path) -> Path | None:
    """The configured vault, verified against its recorded identity, or None."""
    data = read_config(config_home)
    if data is None:
        return None
    vault = Path(data["vault"]["path"])
    if not (vault / MARKER).is_file():
        raise MemoryVaultError(
            f"the configured vault {vault} is missing or no longer a vault; "
            "run agentbot memory setup again"
        )
    if identity(vault) != data["vault"]["identity"]:
        raise MemoryVaultError(
            f"{vault} is a different repository from the one configured; refusing to use it"
        )
    return vault


BINDINGS_NAME = "memory-bindings.json"


def bindings(config_home: Path) -> dict[str, str]:
    """This machine's checkout -> project ID bindings, for repositories with no remote."""
    path = config_home / BINDINGS_NAME
    if path.is_symlink():
        raise MemoryVaultError(f"{path} is a symlink; refusing to read it")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryVaultError(f"{path} is unreadable") from error
    return {str(key): str(value) for key, value in data.get("bindings", {}).items()}


def bind(config_home: Path, toplevel: Path, project_id: str) -> None:
    current = bindings(config_home)
    current[str(toplevel.resolve())] = project_id
    config_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config_home, 0o700)
    path = config_home / BINDINGS_NAME
    write_text_atomic(path, json.dumps({"version": 1, "bindings": current}, indent=2) + "\n")
    os.chmod(path, 0o600)


# --- Validation ---------------------------------------------------------------


def sanitize_url(url: str) -> str:
    """Accept SSH, scp-style, HTTPS, or a local path; refuse embedded credentials."""
    value = url.strip()
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise MemoryVaultError(
            "the remote URL is empty or contains whitespace or control characters"
        )
    if value.startswith("/"):
        return value
    if SCP_URL.match(value) and "://" not in value:
        return value
    match = re.match(r"^(ssh|https|http|git|file)://([^/]*)(/.*)?$", value, re.IGNORECASE)
    if not match:
        raise MemoryVaultError("the remote URL is not an SSH, HTTPS, or local Git URL")
    scheme, authority = match.group(1).lower(), match.group(2)
    if "?" in value or "#" in value:
        raise MemoryVaultError("the remote URL must not carry a query or fragment")
    if "@" in authority:
        userinfo = authority.rsplit("@", 1)[0]
        if scheme != "ssh" or ":" in userinfo:
            raise MemoryVaultError(
                "the remote URL embeds credentials; use an SSH key or a Git credential helper"
            )
    return value


def _inside_repository(path: Path) -> Path | None:
    probe = path
    while not probe.exists():
        probe = probe.parent
    top = _git("-C", str(probe), "rev-parse", "--show-toplevel")
    return Path(top.stdout.strip()) if top.returncode == 0 else None


def check_destination(path: Path, *, allow_empty: bool = True) -> Path:
    """A place a new vault may be created or cloned into."""
    if not path.is_absolute():
        raise MemoryVaultError("give an absolute destination path")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise MemoryVaultError(
                f"{parent} is a symlink; refusing to create the vault through it"
            )
    resolved = path.resolve()
    if resolved in {Path("/"), Path.home().resolve()}:
        raise MemoryVaultError(
            "the vault cannot be the filesystem root or the home directory itself"
        )
    if WSL_DRIVE.match(str(resolved)):
        raise MemoryVaultError("the vault must live on the Linux filesystem, not a Windows drive")
    if any(
        str(resolved) == root or str(resolved).startswith(root + "/") for root in VOLATILE_ROOTS
    ):
        raise MemoryVaultError("the vault must not live in a temporary directory")
    if resolved.exists() and (
        not resolved.is_dir() or (any(resolved.iterdir()) or not allow_empty)
    ):
        raise MemoryVaultError(f"{resolved} already exists and is not empty")
    owner = _inside_repository(resolved)
    if owner is not None:
        raise MemoryVaultError(
            f"{resolved} is inside the repository at {owner}. Choose a location outside it, "
            "or add the vault there as a submodule yourself and use --path."
        )
    return resolved


def check_existing(path: Path) -> Path:
    if not path.is_absolute():
        raise MemoryVaultError("give an absolute path to the vault checkout")
    if path.is_symlink():
        raise MemoryVaultError(f"{path} is a symlink; point setup at the real checkout")
    resolved = path.resolve()
    top = _git("-C", str(resolved), "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != resolved:
        raise MemoryVaultError(f"{resolved} is not the top of a Git checkout")
    read_marker(resolved)
    return resolved


def _describe(vault: Path) -> dict[str, Any]:
    remote = _git("-C", str(vault), "remote", "get-url", "origin")
    branch = _git("-C", str(vault), "symbolic-ref", "--short", "HEAD")
    url = remote.stdout.strip() if remote.returncode == 0 else None
    return {
        "path": str(vault),
        "identity": identity(vault),
        "remote": _redact(url) if url else None,
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
    }


def _redact(url: str) -> str:
    try:
        return sanitize_url(url)
    except MemoryVaultError:
        return re.sub(r"//[^/@]*@", "//***@", url)


# --- Setup flows --------------------------------------------------------------


@dataclass
class SetupResult:
    state: str  # preview, configured, removed, shown, unconfigured
    mode: str
    vault: dict[str, Any] = field(default_factory=dict)
    config: str | None = None
    notes: list[str] = field(default_factory=list)


# The vault's commit and push checks are installed by the setup command itself.
NEXT_STEPS = ("Take a first backup with: agentbot memory backup --destination PATH --yes",)


def setup_path(path: Path, config_home: Path, *, apply: bool = False) -> SetupResult:
    vault = check_existing(path)
    result = SetupResult(state="preview", mode="path", vault=_describe(vault))
    if apply:
        result.config = str(_write_config(config_home, result.vault))
        result.state = "configured"
        result.notes.extend(NEXT_STEPS)
    return result


def setup_clone(
    url: str, destination: Path, config_home: Path, *, apply: bool = False
) -> SetupResult:
    source = sanitize_url(url)
    target = check_destination(destination)
    result = SetupResult(
        state="preview", mode="clone", vault={"path": str(target), "remote": source}
    )
    if not apply:
        result.notes.append(f"Will clone {source} into {target}; nothing is pushed.")
        return result
    created = not target.exists()
    clone = _git("clone", "--quiet", "--no-recurse-submodules", source, str(target))
    try:
        if clone.returncode != 0:
            raise MemoryVaultError(f"git clone failed: {clone.stderr.strip()[:200]}")
        os.chmod(target, 0o700)
        try:
            read_marker(target)
        except MemoryVaultError as error:
            raise MemoryVaultError(f"{source} is not an Agentbot memory vault: {error}") from error
        result.vault = _describe(target)
    except MemoryVaultError:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    result.config = str(_write_config(config_home, result.vault))
    result.state = "configured"
    result.notes.extend(NEXT_STEPS)
    return result


def setup_new(
    destination: Path, config_home: Path, *, remote: str | None = None, apply: bool = False
) -> SetupResult:
    target = check_destination(destination)
    origin = sanitize_url(remote) if remote else None
    result = SetupResult(state="preview", mode="new", vault={"path": str(target), "remote": origin})
    if not apply:
        result.notes.append(
            f"Will create an empty schema {SUPPORTED_SCHEMA} vault at {target}"
            + (f" with origin {origin}; nothing is pushed." if origin else ".")
        )
        return result
    created = not target.exists()
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        if _git("init", "--quiet", "--initial-branch=main", str(target)).returncode != 0:
            raise MemoryVaultError("git init failed")
        marker = {"agentbot_memory_schema": SUPPORTED_SCHEMA, "vault_id": str(uuid.uuid4())}
        for relative, text in NEW_VAULT_READMES.items():
            (target / relative).parent.mkdir(exist_ok=True)
            write_text_atomic(target / relative, text)
        write_text_atomic(target / MARKER, json.dumps(marker, indent=2) + "\n")
        write_text_atomic(target / REGISTRY, '{ "projects": [] }\n')
        write_text_atomic(target / ".gitignore", NEW_VAULT_GITIGNORE)
        _initial_commit(target)
        if origin:
            _git("-C", str(target), "remote", "add", "origin", origin)
        result.vault = _describe(target)
    except BaseException:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    result.config = str(_write_config(config_home, result.vault))
    result.state = "configured"
    if origin:
        result.notes.append("Push the first commit when ready: git -C VAULT push -u origin main")
    result.notes.extend(NEXT_STEPS)
    return result


def _initial_commit(vault: Path) -> None:
    """Plumbing, so a user's git wrapper or hooks cannot alter the first commit."""
    if _git("-C", str(vault), "add", "-A").returncode != 0:
        raise MemoryVaultError("git add failed in the new vault")
    tree = _git("-C", str(vault), "write-tree").stdout.strip()
    commit = _git("-C", str(vault), "commit-tree", tree, "-m", "Create the memory vault")
    if commit.returncode != 0:
        raise MemoryVaultError(
            "git could not create the first commit; set git user.name and user.email first"
        )
    _git("-C", str(vault), "update-ref", "refs/heads/main", commit.stdout.strip())


def remove(config_home: Path, *, apply: bool = False) -> SetupResult:
    data = read_config(config_home)
    if data is None:
        return SetupResult(state="unconfigured", mode="remove")
    result = SetupResult(state="preview", mode="remove", vault=data["vault"])
    result.notes.append("Only the machine's configuration is removed; the vault is not touched.")
    if apply:
        config_path(config_home).unlink()
        result.state = "removed"
    return result


def show(config_home: Path) -> SetupResult:
    data = read_config(config_home)
    if data is None:
        return SetupResult(
            state="unconfigured",
            mode="show",
            notes=["No vault is configured. Use agentbot memory setup --path, --clone, or --new."],
        )
    return SetupResult(
        state="shown", mode="show", vault=data["vault"], config=str(config_path(config_home))
    )


def result_json(result: SetupResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "mode": result.mode,
        "vault": result.vault,
        "config": result.config,
        "notes": result.notes,
    }
