"""Find and remove installed skills that the manifest does not want.

Two things put a skill in ``~/.agents/skills`` that you did not ask for:

* a ``skills: all`` source whose upstream repository ships more than its
  catalog -- its own CLI test fixtures, for example;
* a source that was removed or disabled after its skills were installed.

Neither is visible to Doctor's manual-skill check, because that check treats
anything present in the lock as managed. The lock records what *was* installed;
the manifest declares what *should* be. This module reconciles the two.

Classification, in order:

``excluded``   the owning source installs it but the manifest excludes it
``orphaned``   pinned to a source that is no longer active in the manifest
``stale-pin``  in the lock with no directory on disk
``manual``     a directory with no lock entry -- user-placed, never removed
               unless explicitly requested
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .paths import AgentbotPaths
from .skills_sources import SkillsSourcesConfig

REMOVABLE_BY_DEFAULT = frozenset({"excluded", "orphaned", "stale-pin"})


@dataclass(frozen=True)
class PruneCandidate:
    name: str
    reason: str
    detail: str
    directory: Path | None
    locked: bool

    @property
    def removable_by_default(self) -> bool:
        return self.reason in REMOVABLE_BY_DEFAULT


@dataclass(frozen=True)
class PruneReport:
    candidates: tuple[PruneCandidate, ...]
    removed: tuple[str, ...] = ()
    applied: bool = False
    blocked_reason: str | None = None

    @property
    def removable(self) -> tuple[PruneCandidate, ...]:
        return tuple(item for item in self.candidates if item.removable_by_default)

    @property
    def manual(self) -> tuple[PruneCandidate, ...]:
        return tuple(item for item in self.candidates if item.reason == "manual")

    @property
    def manual_left(self) -> tuple[PruneCandidate, ...]:
        """Manual candidates this run did not remove.

        `manual` is every manual candidate, removed or not. Naming one
        explicitly removes it even though manual skills are not removed by
        default, so the two differ exactly when the operator asked for one by
        name -- which is what the menu does. Reporting `manual` after applying
        told them six skills were "left in place" on the line under the one
        saying those same six had been removed.
        """
        removed = set(self.removed)
        return tuple(item for item in self.manual if item.name not in removed)


@dataclass(frozen=True)
class _LockState:
    status: str
    skills: dict[str, dict]
    detail: str | None = None


def _read_lock(lock_file: Path) -> _LockState:
    if not lock_file.exists() and not lock_file.is_symlink():
        return _LockState("absent", {})
    if not lock_file.is_file():
        return _LockState("invalid", {}, "path is not a regular file")
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _LockState("invalid", {}, str(error))
    if not isinstance(payload, dict):
        return _LockState("invalid", {}, "root must be a JSON object")
    skills = payload.get("skills")
    if not isinstance(skills, dict):
        return _LockState("invalid", {}, "skills must be a JSON object")
    invalid_entries = sorted(name for name, entry in skills.items() if not isinstance(entry, dict))
    if invalid_entries:
        return _LockState(
            "invalid",
            {},
            f"skill entries must be JSON objects: {', '.join(invalid_entries)}",
        )
    return _LockState("valid", skills)


def _installed_skill_dirs(skills_home: Path) -> dict[str, Path]:
    if not skills_home.is_dir():
        return {}
    return {
        entry.name: entry
        for entry in sorted(skills_home.iterdir())
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }


def plan_prune(paths: AgentbotPaths, config: SkillsSourcesConfig) -> PruneReport:
    """Classify every installed skill and lock pin against the manifest."""
    lock_state = _read_lock(paths.global_skill_lock)
    if lock_state.status == "invalid":
        return PruneReport(
            candidates=(),
            blocked_reason=f"invalid global skill lock: {lock_state.detail}",
        )
    lock = lock_state.skills
    installed = _installed_skill_dirs(paths.agents_skills_home)

    active_repos = {source.repo: source for source in config.active_sources() if source.repo}
    declared_names = {
        name
        for source in config.active_sources()
        for name in source.skills
        if name != "*"
    }
    candidates: list[PruneCandidate] = []

    for name, directory in installed.items():
        entry = lock.get(name)
        if entry is None:
            if name in declared_names:
                continue
            if name == "graphify" and (directory / ".graphify_version").is_file():
                continue
            candidates.append(
                PruneCandidate(
                    name=name,
                    reason="manual",
                    detail="on disk, not in the lock; user-placed",
                    directory=directory,
                    locked=False,
                )
            )
            continue

        repo = entry.get("source")
        source = active_repos.get(repo) if isinstance(repo, str) else None
        if source is None:
            candidates.append(
                PruneCandidate(
                    name=name,
                    reason="orphaned",
                    detail=f"pinned to {repo or 'an unknown source'}, no active manifest source",
                    directory=directory,
                    locked=True,
                )
            )
            continue

        if source.excludes(name):
            candidates.append(
                PruneCandidate(
                    name=name,
                    reason="excluded",
                    detail=f"{source.id} installs it, manifest excludes it",
                    directory=directory,
                    locked=True,
                )
            )

    for name in sorted(set(lock) - set(installed)):
        repo = lock[name].get("source")
        candidates.append(
            PruneCandidate(
                name=name,
                reason="stale-pin",
                detail=f"pinned to {repo or 'an unknown source'}, no directory on disk",
                directory=None,
                locked=True,
            )
        )

    candidates.sort(key=lambda item: (item.reason, item.name))
    return PruneReport(candidates=tuple(candidates))


def enforce_exclusions(paths: AgentbotPaths, config: SkillsSourcesConfig) -> tuple[str, ...]:
    """Remove skills the manifest excludes, right after an install.

    `exclude:` has to mean "never present", not "prunable later". The Skills
    CLI installs everything a `skills: all` source publishes and has no
    exclusion flag, so the exclusion is applied here instead: immediately after
    install, before the Claude and Codex bridges ever see the directory.

    Without this, `exclude:` was a treadmill -- every install re-added and
    re-pinned the excluded skills, and only a separate `skills prune` removed
    them again.
    """
    report = plan_prune(paths, config)
    if report.blocked_reason is not None:
        raise ValueError(report.blocked_reason)
    excluded = PruneReport(
        candidates=tuple(item for item in report.candidates if item.reason == "excluded")
    )
    if not excluded.candidates:
        return ()
    return apply_prune(paths, excluded).removed


def _bridge_is_owned(link: Path, owned_root: Path) -> bool:
    """Return whether a bridge symlink points into Agentbot's skill store."""
    if not link.is_symlink():
        return False
    try:
        target = link.resolve()
    except OSError:
        return False
    try:
        target.relative_to(owned_root.resolve())
    except ValueError:
        # Points somewhere else: belongs to the user or another installer.
        return False
    return True


