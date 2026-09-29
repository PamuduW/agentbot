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

One operation runs at a time per clone: every write and every sync holds an
exclusive lock on ``.git/agentbot-memory/lock``. A write is staged in a
private Git index, never the user's, and the vault as it would be after the
commit is validated first; a change that adds a validation error is refused,
and on replay it becomes a conflict. The queue entry is written before HEAD
moves, so a write that was interrupted is finished or replayed on the next
run, never lost. Replay skips operations the remote already has.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .atomic_io import write_text_atomic
from .memory import SLUG, MemoryVaultError, scan_secrets, validate

REGISTRY = ".meta/projects.json"
STATE_DIR = "agentbot-memory"
RECOVERY_PREFIX = "refs/agentbot-memory/recovery"
PUSH_ATTEMPTS = 5
LOCK_WAIT = 120.0
CASE_INSENSITIVE_FORGES = frozenset({"github.com", "gitlab.com", "bitbucket.org"})
IDENTITY = ("-c", "user.name=agentbot-memory", "-c", "user.email=agentbot-memory@localhost")

Kind = Literal["put", "delete", "forget", "approve", "register", "multi", "untrack", "obsidian"]


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
    # The registry ID the project folder had when the write was made. Replay
    # refuses the write if the folder now belongs to another project.
    project_id: str | None = None

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


