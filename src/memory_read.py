"""Consistent working-tree reads without creating or recovering writer state."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

from . import memory, memory_sync

T = TypeVar("T")
ATTEMPTS = 3
LOCK_WAIT = 0.25


class MemoryReadUnavailable(memory.MemoryVaultError):
    """A consistent local snapshot cannot currently be read."""


def _identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@contextlib.contextmanager
def _reader_lock(state: Path) -> Iterator[bool]:
    if state.is_symlink():
        raise MemoryReadUnavailable("memory sync state is not a safe directory")
    try:
        descriptor = os.open(state / "lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        # A first writer may create its lock after this point. The snapshot
        # identity includes that lock, so such a read is retried.
        yield False
        return
    except OSError:
        raise MemoryReadUnavailable("memory reader cannot inspect the writer lock") from None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MemoryReadUnavailable("memory writer lock is not a regular file")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MemoryReadUnavailable("a memory writer is active; retry recall") from None
                time.sleep(0.01)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _fingerprint(root: Path, state: Path) -> tuple:
    if _identity(state / memory_sync.WRITING) is not None:
        raise MemoryReadUnavailable(
            "an interrupted memory write needs writer recovery before recall"
        )
    pending_identity = _identity(state / "pending.json")
    if pending_identity is not None:
        try:
            pending = json.loads(
                memory.read_bounded(state.parent, f"{state.name}/pending.json", 16 * 1024 * 1024)
            )
        except (OSError, ValueError, memory._Unsafe):
            raise MemoryReadUnavailable("memory operation state cannot be safely read") from None
        if not isinstance(pending, list) or any(
            not isinstance(op, dict) or not op.get("commit") for op in pending
        ):
            raise MemoryReadUnavailable(
                "an uncommitted memory operation needs writer recovery before recall"
            )
    head = memory_sync._git(root, "rev-parse", "HEAD", check=False)
    files = tuple(
        (relative, _identity(root / relative))
        for relative in memory._listed_paths(root, None, others=True)
        if not relative.startswith(memory.IGNORED_PREFIXES)
    )
    return (
        head.stdout if head.returncode == 0 else None,
        _identity(state / "lock"),
        pending_identity,
        files,
    )


def consistent(root: Path, read: Callable[[], T]) -> T:
    git_dir = Path(memory_sync._git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    state = git_dir / memory_sync.STATE_DIR
    for _ in range(ATTEMPTS):
        with _reader_lock(state) as locked:
            before = _fingerprint(root, state)
            if not locked and _identity(state / "lock") is not None:
                continue
            try:
                result = read()
            except (OSError, memory.MemoryVaultError):
                if before == _fingerprint(root, state):
                    raise
                continue
            if before == _fingerprint(root, state):
                return result
    raise MemoryReadUnavailable("memory changed during recall; retry for a consistent snapshot")
