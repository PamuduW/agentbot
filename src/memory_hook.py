"""The owned pre-commit and pre-push hooks that scan the vault before Git acts.

The vault is private, so no hosted secret scanning sits behind it; these hooks
are its only scan before a commit or push. They run ``memory validate`` over
the working tree, which covers every tracked, unignored, and draft Markdown
file. They do not scan ``.obsidian/``: plugin settings such as
``plugins/*/data.json`` are committed and unscanned.

A hook is Agentbot's only when it carries MARKER. Any other file at a hook path
is left alone and reported. The hooks directory is Git's effective one
(``core.hooksPath`` included), and it must live inside the vault's own Git
directory, so a shared hooks path is never written. Hooks are an accident
guard: ``git commit --no-verify`` bypasses them.
"""

from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import MemoryVaultError, _git_text

MARKER = "agentbot-memory-hook v1"
HOOKS = ("pre-commit", "pre-push")
SCANNER_GAP = (
    ".obsidian/ is not scanned: plugin settings such as plugins/*/data.json are committed "
    "without a secret scan. Check what a plugin stores before installing it."
)


def render_hook(agentbot_home: Path) -> str:
    installer = shlex.quote(str(agentbot_home / "install.sh"))
    return f"""#!/bin/sh
# {MARKER}: managed by Agentbot. Remove with: agentbot memory hook remove --yes
# Validates and secret-scans this vault's Markdown before Git continues.
# {SCANNER_GAP}
root="$(git rev-parse --show-toplevel)" || exit 1
if [ ! -x {installer} ]; then
	echo "agentbot-memory-hook: Agentbot is not at {installer}; refusing." >&2
	echo "agentbot-memory-hook: reinstall the hook, or bypass once with --no-verify." >&2
	exit 1
fi
unset AGENTBOT_MEMORY_ROOT
AGENTBOT_MEMORY_DIR="$root" exec {installer} memory validate
"""


@dataclass
class HookState:
    hooks_dir: Path | None
    states: dict[str, str] = field(default_factory=dict)  # absent, owned, stale, unowned
    problem: str | None = None
    applied: list[str] = field(default_factory=list)


def hooks_directory(root: Path) -> Path:
    git_dir = _git_text(root, "rev-parse", "--absolute-git-dir")
    hooks = _git_text(root, "rev-parse", "--git-path", "hooks")
    if git_dir is None or hooks is None:
        raise MemoryVaultError("git could not resolve the vault's hooks directory")
    path = Path(hooks) if os.path.isabs(hooks) else root / hooks
    resolved, owner = path.resolve(), Path(git_dir).resolve()
    if resolved != owner and owner not in resolved.parents:
        raise MemoryVaultError(
            "core.hooksPath points outside this vault's Git directory; "
            "Agentbot will not write a shared hooks directory"
        )
    return resolved


def inspect(root: Path, agentbot_home: Path) -> HookState:
    try:
        directory = hooks_directory(root)
    except MemoryVaultError as error:
        return HookState(hooks_dir=None, problem=str(error))
    wanted = render_hook(agentbot_home)
    state = HookState(hooks_dir=directory)
    for name in HOOKS:
        state.states[name] = _classify(directory / name, wanted)
    return state


def _classify(path: Path, wanted: str) -> str:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISREG(mode):
        return "unowned"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "unowned"
    lines = text.splitlines()
    if len(lines) < 2 or MARKER not in lines[1]:
        return "unowned"
    return "owned" if text == wanted and mode & stat.S_IXUSR else "stale"


def install(root: Path, agentbot_home: Path) -> HookState:
    state = inspect(root, agentbot_home)
    if state.hooks_dir is None:
        return state
    state.hooks_dir.mkdir(parents=True, exist_ok=True)
    wanted = render_hook(agentbot_home)
    for name, current in state.states.items():
        if current in {"absent", "stale"}:
            # Checked again at the last moment: a hook that appeared since
            # inspect() is somebody else's.
            if _classify(state.hooks_dir / name, wanted) not in {"absent", "stale"}:
                state.states[name] = "unowned"
                continue
            _write(state.hooks_dir, name, wanted.encode("utf-8"))
            state.applied.append(name)
            state.states[name] = "owned"
    return state


def remove(root: Path, agentbot_home: Path) -> HookState:
    state = inspect(root, agentbot_home)
    if state.hooks_dir is None:
        return state
    for name, current in state.states.items():
        if current in {"owned", "stale"}:
            (state.hooks_dir / name).unlink()
            state.applied.append(name)
            state.states[name] = "absent"
    return state


def _write(directory: Path, name: str, content: bytes) -> None:
    temporary = directory / f".{name}.agentbot.tmp"
    with open(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o755), "wb"
    ) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    # Replacing is safe here: the target is absent or already carries MARKER,
    # and inspect() refuses anything else before this runs.
    os.replace(temporary, directory / name)


def hook_json(state: HookState) -> dict[str, Any]:
    return {
        "hooks_dir": str(state.hooks_dir) if state.hooks_dir else None,
        "hooks": state.states,
        "applied": state.applied,
        "problem": state.problem,
        "scanner_gap": SCANNER_GAP,
    }