def _git(
    root: Path, *args: str, check: bool = True, index: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", SYNCING_ENV: "1"}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *IDENTITY, *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        # A hung fetch or push is the network, not a broken vault: callers
        # that tolerate failure (check=False) see it as one.
        if check:
            raise MemoryVaultError(f"git {args[0]} timed out") from None
        return subprocess.CompletedProcess(list(args), 124, "", "timed out")
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


# How deep this thread already holds each clone's lock (re-entrancy only).
_held = threading.local()


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    """One memory operation at a time in this clone, across processes.

    Re-entrant within a process. The first holder also finishes or rolls back
    any write an earlier process left half done (see ``_recover``).
    """
    state = _state(root)
    key = str(state)
    depth: dict[str, int] = _held.__dict__.setdefault("depth", {})
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    with open(state / "lock", "a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MemoryVaultError(
                        "another Agentbot memory operation is still running on this vault"
                    ) from None
                time.sleep(0.05)
        depth[key] = 1
        try:
            _recover(root)
            yield
        finally:
            depth[key] = 0
            fcntl.flock(handle, fcntl.LOCK_UN)


def _in_history(root: Path, op_id: str, rev: str) -> str | None:
    """The commit in ``rev``'s history that carries this operation, if any."""
    found = _git(
        root, "log", "-n", "1", "--format=%H", "-F", f"--grep=Agentbot-Op: {op_id}", rev
    ).stdout.strip()
    return found or None


def _recover(root: Path) -> None:
    """Finish or undo a write that was queued but stopped before its commit was recorded.

    The queue entry is written before HEAD moves. If the commit reached HEAD,
    record it; otherwise put the operation's files back as HEAD has them. The
    entry stays queued either way, so the next sync applies it: a write that
    was queued is never lost.
    """
    items = _load(root, "pending.json")
    changed = False
    for item in items:
        if item.get("commit"):
            continue
        op = Op(**item)
        found = _in_history(root, op.id, "HEAD")
        if found:
            item["commit"] = found
            _refresh_index(root, op)
        else:
            _restore(root, op)
        changed = True
    if changed:
        _save(root, "pending.json", items)


def _log(root: Path, op_id: str, outcome: str, detail: str = "") -> None:
    entries = _load(root, "log.json")
    entries.append({"op": op_id, "outcome": outcome, "detail": detail})
    _save(root, "log.json", entries)


# Obsidian's own folders, kept per device: its settings and layout, which it
# rewrites itself (on open, as you zoom the graph, as you resize a table), the
# Bases views it saves the same way, and its trash. They are never memory, so
# they neither block automatic work nor belong in Git.
DEVICE_STATE_DIRS = (".obsidian/", "views/", ".trash/")
# Entries an older version of the managed .gitignore block listed.
LEGACY_DEVICE_LINES = (".obsidian/workspace.json", ".obsidian/workspace-mobile.json")
DEVICE_STATE_BLOCK = "# Obsidian per-device state, managed by Agentbot"


def _device_state(path: str) -> bool:
    path = path.strip().strip('"')
    return path.startswith(DEVICE_STATE_DIRS) or f"{path}/" in DEVICE_STATE_DIRS


def _require_clean(root: Path) -> None:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal").stdout
    manual = [
        line
        for line in status.splitlines()
        if line.strip() and not all(_device_state(part) for part in line[3:].split(" -> "))
    ]
    if manual:
        raise MemoryVaultError(
            "the vault has uncommitted manual changes; commit or revert them before automatic memory work"
        )


def manual_changes(root: Path) -> bool:
    """Whether hand edits would pause automatic sync (read-only; for Doctor)."""
    try:
        _require_clean(root)
    except MemoryVaultError:
        return True
    return False


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


DEFAULT_PORTS = {"ssh": 22, "git+ssh": 22, "ssh+git": 22, "git": 9418, "http": 80, "https": 443}
SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _ssh_endpoint(host: str) -> tuple[str, int | None]:
    """The real host and port behind an SSH host alias, as the SSH config maps it.

    `ssh -G` only reads configuration; it never connects. Without ssh, or for
    an unusual name, the host stays as written.
    """
    if not SSH_HOST.match(host):
        return host, None
    try:
        shown = subprocess.run(
            ["ssh", "-G", host], capture_output=True, text=True, check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return host, None
    values = dict(line.split(" ", 1) for line in shown.stdout.splitlines() if " " in line)
    port = values.get("port", "").strip()
    return values.get("hostname", host).strip() or host, int(port) if port.isdigit() else None


def normalize_origin(url: str) -> str:
    """host[:port]/namespace/repo for SSH, scp-style, and HTTP(S) remotes, credentials removed.

    An SSH host alias resolves to the host it stands for, so `github-work:o/r`
    and `github.com:o/r` are one project. A port stays when it is not the
    scheme's default: two services on one host are two repositories.
    """
    value = url.strip()
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$", value)
    port: int | None = None
    if scp and "://" not in value:
        scheme, host, path = "ssh", scp.group(1), scp.group(2)
    else:
        match = re.match(
            r"^([a-z+]+)://(?:[^@/]+@)?([^/:]+)(?::(\d+))?/(.+)$", value, re.IGNORECASE
        )
        if not match:
            raise MemoryVaultError("the remote URL is not a recognizable Git origin")
        scheme, host, path = match.group(1).lower(), match.group(2), match.group(4)
        port = int(match.group(3)) if match.group(3) else None
    if DEFAULT_PORTS.get(scheme) == 22:
        host, configured = _ssh_endpoint(host)
        port = port or configured
    host = host.lower()
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
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


def _project_id(root: Path, folder: str) -> str:
    for entry in _registry(root)["projects"]:
        if entry["folder"] == folder:
            return str(entry["id"])
    raise MemoryVaultError(f"projects/{folder} is not a registered project")


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
    """Apply to the local tree, check the whole vault, queue, then commit."""
    with _locked(root):
        _require_clean(root)
        before = _snapshot(root)
        index: Path | None = None
        try:
            _apply(root, op)
            index = _stage(root, op)
            problems = _new_errors(root, before, index, op)
            if problems:
                raise MemoryVaultError(
                    f"{op.path} was not written: it would make the vault invalid ("
                    + "; ".join(problems)
                    + ")"
                )
        except BaseException:
            _restore(root, op)
            if index is not None:
                index.unlink(missing_ok=True)
            raise
        pending = _load(root, "pending.json")
        pending.append(asdict(op))
        _save(root, "pending.json", pending)
        try:
            op.commit = _commit(root, op, index)
        except MemoryVaultError:
            _save(
                root, "pending.json", [i for i in _load(root, "pending.json") if i["id"] != op.id]
            )
            _restore(root, op)
            raise
        finally:
            index.unlink(missing_ok=True)
        _save(
            root,
            "pending.json",
            [
                {**i, "commit": op.commit} if i["id"] == op.id else i
                for i in _load(root, "pending.json")
            ],
        )
        return op


@dataclass
class _Snapshot:
    """What the checks compare: validation errors and the size of each pool."""

    errors: set[tuple[str, str]]
    live: dict[str, int]  # project folder -> accepted records
    proposals: int
    records: list[Any]
    texts: dict[tuple[str, str], int]  # (project folder, body) -> accepted records with it


def _snapshot(root: Path, index: Path | None = None) -> _Snapshot:
    report = validate(root, index=index)
    live: dict[str, int] = {}
    texts: dict[tuple[str, str], int] = {}
    for record in report.records:
        if record.path.startswith("projects/") and not record.draft and record.status == "accepted":
            folder = record.path.split("/")[1]
            live[folder] = live.get(folder, 0) + 1
            if not record.path.endswith("/project.md"):
                key = (folder, _body(root / record.path))
                texts[key] = texts.get(key, 0) + 1
    return _Snapshot(
        errors={(f.path, f.rule) for f in report.findings if f.severity == "error"},
        live=live,
        proposals=sum(1 for r in report.records if r.draft and r.path.startswith("proposals/")),
        records=report.records,
        texts=texts,
    )


def _new_errors(root: Path, before: _Snapshot, index: Path, op: Op) -> list[str]:
    """What the staged change would break. Problems already there do not block it.

    The vault must stay valid, and the maintenance limits hold here too, so a
    replay, a keep-mine, or a lower-level write cannot pass them either: no
    more live records or proposals past the hard limits, no active context
    over its hard size, and no second record in a project with the same text.
    """
    from .memory_project_writes import CONTEXT_HARD_TOKENS, POOL_HARD
    from .memory_proposals import HARD_CAP
    from .memory_retrieve import estimate_tokens

    after = _snapshot(root, index)
    problems = [f"{path}: {rule}" for path, rule in sorted(after.errors - before.errors)]
    for folder, count in sorted(after.live.items()):
        if count > POOL_HARD and count > before.live.get(folder, 0):
            problems.append(
                f"projects/{folder} would have {count} live records (limit {POOL_HARD})"
            )
    if after.proposals > HARD_CAP and after.proposals > before.proposals:
        problems.append(f"{after.proposals} proposals would be open (limit {HARD_CAP})")
    changed = set(_paths(op))
    for record in after.records:
        if record.path not in changed or not record.path.startswith("projects/"):
            continue
        body = _body(root / record.path)
        if record.path.endswith("/active-context.md"):
            if estimate_tokens(body) > CONTEXT_HARD_TOKENS:
                problems.append(f"{record.path} is over {CONTEXT_HARD_TOKENS} tokens")
            continue
        if record.path.endswith("/project.md") or record.status != "accepted" or not body:
            continue
        # Only a write that adds a copy counts: moving a record, or editing
        # one that already shared its text, makes nothing worse.
        key = (record.path.split("/")[1], body)
        if after.texts.get(key, 0) > max(1, before.texts.get(key, 0)):
            problems.append(f"{record.path} repeats the text of another record in its project")
    return problems


def _body(path: Path) -> str:
    """A record's text below its front matter."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), 0)
    return "\n".join(lines[end + 1 :]).strip()


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
        project_id=_project_id(root, project),
    )
    return _record(root, op)


def delete_project(root: Path, project: str, rel: str, *, client: str = "unknown") -> Op:
    relative = _project_path(project, rel)
    prior = _current(root, relative)
    if prior is None:
        raise MemoryVaultError(f"{relative} does not exist")
    op = Op(
        str(uuid.uuid4()),
        "delete",
        "project",
        relative,
        project,
        None,
        _sha(prior),
        client=client,
        project_id=_project_id(root, project),
    )
    return _record(root, op)


def forget_project(root: Path, project: str, *, client: str = "unknown") -> Op:
    folder = f"projects/{project}"
    if not SLUG.match(project):
        raise MemoryVaultError(f"invalid project folder: {project!r}")
    digest = _subtree_digest(root, folder)
    if digest is None:
        raise MemoryVaultError(f"{folder} has no memory to forget")
    op = Op(
        str(uuid.uuid4()),
        "forget",
        "project",
        folder,
        project,
        None,
        digest,
        client=client,
        project_id=_project_id(root, project),
    )
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
    if op.tier == "project" and op.project_id is not None and op.project is not None:
        # Folder names are chosen per machine; the ID is the identity. Two
        # machines that registered different repositories under one name
        # must not write into each other's project.
        owner = next(
            (e["id"] for e in _registry(root)["projects"] if e["folder"] == op.project), None
        )
        if owner != op.project_id:
            raise SyncConflict(f"projects/{op.project} now belongs to another project")
    if op.kind == "register":
        return _apply_register(root, op)
    if op.kind in {"untrack", "obsidian"}:
        return _apply_obsidian(root)
    if op.kind == "multi":
        return _apply_multi(root, op)
    if op.kind == "forget":
        current_digest = _subtree_digest(root, op.path)
        if current_digest is None:
            return "noop"
        if current_digest != op.expected:
            raise SyncConflict(f"{op.path} changed since it was forgotten")
        shutil.rmtree(root / op.path)
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


def _tracked_device_state(root: Path) -> list[str]:
    tracked = _git(root, "ls-files", "--", *DEVICE_STATE_DIRS).stdout
    return [line for line in tracked.splitlines() if line.strip()]


def gitignore_with_device_state(root: Path) -> bytes | None:
    """The .gitignore with the managed block, or None when it already has it."""
    current = (_current(root, ".gitignore") or b"").decode("utf-8")
    lines = current.splitlines()
    if (
        DEVICE_STATE_BLOCK in lines
        and all(entry in lines for entry in DEVICE_STATE_DIRS)
        and not any(entry in lines for entry in LEGACY_DEVICE_LINES)
    ):
        return None
    block = [DEVICE_STATE_BLOCK, *DEVICE_STATE_DIRS]
    kept = [line for line in lines if line not in (*block, *LEGACY_DEVICE_LINES)]
    while kept and not kept[-1].strip():
        kept.pop()
    return ("\n".join([*kept, "", *block] if kept else block) + "\n").encode("utf-8")


def _apply_obsidian(root: Path) -> str:
    """The vault's Obsidian setup (see memory_obsidian), keeping local device files.

    Recomputed from the current tree on every replay, so two machines doing it
    at once converge instead of conflicting. The older ``untrack`` operation,
    which did only the device-state part, replays through here too.
    """
    from . import memory_obsidian

    wrote = memory_obsidian.apply(root)
    return "applied" if wrote or _tracked_device_state(root) else "noop"


def setup_obsidian(root: Path, *, client: str = "agentbot") -> Op | None:
    """Queue the vault's Obsidian setup when a vault used with Obsidian needs it."""
    from . import memory_obsidian

    # This machine's own settings: written here, never committed.
    memory_obsidian.apply_device(root)
    tracked = _tracked_device_state(root)
    if not tracked and not (
        memory_obsidian.opened_in_obsidian(root) and memory_obsidian.changes(root)
    ):
        return None
    op = Op(str(uuid.uuid4()), "obsidian", "meta", ".", None, None, None, client=client)
    return _record(root, op)


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
        project_id=_project_id(root, project),
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


def _summary(op: Op) -> str:
    """What the commit changed, in words `git log` can show."""
    if op.kind == "untrack":
        return "untrack Obsidian per-device state"
    if op.kind == "obsidian":
        return "set up Obsidian views and settings"
    if op.kind != "multi":
        return f"{op.kind} {op.path}"
    items = json.loads(op.content or "[]")
    removed = [item for item in items if item["content"] is None]
    verb = "delete" if len(removed) == len(items) else "update" if not removed else "change"
    first = next((item["path"] for item in items if item["content"] is not None), None)
    first = first or (items[0]["path"] if items else op.path)
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    return f"{verb} {first}{more}"


def _paths(op: Op) -> list[str]:
    """The files (or, for forget, the folder) an operation writes in the work tree."""
    if op.kind == "multi":
        return [item["path"] for item in json.loads(op.content or "[]")]
    if op.kind in {"untrack", "obsidian"}:
        from .memory_obsidian import KIT_PATHS

        return list(KIT_PATHS)
    return [op.path]


def _in_head(root: Path, path: str) -> bool:
    return _git(root, "cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0


def _restore(root: Path, op: Op) -> None:
    """Put an operation's files back as HEAD has them. Device state is never touched."""
    for path in _paths(op):
        target = root / path
        if _in_head(root, path):
            _git(root, "checkout", "-q", "HEAD", "--", path)
        elif target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        # Folders the write created and left empty go too; Git ignores them,
        # but Obsidian would still show them.
        parent = target.parent
        while parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def _stage(root: Path, op: Op) -> Path:
    """The vault after this operation, in a private index. The user's index is not used."""
    handle, name = tempfile.mkstemp(prefix="index-", dir=_state(root))
    os.close(handle)
    index = Path(name)
    index.unlink()  # git builds it; an empty file is not a valid index
    _git(root, "read-tree", "HEAD", index=index)
    if op.kind == "forget":
        _git(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", op.path, index=index)
    elif op.kind in {"untrack", "obsidian"}:
        # Per-device files leave Git; this machine's copies stay on disk.
        untrack = list(DEVICE_STATE_DIRS)
        _git(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *untrack, index=index)
        for path in _paths(op):
            # One at a time: git refuses a whole batch if one path is ignored.
            if (root / path).is_file():
                _git(root, "add", "--", path, check=False, index=index)
    else:
        _git(root, "add", "-A", "--", *_paths(op), index=index)
    return index


def _refresh_index(root: Path, op: Op) -> None:
    """Bring the user's index in line with HEAD for this operation's paths only."""
    paths = _paths(op)
    if op.kind in {"untrack", "obsidian"}:
        paths += list(DEVICE_STATE_DIRS)
    for path in paths:
        # One at a time: a path in neither HEAD nor the index fails on its own.
        _git(root, "reset", "-q", "--", path, check=False)


def _commit(root: Path, op: Op, index: Path) -> str:
    message = (
        f"memory({op.tier}): {_summary(op)}\n\n"
        f"Agentbot-Op: {op.id}\nAgentbot-Tier: {op.tier}\nAgentbot-Client: {op.client}\n"
    )
    # Plumbing, not `git commit`: a user's git wrapper or commit hooks (for
    # example one that fetches first) must not change what the engine does,
    # and offline commits must work. The staged vault was validated first.
    tree = _git(root, "write-tree", index=index).stdout.strip()
    parent = _git(root, "rev-parse", "HEAD").stdout.strip()
    commit = _git(root, "commit-tree", tree, "-p", parent, "-m", message).stdout.strip()
    _git(root, "update-ref", "HEAD", commit, parent)
    _refresh_index(root, op)
    return commit


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
    with _locked(root):
        return _sync(root, remote)


def _sync(root: Path, remote: str) -> SyncResult:
    started = time.monotonic()
    result = SyncResult(state="synced")
    _require_clean(root)
    # The reset below would put back the remote's copy of a tracked layout
    # file, or of trash that older history still tracks; each machine keeps
    # its own. Read just before each reset, so an edit during the fetch is kept.
    device_state: dict[str, bytes] = {}
    branch = _branch(root)
    upstream = f"{remote}/{branch}"
    reset_to: list[str] = []
    try:
        _replay_and_push(root, remote, branch, upstream, result, reset_to, device_state)
    finally:
        _keep_device_state(root, device_state, ["HEAD", *reset_to])
    result.seconds = time.monotonic() - started
    return result


def _replay_and_push(
    root: Path,
    remote: str,
    branch: str,
    upstream: str,
    result: SyncResult,
    reset_to: list[str],
    device_state: dict[str, bytes],
) -> None:
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        result.attempts = attempt
        if _git(root, "fetch", "-q", remote, check=False).returncode != 0:
            result.state = "offline"
            return
        pending = _load(root, "pending.json")
        has_upstream = (
            _git(root, "rev-parse", "--verify", "-q", upstream, check=False).returncode == 0
        )
        if has_upstream and not _local_commits_are_ours(root, upstream, pending):
            raise MemoryVaultError(
                "the vault has local commits Agentbot did not make; push them by hand first"
            )
        if has_upstream:
            tip = _git(root, "rev-parse", upstream).stdout.strip()
            # The first reading of a file is this machine's: a later attempt
            # would read what the previous reset wrote.
            for path, data in _device_files(root, tip).items():
                device_state.setdefault(path, data)
            reset_to.append(tip)
            _git(root, "reset", "-q", "--hard", tip)
        before = _snapshot(root)
        remaining: list[dict[str, Any]] = []
        for item in pending:
            op = Op(**item)
            # Pushed by an earlier run that stopped before it cleared the
            # queue: replaying it again could only conflict with itself.
            if has_upstream and _in_history(root, op.id, upstream):
                result.noops.append(op.id)
                continue
            try:
                outcome = _apply(root, op)
            except SyncConflict as error:
                _restore(root, op)
                _preserve(root, op, str(error), result)
                continue
            if outcome == "applied":
                index = _stage(root, op)
                try:
                    problems = _new_errors(root, before, index, op)
                    if problems:
                        _restore(root, op)
                        reason = "it would make the vault invalid (" + "; ".join(problems) + ")"
                        _preserve(root, op, reason, result)
                        continue
                    _commit(root, op, index)
                finally:
                    index.unlink(missing_ok=True)
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
            return
        # Someone pushed first: keep the queue, fetch, and replay again.
        result.applied.clear()
        result.noops.clear()
        _save(root, "pending.json", remaining)
    result.state = "retry-exhausted"


DEVICE_FILE_LIMIT = 500
DEVICE_BYTES_LIMIT = 16 * 1024 * 1024


def _device_files(root: Path, tip: str) -> dict[str, bytes]:
    """This machine's copy of every per-device file a reset to ``tip`` can touch.

    Only paths Git tracks, here or at the tip: a reset leaves untracked files
    alone, so plugin folders and the rest never count against the limits. All
    of them or none: past the limits the sync stops before the reset, rather
    than keep some and lose the rest.
    """
    tracked = set()
    for rev in ("HEAD", tip):
        listed = _git(
            root, "ls-tree", "-r", "-z", "--name-only", rev, "--", *DEVICE_STATE_DIRS, check=False
        ).stdout
        tracked.update(path for path in listed.split("\0") if path)
    found: dict[str, bytes] = {}
    total = 0
    for relative in sorted(tracked):
        path = root / relative
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            total += len(data)
            found[relative] = data
    if len(found) > DEVICE_FILE_LIMIT or total > DEVICE_BYTES_LIMIT:
        raise MemoryVaultError(
            f"the vault's history still tracks {len(found)} Obsidian files ({total} bytes), more "
            "than sync can keep safely; untrack .obsidian/, views/ and .trash/ by hand first"
        )
    return found


def _keep_device_state(root: Path, saved: dict[str, bytes], revs: list[str]) -> None:
    """Put back a per-device file only where Git changed it, never over a newer edit."""
    for path, data in saved.items():
        target = root / path
        if target.is_symlink():
            continue
        now = target.read_bytes() if target.is_file() else None
        if now == data:
            continue
        if now is not None:
            # Changed since sync started. Git did it only if the file is now
            # exactly what one of the checked-out revisions tracks; otherwise
            # it is an edit made meanwhile, and it stays.
            written = _git(root, "hash-object", "--", path, check=False).stdout.strip()
            blobs = {
                _git(
                    root, "rev-parse", "-q", "--verify", f"{rev}:{path}", check=False
                ).stdout.strip()
                for rev in revs
            }
            if written not in blobs - {""}:
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


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
    with _locked(root):
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