def _prepare_lock_update(
    lock_file: Path, removed: list[str], *, required: bool
) -> Path | None:
    """Write the updated lock beside the destination without replacing it."""
    if not lock_file.exists() and not lock_file.is_symlink():
        if required:
            raise ValueError("skill lock disappeared after the prune plan was created")
        return None
    if not lock_file.is_file():
        raise ValueError(f"invalid skill lock: {lock_file} is not a regular file")
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid skill lock: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), dict):
        raise ValueError("invalid skill lock: expected an object with a skills object")
    for name in removed:
        payload["skills"].pop(name, None)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{lock_file.name}.", dir=lock_file.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(lock_file.stat().st_mode & 0o777)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _stage_path(path: Path, staging_roots: dict[Path, Path]) -> tuple[Path, Path]:
    """Move one path into a private directory on the same filesystem."""
    staging_root = staging_roots.get(path.parent)
    if staging_root is None:
        staging_root = Path(
            tempfile.mkdtemp(prefix=".agentbot-prune-", dir=path.parent)
        )
        staging_roots[path.parent] = staging_root
    staged = staging_root / str(len(tuple(staging_root.iterdir())))
    path.rename(staged)
    return path, staged


def _restore_staged(staged_paths: list[tuple[Path, Path]]) -> None:
    for original, staged in reversed(staged_paths):
        if staged.exists() or staged.is_symlink():
            staged.rename(original)


def apply_prune(
    paths: AgentbotPaths,
    report: PruneReport,
    *,
    include_manual: bool = False,
    manual_names: tuple[str, ...] | None = None,
    candidate_names: tuple[str, ...] | None = None,
) -> PruneReport:
    """Remove the classified skills: directory, lock pin, and bridge links."""
    if report.blocked_reason is not None:
        raise ValueError(report.blocked_reason)
    selectors = sum(value is not None for value in (manual_names, candidate_names)) + int(
        include_manual
    )
    if selectors > 1:
        raise ValueError(
            "include_manual, manual_names, and candidate_names cannot be combined"
        )

    if candidate_names is not None:
        candidates_by_name = {item.name: item for item in report.candidates}
        invalid = sorted(set(candidate_names) - set(candidates_by_name))
        if invalid:
            raise ValueError(f"not prune candidates: {', '.join(invalid)}")
        targets = [candidates_by_name[name] for name in dict.fromkeys(candidate_names)]
    elif manual_names is not None:
        manual_by_name = {item.name: item for item in report.manual}
        invalid = sorted(set(manual_names) - set(manual_by_name))
        if invalid:
            raise ValueError(f"not removable manual skills: {', '.join(invalid)}")
        targets = [manual_by_name[name] for name in dict.fromkeys(manual_names)]
    else:
        targets = [item for item in report.candidates if item.removable_by_default]
    if include_manual:
        targets.extend(report.manual)
    if not targets:
        return PruneReport(candidates=report.candidates, removed=(), applied=True)

    removed = [item.name for item in targets]
    lock_file = paths.global_skill_lock
    prepared_lock = _prepare_lock_update(
        lock_file, removed, required=any(item.locked for item in targets)
    )
    store = paths.agents_skills_home
    staging_roots: dict[Path, Path] = {}
    staged_paths: list[tuple[Path, Path]] = []
    committed = False
    try:
        for item in targets:
            for bridge in (
                paths.claude_skills_home / item.name,
                paths.codex_home / "skills" / item.name,
            ):
                if _bridge_is_owned(bridge, store):
                    staged_paths.append(_stage_path(bridge, staging_roots))
            if item.directory is not None and item.directory.is_dir():
                staged_paths.append(_stage_path(item.directory, staging_roots))
        if prepared_lock is not None:
            os.replace(prepared_lock, lock_file)
        committed = True
    except BaseException:
        _restore_staged(staged_paths)
        for staging_root in staging_roots.values():
            staging_root.rmdir()
        raise
    finally:
        if not committed and prepared_lock is not None:
            prepared_lock.unlink(missing_ok=True)

    for staging_root in staging_roots.values():
        shutil.rmtree(staging_root)

    return PruneReport(
        candidates=report.candidates, removed=tuple(sorted(removed)), applied=True
    )
