"""Locate the dotfiles-shared checkout.

The Bash side resolves this in scripts/lib/shared_resolve.sh. This is the same
resolution for the Python side, which cannot source a shell file, and the two
must stay in step: same order, same CONTRACT revision, same failure text.

Resolution order, first valid checkout wins:

1. ``DOTFILES_SHARED_ROOT``  already resolved by the shell entrypoint
2. ``DOTFILES_SHARED_DIR``   an operator naming the checkout outright
3. ``<parent of this repo>/dotfiles-shared``
4. ``~/dotfiles-shared``
"""

from __future__ import annotations

import os
from pathlib import Path

#: The CONTRACT revision this repository is written against. It is raised
#: whenever the pairing changes -- including when this side starts requiring a
#: module the shared checkout did not used to carry, which is a breaking
#: combination even though the shared side only gained something.
CONTRACT_REQUIRED = 2

URL = "https://github.com/PamuduW/dotfiles-shared"


class SharedCheckoutError(RuntimeError):
    """Raised when no usable dotfiles-shared checkout is available."""


def _is_checkout(path: Path) -> bool:
    return (path / "CONTRACT").is_file() and (
        path / "scripts" / "lib" / "shared" / "tui" / "colors.sh"
    ).is_file()


def _candidates(repo_root: Path) -> list[Path]:
    found: list[Path] = []
    for name in ("DOTFILES_SHARED_ROOT", "DOTFILES_SHARED_DIR"):
        value = os.environ.get(name)
        if value:
            found.append(Path(value))
    found.append(repo_root.parent / "dotfiles-shared")
    found.append(Path.home() / "dotfiles-shared")
    return found


def shared_root(repo_root: Path) -> Path:
    """Return the dotfiles-shared checkout, or raise with the fix."""
    candidates = _candidates(repo_root)
    for candidate in candidates:
        if not _is_checkout(candidate):
            continue
        found = (candidate / "CONTRACT").read_text(encoding="utf-8").strip()
        # At least, not exactly -- the Bash resolver's rule, for the same
        # reason. A raise means a consumer started needing something the shared
        # tree gained, so a shared checkout ahead of this one is a superset and
        # safe. Behind is the unsafe direction.
        if not found.isdigit():
            raise SharedCheckoutError(
                f"dotfiles-shared at {candidate} has an unreadable CONTRACT. "
                f"Update the shared checkout: git -C {candidate} pull"
            )
        if int(found) < CONTRACT_REQUIRED:
            raise SharedCheckoutError(
                f"dotfiles-shared at {candidate} is CONTRACT {found}, older than the "
                f"{CONTRACT_REQUIRED} this repository needs. "
                f"Update the shared checkout: git -C {candidate} pull"
            )
        return candidate.resolve()
    looked = "\n    ".join(str(path) for path in candidates)
    raise SharedCheckoutError(
        "no dotfiles-shared checkout found. Looked in:\n    "
        f"{looked}\n  Clone it beside this repository, then rerun:\n"
        f"    git clone {URL} {repo_root.parent / 'dotfiles-shared'}"
    )


def shared_python_path(repo_root: Path) -> Path:
    """The directory holding the shared Python modules."""
    return shared_root(repo_root) / "scripts" / "lib" / "shared" / "python"
