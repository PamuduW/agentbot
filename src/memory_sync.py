"""Two-tier memory writes and Git synchronization (ADR-0009, ticket M1 prototype).

Every change is a structured operation, never a raw file write:

- ``put``      create or replace one Markdown record inside the current project;
- ``delete``   remove one record inside the current project;
- ``forget``   remove a whole project folder;
- ``approve``  a human-approved core record (the only way into ``core/``);
- ``register`` add or extend a project in ``.meta/projects.json``.

Each operation records what it expects to find: the prior content hash of its
file, or a digest of the project subtree for ``forget``. It is committed
locally at once, so offline work is never lost, and queued in the clone's
private ``.git/agentbot-memory/`` state.

``sync`` fetches, resets the branch to the remote tip, and replays each queued
operation on top of it. An operation whose expectation no longer holds is not
applied: it becomes a conflict, its original commit is kept on a recovery ref,
and nothing is silently chosen. Core conflicts are never resolved
automatically. Pushes are never forced; a rejected push re-fetches and
replays again. The registry merges by entry, so two machines adding different
projects never conflict.

This prototype checks path boundaries and runs the secret scanner on every
write. Full schema v3 validation arrives with ticket M4.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .atomic_io import write_text_atomic
from .memory import SLUG, MemoryVaultError, scan_secrets

REGISTRY = ".meta/projects.json"
STATE_DIR = "agentbot-memory"
RECOVERY_PREFIX = "refs/agentbot-memory/recovery"
PUSH_ATTEMPTS = 5
CASE_INSENSITIVE_FORGES = frozenset({"github.com", "gitlab.com", "bitbucket.org"})
IDENTITY = ("-c", "user.name=agentbot-memory", "-c", "user.email=agentbot-memory@localhost")

Kind = Literal["put", "delete", "forget", "approve", "register", "multi"]


class SyncConflict(MemoryVaultError):
    """An operation's expectation no longer holds against the shared tip."""


@dataclass
class Op:
    id: str
    kind: Kind
    tier: Literal["project", "core", "meta", "proposal"]
    path: str
    project: str | None = None
    content: str | None = None  # base64 for put/approve; JSON entry for register
    expected: str | None = None  # prior sha256, subtree digest, or None for "absent"
    commit: str | None = None
    client: str = "unknown"

    def data(self) -> bytes | None:
        return None if self.content is None else base64.b64decode(self.content)


@dataclass
class SyncResult:
    state: str  # synced, offline, no-remote
    applied: list[str] = field(default_factory=list)
    noops: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    seconds: float = 0.0


# --- Git plumbing -------------------------------------------------------------


