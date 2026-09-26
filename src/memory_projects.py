"""Project identity across machines (ADR-0009, ticket M3).

A working directory resolves to a project through, in order:

    cwd -> Git top level -> remote.origin.url -> normalized origin
        -> .meta/projects.json entry (origin or alias) -> project ID and folder

The registry lives in the vault, so every machine resolves the same repository
to the same folder. A repository with no remote cannot be recognized on
another machine by name, so its identity is a generated ``local/<id>-<name>``
origin, and each extra machine attaches its own checkout explicitly. That
binding is per-machine and lives in private config, never in the vault.

Nothing is guessed:

- a fork is a different origin, so a different project, unless the user links
  it;
- a renamed repository becomes an alias of its old project only through an
  explicit link;
- a registry in which two projects claim one origin or one folder is a
  collision, and every write for the affected projects stops until it is
  fixed.

Registry changes go through ``memory_sync.register_origin``: one structured,
merge-safe operation, committed and queued for sync.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memory_setup
from .memory import MemoryVaultError
from .memory_sync import _registry, normalize_origin, register_origin

LOCAL_PREFIX = "local/"


@dataclass(frozen=True)
class RepoIdentity:
    toplevel: Path
    origin_url: str | None
    canonical: str | None


@dataclass
class Resolution:
    state: str  # resolved, unregistered, collision, no-repository
    identity: RepoIdentity | None = None
    entry: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, timeout=60
    )


def repo_identity(cwd: Path) -> RepoIdentity | None:
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    toplevel = Path(top.stdout.strip()).resolve()
    remote = _git(toplevel, "config", "--get", "remote.origin.url")
    url = remote.stdout.strip() if remote.returncode == 0 and remote.stdout.strip() else None
    return RepoIdentity(toplevel, url, normalize_origin(url) if url else None)


def registry_problems(registry: dict[str, Any]) -> dict[str, list[str]]:
    """Problems keyed by project ID: shared origins, aliases, folders, or IDs."""
    problems: dict[str, list[str]] = {}
    claims: dict[str, str] = {}
    folders: dict[str, str] = {}
    seen: set[str] = set()
    for entry in registry["projects"]:
        pid = entry["id"]
        if pid in seen:
            problems.setdefault(pid, []).append("the project ID appears twice")
        seen.add(pid)
        for name in (entry["origin"], *entry.get("aliases", [])):
            owner = claims.setdefault(name, pid)
            if owner != pid:
                for affected in (owner, pid):
                    problems.setdefault(affected, []).append(f"{name} is claimed by two projects")
        owner = folders.setdefault(entry["folder"], pid)
        if owner != pid:
            for affected in (owner, pid):
                problems.setdefault(affected, []).append(f"folder {entry['folder']} is used twice")
    return problems


def resolve(vault: Path, cwd: Path, config_home: Path) -> Resolution:
    identity = repo_identity(cwd)
    if identity is None:
        return Resolution(state="no-repository")
    registry = _registry(vault)
    problems = registry_problems(registry)
    matches = []
    if identity.canonical is not None:
        matches = [
            entry
            for entry in registry["projects"]
            if identity.canonical in {entry["origin"], *entry.get("aliases", [])}
        ]
    else:
        bound = memory_setup.bindings(config_home).get(str(identity.toplevel))
        matches = [entry for entry in registry["projects"] if entry["id"] == bound]
    if not matches:
        return Resolution(state="unregistered", identity=identity)
    if len(matches) > 1:
        return Resolution(
            state="collision",
            identity=identity,
            problems=[f"{identity.canonical} matches {len(matches)} projects"],
        )
    entry = matches[0]
    if entry["id"] in problems:
        return Resolution(
            state="collision", identity=identity, entry=entry, problems=problems[entry["id"]]
        )
    return Resolution(state="resolved", identity=identity, entry=entry)


def require_project(vault: Path, cwd: Path, config_home: Path) -> dict[str, Any]:
    """The resolved project entry, or an error that stops the write."""
    result = resolve(vault, cwd, config_home)
    if result.state == "resolved" and result.entry is not None:
        return result.entry
    if result.state == "collision":
        raise MemoryVaultError(
            "project identity collision; writes stop until it is fixed: "
            + "; ".join(result.problems)
        )
    if result.state == "unregistered":
        raise MemoryVaultError(
            "this repository has no project memory yet; register or attach it first"
        )
    raise MemoryVaultError("not inside a Git repository, so there is no current project")


def folder_name(name: str, registry: dict[str, Any]) -> str:
    """A readable, unused folder name: the repository name, suffixed on a clash."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-") or "project"
    used = {entry["folder"] for entry in registry["projects"]}
    candidate, n = slug, 2
    while candidate in used:
        candidate, n = f"{slug}-{n}", n + 1
    return candidate


def register(
    vault: Path, cwd: Path, config_home: Path, *, client: str = "agentbot"
) -> dict[str, Any]:
    """Give the current repository project memory. Refuses if it already has some."""
    current = resolve(vault, cwd, config_home)
    if current.state == "resolved" and current.entry is not None:
        raise MemoryVaultError(f"this repository is already project {current.entry['folder']}")
    identity = current.identity
    if current.state != "unregistered" or identity is None:
        require_project(vault, cwd, config_home)  # raises with the specific reason
        raise MemoryVaultError("this repository cannot be registered")
    registry = _registry(vault)
    project_id = str(uuid.uuid4())
    if identity.canonical is not None:
        folder = folder_name(identity.canonical.rsplit("/", 1)[-1], registry)
        register_origin(vault, identity.origin_url or "", folder, project_id, client=client)
    else:
        folder = folder_name(identity.toplevel.name, registry)
        origin = f"{LOCAL_PREFIX}{project_id[:8]}-{folder}"
        register_origin(vault, origin, folder, project_id, client=client)
        memory_setup.bind(config_home, identity.toplevel, project_id)
    return {"id": project_id, "folder": folder}


def attach(vault: Path, cwd: Path, config_home: Path, project: str) -> dict[str, Any]:
    """Bind this machine's no-remote checkout to an existing project (by ID or folder)."""
    identity = repo_identity(cwd)
    if identity is None:
        raise MemoryVaultError("not inside a Git repository")
    if identity.canonical is not None:
        raise MemoryVaultError(
            "this repository has a remote; it resolves by origin. Use link for a rename or fork."
        )
    entry = _find(vault, project)
    if not entry["origin"].startswith(LOCAL_PREFIX):
        raise MemoryVaultError(
            f"project {entry['folder']} belongs to {entry['origin']}; only local projects are attached"
        )
    memory_setup.bind(config_home, identity.toplevel, entry["id"])
    return entry


def link(vault: Path, origin_url: str, project: str, *, client: str = "human") -> dict[str, Any]:
    """Human-only: make another origin (a rename, or a fork to share) an alias of a project."""
    entry = _find(vault, project)
    register_origin(vault, origin_url, entry["folder"], entry["id"], client=client)
    return entry


def _find(vault: Path, project: str) -> dict[str, Any]:
    for entry in _registry(vault)["projects"]:
        if project in {entry["id"], entry["folder"]}:
            return entry
    raise MemoryVaultError(f"no registered project {project!r}")


def resolution_json(result: Resolution) -> dict[str, Any]:
    identity = result.identity
    return {
        "state": result.state,
        "repository": str(identity.toplevel) if identity else None,
        "origin": identity.canonical if identity else None,
        "project": result.entry,
        "problems": result.problems,
    }
