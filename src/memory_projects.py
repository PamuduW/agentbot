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

import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import memory_setup
from .atomic_io import write_text_atomic
from .memory import (
    MAX_FILE_BYTES,
    MemoryVaultError,
    _front_matter,
    _Unsafe,
    read_bounded,
    registry_problems,
)
from .memory_sync import _locked, _registry, _state, normalize_origin, put_project, register_origin

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
    """Register project memory, resuming an interrupted registration on retry."""
    with _locked(vault):
        current = resolve(vault, cwd, config_home)
        identity = current.identity
        if identity is None or current.state not in {"resolved", "unregistered"}:
            require_project(vault, cwd, config_home)
            raise MemoryVaultError("this repository cannot be registered")
        key = sha256(str(identity.toplevel).encode()).hexdigest()
        intent_path = _state(vault) / f"registration-{key}.json"
        if intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if intent["canonical"] != identity.canonical:
                raise MemoryVaultError("repository identity changed during registration; preserve the pending intent")
        else:
            if current.state == "resolved" and current.entry is not None:
                raise MemoryVaultError(f"this repository is already project {current.entry['folder']}")
            registry = _registry(vault)
            project_id = str(uuid.uuid4())
            name = identity.canonical.rsplit("/", 1)[-1] if identity.canonical else identity.toplevel.name
            folder = folder_name(name, registry)
            origin = identity.canonical or f"{LOCAL_PREFIX}{project_id[:8]}-{folder}"
            intent = {"id": project_id, "folder": folder, "origin": origin, "canonical": identity.canonical}
            write_text_atomic(intent_path, json.dumps(intent) + "\n")
        project_id, folder, origin = intent["id"], intent["folder"], intent["origin"]
        entry = next((entry for entry in _registry(vault)["projects"] if entry["id"] == project_id), None)
        if entry is None:
            register_origin(vault, identity.origin_url or origin, folder, project_id, client=client)
        elif entry["folder"] != folder or entry["origin"] != origin:
            raise MemoryVaultError("project registration changed; preserve the pending intent")
        if identity.canonical is None:
            memory_setup.bind(config_home, identity.toplevel, project_id)
        relative = f"projects/{folder}/project.md"
        if not (vault / relative).exists():
            put_project(vault, folder, "project.md", project_md(project_id, folder, origin), client=client)
        else:
            try:
                metadata = _front_matter(read_bounded(vault, relative, MAX_FILE_BYTES).decode("utf-8"))
            except (_Unsafe, UnicodeDecodeError) as error:
                raise MemoryVaultError("existing project identity is invalid; preserve it for review") from error
            if metadata.get("id") != project_id or metadata.get("projects") != [folder]:
                raise MemoryVaultError("existing project identity differs; preserve it for review")
        intent_path.unlink()
        return {"id": project_id, "folder": folder}


def project_md(project_id: str, folder: str, origin: str) -> bytes:
    """The system-maintained identity record of one project."""
    today = datetime.now(timezone.utc).date().isoformat()
    return (
        "---\nschema: 3\n"
        f"id: {project_id}\ntype: project\ntitle: {folder}\ndate: {today}\n"
        f"status: accepted\nscope: project\nprojects: [{folder}]\ntags: []\n"
        f"---\n\nOrigin: {origin}\n"
    ).encode()


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