# Set on every Git command the engine runs, and so on any hook those commands
# start (a vault pre-push hook runs `agentbot memory validate`). Automatic sync
# refuses to start while it is set, so a hook can never re-enter the sync that
# launched it.
SYNCING_ENV = "AGENTBOT_MEMORY_SYNCING"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", SYNCING_ENV: "1"}
    result = subprocess.run(
        ["git", "-C", str(root), *IDENTITY, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise MemoryVaultError(f"git {args[0]} failed: {result.stderr.strip()[:200]}")
    return result


def _branch(root: Path) -> str:
    return _git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()


def _state(root: Path) -> Path:
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    path = git_dir / STATE_DIR
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _load(root: Path, name: str) -> list[dict[str, Any]]:
    path = _state(root) / name
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save(root: Path, name: str, items: list[dict[str, Any]]) -> None:
    write_text_atomic(_state(root) / name, json.dumps(items, indent=1) + "\n")


def _log(root: Path, op_id: str, outcome: str, detail: str = "") -> None:
    entries = _load(root, "log.json")
    entries.append({"op": op_id, "outcome": outcome, "detail": detail})
    _save(root, "log.json", entries)


def _require_clean(root: Path) -> None:
    if _git(root, "status", "--porcelain=v1", "--untracked-files=normal").stdout.strip():
        raise MemoryVaultError(
            "the vault has uncommitted manual changes; commit or revert them before automatic memory work"
        )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Paths and identity -------------------------------------------------------


def _safe_rel(rel: str) -> str:
    parts = rel.split("/")
    if (
        not rel
        or rel.startswith("/")
        or "\\" in rel
        or any(part in {"", ".", ".."} for part in parts)
        or not rel.endswith(".md")
    ):
        raise MemoryVaultError(f"unsafe record path: {rel!r}")
    return rel


def _no_symlinks(root: Path, relative: str) -> None:
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise MemoryVaultError(f"{relative} crosses a symlink")


def _project_path(project: str, rel: str) -> str:
    if not SLUG.match(project):
        raise MemoryVaultError(f"invalid project folder: {project!r}")
    return f"projects/{project}/{_safe_rel(rel)}"


def normalize_origin(url: str) -> str:
    """host/namespace/repo for SSH, scp-style, and HTTP(S) remotes, credentials removed."""
    value = url.strip()
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$", value)
    if scp and "://" not in value:
        host, path = scp.group(1), scp.group(2)
    else:
        match = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", value, re.IGNORECASE)
        if not match:
            raise MemoryVaultError("the remote URL is not a recognizable Git origin")
        host, path = match.group(1), match.group(2)
    host = host.lower()
    path = re.sub(r"[?#].*$", "", path).strip("/")
    path = re.sub(r"/+", "/", path)
    if path.endswith(".git"):
        path = path[:-4]
    if host in CASE_INSENSITIVE_FORGES:
        path = path.lower()
    return f"{host}/{path}"


def resolve_project(root: Path, origin_url: str) -> dict[str, Any] | None:
    """The registry entry whose origin or alias matches, or None."""
    wanted = normalize_origin(origin_url)
    for entry in _registry(root)["projects"]:
        if wanted == entry["origin"] or wanted in entry.get("aliases", []):
            return entry
    return None


def _registry(root: Path) -> dict[str, Any]:
    path = root / REGISTRY
    if not path.exists():
        return {"projects": []}
    return json.loads(path.read_text(encoding="utf-8"))


# --- Operations ---------------------------------------------------------------


def _current(root: Path, relative: str) -> bytes | None:
    path = root / relative
    _no_symlinks(root, relative)
    return path.read_bytes() if path.is_file() else None


def _subtree_digest(root: Path, folder: str) -> str | None:
    listing = _git(root, "ls-tree", "-r", "HEAD", "--", folder).stdout
    return _sha(listing.encode()) if listing else None


def _scan(relative: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MemoryVaultError(f"{relative}: not UTF-8") from error
    blocking = [item.rule for item in scan_secrets(relative, text) if item.severity == "error"]
    if blocking:
        raise MemoryVaultError(f"{relative}: blocked by secret scanner ({', '.join(blocking)})")


def _record(root: Path, op: Op) -> Op:
    """Apply to the local tree, commit, and queue for sync."""
    _require_clean(root)
    _apply(root, op)
    _commit(root, op)
    op.commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    pending = _load(root, "pending.json")
    pending.append(asdict(op))
    _save(root, "pending.json", pending)
    return op


def put_project(root: Path, project: str, rel: str, data: bytes, *, client: str = "unknown") -> Op:
    relative = _project_path(project, rel)
    _scan(relative, data)
    prior = _current(root, relative)
    op = Op(
        id=str(uuid.uuid4()),
        kind="put",
        tier="project",
        path=relative,
        project=project,
        content=base64.b64encode(data).decode(),
        expected=None if prior is None else _sha(prior),
        client=client,
    )
    return _record(root, op)


def delete_project(root: Path, project: str, rel: str, *, client: str = "unknown") -> Op:
    relative = _project_path(project, rel)
    prior = _current(root, relative)
    if prior is None:
        raise MemoryVaultError(f"{relative} does not exist")
    op = Op(
        str(uuid.uuid4()), "delete", "project", relative, project, None, _sha(prior), client=client
    )
    return _record(root, op)


def forget_project(root: Path, project: str, *, client: str = "unknown") -> Op:
    folder = f"projects/{project}"
    if not SLUG.match(project):
        raise MemoryVaultError(f"invalid project folder: {project!r}")
    digest = _subtree_digest(root, folder)
    if digest is None:
        raise MemoryVaultError(f"{folder} has no memory to forget")
    op = Op(str(uuid.uuid4()), "forget", "project", folder, project, None, digest, client=client)
    return _record(root, op)


def approve_core(root: Path, rel: str, data: bytes, *, client: str = "human") -> Op:
    """The human-only path into core memory."""
    relative = f"core/{_safe_rel(rel)}"
    _scan(relative, data)
    prior = _current(root, relative)
    op = Op(
        str(uuid.uuid4()),
        "approve",
        "core",
        relative,
        None,
        base64.b64encode(data).decode(),
        None if prior is None else _sha(prior),
        client=client,
    )
    return _record(root, op)


def register_origin(
    root: Path,
    origin_url: str,
    folder: str,
    project_id: str | None = None,
    *,
    client: str = "agentbot",
) -> Op:
    """Add a project, or an alias to an existing project ID."""
    if not SLUG.match(folder):
        raise MemoryVaultError(f"invalid project folder: {folder!r}")
    # A generated local origin is already canonical; everything else is a URL.
    origin = origin_url if origin_url.startswith("local/") else normalize_origin(origin_url)
    entry = {"id": project_id or str(uuid.uuid4()), "origin": origin, "folder": folder}
    op = Op(
        str(uuid.uuid4()),
        "register",
        "meta",
        REGISTRY,
        folder,
        json.dumps(entry),
        None,
        client=client,
    )
    return _record(root, op)


def _apply(root: Path, op: Op) -> str:
    """Apply one operation to the working tree. Returns applied or noop; raises SyncConflict."""
    if op.kind == "register":
        return _apply_register(root, op)
    if op.kind == "multi":
        return _apply_multi(root, op)
    if op.kind == "forget":
        current_digest = _subtree_digest(root, op.path)
        if current_digest is None:
            return "noop"
        if current_digest != op.expected:
            raise SyncConflict(f"{op.path} changed since it was forgotten")
        _git(root, "rm", "-r", "-q", "--", op.path)
        return "applied"
    current = _current(root, op.path)
    current_hash = None if current is None else _sha(current)
    target = op.data()
    if op.kind == "delete":
        if current is None:
            return "noop"
        if current_hash != op.expected:
            raise SyncConflict(f"{op.path} changed since it was read")
        (root / op.path).unlink()
        return "applied"
    if target is None:
        raise MemoryVaultError(f"{op.kind} operation {op.id} carries no content")
    if current_hash == _sha(target):
        return "noop"
    if current_hash != op.expected:
        raise SyncConflict(f"{op.path} changed since it was read")
    _scan(op.path, target)
    destination = root / op.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(target)
    return "applied"


def _apply_multi(root: Path, op: Op) -> str:
    """Several files in one project, all or nothing: every expectation is checked first."""
    items = json.loads(op.content or "[]")
    plan: list[tuple[str, bytes | None]] = []
    for item in items:
        if not _within(item["path"], op):
            raise MemoryVaultError(f"{item['path']} is outside {op.path}")
        current = _current(root, item["path"])
        current_hash = None if current is None else _sha(current)
        target = None if item["content"] is None else base64.b64decode(item["content"])
        done = (current is None) if target is None else current_hash == _sha(target)
        if done:
            continue
        if current_hash != item["expected"]:
            raise SyncConflict(f"{item['path']} changed since it was read")
        plan.append((item["path"], target))
    if not plan:
        return "noop"
    for path, target in plan:
        if target is not None:
            _scan(path, target)
    for path, target in plan:
        destination = root / path
        if target is None:
            destination.unlink()
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(target)
    return "applied"


CORE_PREFIXES = ("core/", "proposals/core/")


def _within(path: str, op: Op) -> bool:
    """A multi operation's files stay in its scope: one project, or core and proposals."""
    if op.path == ".":
        return op.tier in {"core", "proposal"} and path.startswith(CORE_PREFIXES)
    return path.startswith(op.path + "/")


def change_core(
    root: Path,
    changes: list[tuple[str, bytes | None]],
    *,
    tier: Literal["core", "proposal"],
    client: str = "unknown",
) -> Op:
    """Put or delete core records and proposals as one operation.

    ``proposal`` operations may touch only ``proposals/core/``; ``core``
    operations (the user's approvals) may touch both.
    """
    allowed = ("proposals/core/",) if tier == "proposal" else CORE_PREFIXES
    items = []
    for relative, data in changes:
        if not relative.startswith(allowed):
            raise MemoryVaultError(f"{relative} is outside {' and '.join(allowed)}")
        _safe_rel(relative)
        if data is not None:
            _scan(relative, data)
        prior = _current(root, relative)
        if data is None and prior is None:
            raise MemoryVaultError(f"{relative} does not exist")
        items.append(
            {
                "path": relative,
                "content": None if data is None else base64.b64encode(data).decode(),
                "expected": None if prior is None else _sha(prior),
            }
        )
    op = Op(str(uuid.uuid4()), "multi", tier, ".", None, json.dumps(items), None, client=client)
    return _record(root, op)


def change_project(
    root: Path, project: str, changes: list[tuple[str, bytes | None]], *, client: str = "unknown"
) -> Op:
    """Put or delete several records of one project as a single operation."""
    folder = f"projects/{project}"
    if not SLUG.match(project):
        raise MemoryVaultError(f"invalid project folder: {project!r}")
    items = []
    for relative, data in changes:
        if not relative.startswith(folder + "/"):
            raise MemoryVaultError(f"{relative} is outside {folder}")
        _safe_rel(relative.split("/", 2)[2])
        if data is not None:
            _scan(relative, data)
        prior = _current(root, relative)
        if data is None and prior is None:
            raise MemoryVaultError(f"{relative} does not exist")
        items.append(
            {
                "path": relative,
                "content": None if data is None else base64.b64encode(data).decode(),
                "expected": None if prior is None else _sha(prior),
            }
        )
    op = Op(
        str(uuid.uuid4()),
        "multi",
        "project",
        folder,
        project,
        json.dumps(items),
        None,
        client=client,
    )
    return _record(root, op)


def _apply_register(root: Path, op: Op) -> str:
    entry = json.loads(op.content or "{}")
    registry = _registry(root)
    for existing in registry["projects"]:
        names = {existing["origin"], *existing.get("aliases", [])}
        if existing["id"] == entry["id"]:
            if entry["origin"] in names:
                return "noop"
            existing.setdefault("aliases", []).append(entry["origin"])
            break
        if entry["origin"] in names:
            raise SyncConflict(f"{entry['origin']} already belongs to project {existing['folder']}")
        if existing["folder"] == entry["folder"]:
            raise SyncConflict(f"folder {entry['folder']} already belongs to another project")
    else:
        registry["projects"].append(entry)
    path = root / REGISTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(registry, indent=2, sort_keys=True) + "\n")
    return "applied"


def _commit(root: Path, op: Op) -> None:
    if op.kind == "multi":
        # Stage exactly the operation's own files, added or removed.
        paths = [item["path"] for item in json.loads(op.content or "[]")]
        _git(root, "add", "-A", "--", *paths)
    elif op.kind != "forget":  # git rm already staged a forget
        _git(root, "add", "-A", "--", op.path)
    message = (
        f"memory({op.tier}): {op.kind} {op.path}\n\n"
        f"Agentbot-Op: {op.id}\nAgentbot-Tier: {op.tier}\nAgentbot-Client: {op.client}\n"
    )
    # Plumbing, not `git commit`: a user's git wrapper or commit hooks (for
    # example one that fetches first) must not change what the engine does,
    # and offline commits must work. The engine has already secret-scanned.
    tree = _git(root, "write-tree").stdout.strip()
    parent = _git(root, "rev-parse", "HEAD").stdout.strip()
    commit = _git(root, "commit-tree", tree, "-p", parent, "-m", message).stdout.strip()
    _git(root, "update-ref", "HEAD", commit, parent)


# --- Sync -----------------------------------------------------------------------


def _local_commits_are_ours(root: Path, upstream: str, pending: list[dict[str, Any]]) -> bool:
    ours = {item["id"] for item in pending}
    log = _git(
        root, "log", "--format=%(trailers:key=Agentbot-Op,valueonly)", f"{upstream}..HEAD"
    ).stdout
    found = [line.strip() for line in log.splitlines() if line.strip()]
    return all(op_id in ours for op_id in found) and len(found) == len(
        _git(root, "rev-list", f"{upstream}..HEAD").stdout.split()
    )


def sync(root: Path, *, remote: str = "origin") -> SyncResult:
    """Fetch, replay queued operations on the remote tip, push. Never force."""
    started = time.monotonic()
    result = SyncResult(state="synced")
    _require_clean(root)
    branch = _branch(root)
    upstream = f"{remote}/{branch}"
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        result.attempts = attempt
        if _git(root, "fetch", "-q", remote, check=False).returncode != 0:
            result.state = "offline"
            break
        pending = _load(root, "pending.json")
        has_upstream = (
            _git(root, "rev-parse", "--verify", "-q", upstream, check=False).returncode == 0
        )
        if has_upstream and not _local_commits_are_ours(root, upstream, pending):
            raise MemoryVaultError(
                "the vault has local commits Agentbot did not make; push them by hand first"
            )
        if has_upstream:
            _git(root, "reset", "-q", "--hard", upstream)
        remaining: list[dict[str, Any]] = []
        for item in pending:
            op = Op(**item)
            try:
                outcome = _apply(root, op)
            except SyncConflict as error:
                _preserve(root, op, str(error), result)
                continue
            if outcome == "applied":
                _commit(root, op)
                result.applied.append(op.id)
            else:
                result.noops.append(op.id)
            remaining.append(item)
        push = _git(root, "push", "-q", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            for op_id in result.applied:
                _log(root, op_id, "applied")
            for op_id in result.noops:
                _log(root, op_id, "noop")
            _save(root, "pending.json", [])
            if not has_upstream:
                _git(root, "branch", "-q", "--set-upstream-to", upstream, check=False)
            break
        # Someone pushed first: keep the queue, fetch, and replay again.
        result.applied.clear()
        result.noops.clear()
        _save(root, "pending.json", remaining)
    else:
        result.state = "retry-exhausted"
    result.seconds = time.monotonic() - started
    return result


def _preserve(root: Path, op: Op, reason: str, result: SyncResult) -> None:
    ref = f"{RECOVERY_PREFIX}/{op.id}"
    if op.commit:
        _git(root, "update-ref", ref, op.commit)
    conflict = {
        "op": op.id,
        "kind": op.kind,
        "tier": op.tier,
        "path": op.path,
        "project": op.project,
        "reason": reason,
        "recovery_ref": ref,
        "resolver": "human" if op.tier == "core" else "agent",
        # The whole operation, so "keep mine" can re-apply it later.
        "op_data": asdict(op),
    }
    conflicts = _load(root, "conflicts.json")
    conflicts.append(conflict)
    _save(root, "conflicts.json", conflicts)
    pending = [item for item in _load(root, "pending.json") if item["id"] != op.id]
    _save(root, "pending.json", pending)
    _log(root, op.id, "conflict", reason)
    result.conflicts.append(conflict)


def conflicts(root: Path) -> list[dict[str, Any]]:
    return _load(root, "conflicts.json")


def mark_resolved(root: Path, op_id: str, keep: str, resolution_op: str | None) -> None:
    """Record how a conflict was settled; the recovery ref stays for audit."""
    items = _load(root, "conflicts.json")
    for item in items:
        if item["op"] == op_id:
            item["resolved"] = keep
            item["resolution_op"] = resolution_op
    _save(root, "conflicts.json", items)


def outcomes(root: Path) -> dict[str, str]:
    return {entry["op"]: entry["outcome"] for entry in _load(root, "log.json")}


def pending(root: Path) -> list[dict[str, Any]]:
    return _load(root, "pending.json")
