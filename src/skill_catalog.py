from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .command_runner import CommandRunner
from .skills_sources import SkillsSourcesConfig, source_clone_url


@dataclass(frozen=True)
class SourceCatalog:
    source_id: str
    repo: str
    revision: str
    skills: tuple[str, ...]


class StaleSourceCatalogError(RuntimeError):
    """Raised before mutation when a source changed after update preview."""


CloneSource = Callable[[str, Path], None]
RevisionReader = Callable[[Path], str]


#: Concurrent shallow clones while building an update plan. Twelve sources
#: cloned one after another is twelve network round-trips in a row -- about
#: thirty seconds before the plan could be shown, and the operator was looking
#: at nothing for all of it. They do not contend for anything but bandwidth:
#: each clones into its own directory and git is a subprocess, so the work is
#: waiting, not computing.
#:
#: Set AGENTBOT_CATALOG_WORKERS=1 to go back to one at a time.
DEFAULT_CATALOG_WORKERS = 8


def _catalog_workers(count: int) -> int:
    configured = os.environ.get("AGENTBOT_CATALOG_WORKERS", "")
    limit = int(configured) if configured.isdigit() and int(configured) > 0 else None
    return max(1, min(limit or DEFAULT_CATALOG_WORKERS, count))


def _clone_catalogs(
    sources: list,
    root: Path,
    clone: CloneSource,
    revision: RevisionReader,
    progress: Callable[[str, str], None] | None = None,
) -> tuple[SourceCatalog, ...]:
    """Clone every source into `root` and describe each one.

    Both passes of an update do exactly this -- the plan to say what would
    change, the apply to verify that it still holds -- and they must describe a
    source identically, or the apply reports a source as changed because the two
    read it differently. One implementation is how that is guaranteed rather
    than hoped for.

    Returned in manifest order whatever order the clones finish in: the apply
    compares its tuple against the plan's, and an order that depended on network
    timing would fail that comparison at random.
    """
    if not sources:
        return ()

    def inspect(source) -> SourceCatalog:
        checkout = root / source.id
        clone(source.repo, checkout)
        catalog = SourceCatalog(
            source.id,
            source.repo,
            revision(checkout),
            discover_checkout_skills(checkout),
        )
        if progress is not None:
            progress(source.id, source.repo)
        return catalog

    workers = _catalog_workers(len(sources))
    if workers == 1:
        return tuple(inspect(source) for source in sources)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(inspect, source): source for source in sources}
        found = {futures[future].id: future.result() for future in as_completed(futures)}
    return tuple(found[source.id] for source in sources)


def discover_remote_catalogs(
    config: SkillsSourcesConfig,
    *,
    clone_source: CloneSource | None = None,
    revision_reader: RevisionReader | None = None,
    runner: CommandRunner | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> tuple[SourceCatalog, ...]:
    command_runner = runner or CommandRunner()
    clone = clone_source or (lambda repo, path: _clone_source(repo, path, runner=command_runner))
    revision = revision_reader or (lambda path: _revision(path, runner=command_runner))
    sources = [source for source in config.active_sources() if source.repo is not None]

    with tempfile.TemporaryDirectory(prefix="agentbot-update-plan-") as temporary:
        return _clone_catalogs(sources, Path(temporary), clone, revision, progress)


@contextmanager
def verified_source_checkouts(
    config: SkillsSourcesConfig,
    expected: tuple[SourceCatalog, ...],
    *,
    clone_source: CloneSource | None = None,
    revision_reader: RevisionReader | None = None,
    runner: CommandRunner | None = None,
):
    command_runner = runner or CommandRunner()
    clone = clone_source or (lambda repo, path: _clone_source(repo, path, runner=command_runner))
    revision = revision_reader or (lambda path: _revision(path, runner=command_runner))
    expected_by_id = {catalog.source_id: catalog for catalog in expected}
    sources = [source for source in config.active_sources() if source.repo is not None]
    with tempfile.TemporaryDirectory(prefix="agentbot-update-apply-") as temporary:
        root = Path(temporary)
        # Was a serial loop while the plan's pass was already concurrent, so the
        # cheaper half of an update cost roughly three times the dearer one:
        # 23.2s against 9.3s over the same twelve sources, measured 2026-09-12.
        current_tuple = _clone_catalogs(sources, root, clone, revision)
        checkouts: dict[str, Path] = {
            catalog.source_id: root / catalog.source_id for catalog in current_tuple
        }
        if current_tuple != expected or set(checkouts) != set(expected_by_id):
            raise StaleSourceCatalogError(
                "Upstream skill sources changed after preview; preview again before applying."
            )
        yield checkouts


def _clone_source(repo: str, destination: Path, *, runner: CommandRunner | None = None) -> None:
    clone_url = source_clone_url(repo)
    if clone_url is None:
        raise ValueError(f"not a supported owner/repository source: {repo!r}")
    timeout = int(os.environ.get("AGENTBOT_GITHUB_CLONE_TIMEOUT_SECONDS", "300"))
    completed = (runner or CommandRunner()).run(
        ["git", "clone", "--depth=1", clone_url, str(destination)],
        timeout_seconds=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"failed to inspect skill source {repo!r}: {completed.detail()}"
        )


def _revision(checkout: Path, *, runner: CommandRunner | None = None) -> str:
    completed = (runner or CommandRunner()).run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        timeout_seconds=30,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"unable to read source revision for {checkout.name!r}")
    return completed.stdout.strip()


def skill_name_from_file(skill_file: Path) -> str:
    content = skill_file.read_text(encoding="utf-8")
    if content.startswith("---"):
        closing = content.find("\n---", 3)
        if closing != -1:
            frontmatter = content[3:closing]
            match = re.search(
                r"^name:\s*([^#\n]+)", frontmatter, flags=re.MULTILINE
            )
            if match:
                return match.group(1).strip().strip("\"'")
    return skill_file.parent.name


def discover_checkout_skills(checkout: Path) -> tuple[str, ...]:
    if not checkout.is_dir():
        return ()
    names = {
        skill_name_from_file(path)
        for path in checkout.rglob("SKILL.md")
        if path.is_file()
    }
    return tuple(sorted(name for name in names if name))
