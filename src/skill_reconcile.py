from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .atomic_io import write_text_atomic
from .command_runner import CommandRunner
from .skill_catalog import skill_name_from_file
from .skills_sources import SkillsSourcesConfig


@dataclass(frozen=True)
class ManifestChange:
    source_id: str
    skill: str
    action: str

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.skill}:{self.action}"


@dataclass(frozen=True)
class SkillReconcilePlan:
    updates: tuple[str, ...]
    wildcard_additions: tuple[str, ...]
    wildcard_removals: tuple[str, ...]
    explicit_missing: tuple[str, ...]
    explicit_discovered: tuple[str, ...]
    manifest_changes: tuple[ManifestChange, ...]


class ReconcileError(RuntimeError):
    """Raised when a reconciliation cannot be safely applied."""


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    changed_paths: tuple[Path, ...]
    removed_skills: tuple[str, ...]
    added_skills: tuple[str, ...]
    backup_path: Path | None = None
    message: str = ""
    updated_skills: tuple[str, ...] = ()
    workspace_report: object | None = None


def _lock_skills(lock: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(lock, Mapping):
        return {}
    skills = lock.get("skills", {})
    return skills if isinstance(skills, Mapping) else {}


def _source_owned_lock_names(lock: Mapping[str, Any], repo: str | None) -> set[str]:
    if not repo:
        return set()
    owned: set[str] = set()
    for name, entry in _lock_skills(lock).items():
        if isinstance(entry, Mapping) and entry.get("source") == repo:
            owned.add(str(name))
    return owned


def build_reconcile_plan(
    config: SkillsSourcesConfig,
    *,
    discovered: Mapping[str, Iterable[str]],
    lock: Mapping[str, Any] | None = None,
) -> SkillReconcilePlan:
    """Compute source-owned deltas without writing manifests, locks, or links."""
    wildcard_additions: set[str] = set()
    wildcard_removals: set[str] = set()
    explicit_missing: set[str] = set()
    explicit_discovered: set[str] = set()
    manifest_changes: list[ManifestChange] = []
    updates: set[str] = set()

    for source in config.active_sources():
        available = {
            str(name)
            for name in discovered.get(source.id, ())
            if not source.excludes(str(name))
        }
        if source.skills == ["*"]:
            owned = _source_owned_lock_names(lock or {}, source.repo)
            additions = available - owned
            removals = owned - available
            wildcard_additions.update(additions)
            wildcard_removals.update(removals)
            if additions or removals:
                updates.add(source.id)
            continue

        selected = set(source.skills)
        missing = selected - available
        newly_discovered = available - selected
        explicit_missing.update(missing)
        explicit_discovered.update(newly_discovered)
        if missing or newly_discovered:
            updates.add(source.id)
        manifest_changes.extend(
            ManifestChange(source_id=source.id, skill=skill, action="remove")
            for skill in sorted(missing)
        )

    return SkillReconcilePlan(
        updates=tuple(sorted(updates)),
        wildcard_additions=tuple(sorted(wildcard_additions)),
        wildcard_removals=tuple(sorted(wildcard_removals)),
        explicit_missing=tuple(sorted(explicit_missing)),
        explicit_discovered=tuple(sorted(explicit_discovered)),
        manifest_changes=tuple(sorted(manifest_changes, key=lambda change: change.key)),
    )


def _lock_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 3, "skills": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReconcileError(f"unable to read global skill lock {path}: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("skills", {}), dict):
        raise ReconcileError(f"global skill lock {path} must contain an object skills map")
    return value


def _approved_lock_mutations(
    config: SkillsSourcesConfig,
    plan: SkillReconcilePlan,
    lock: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    lock_skills = _lock_skills(lock)
    active_repos = {source.repo for source in config.active_sources() if source.repo}
    owned_by_name = {
        str(name): str(entry.get("source"))
        for name, entry in lock_skills.items()
        if isinstance(entry, Mapping) and entry.get("source")
    }
    candidates = set(plan.wildcard_removals) | {
        change.skill
        for change in plan.manifest_changes
        if change.action == "remove"
    }
    removals = {
        skill
        for skill in candidates
        if owned_by_name.get(skill) in active_repos
    }
    return set(plan.wildcard_additions), removals


def _checkout_dirs(checkout: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not checkout.is_dir():
        return result
    for skill_file in checkout.rglob("SKILL.md"):
        if skill_file.is_file():
            result.setdefault(skill_name_from_file(skill_file), skill_file.parent)
    return result


def _manifest_after_removals(path: Path, removals: set[tuple[str, str]]) -> str:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ReconcileError(f"unable to read skills manifest {path}: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ReconcileError(f"skills manifest {path} must contain a sources list")
    for source in raw["sources"]:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id", ""))
        selected = source.get("skills")
        if not isinstance(selected, list):
            continue
        source["skills"] = [
            skill for skill in selected if (source_id, str(skill)) not in removals
        ]
    return yaml.safe_dump(raw, sort_keys=False, default_flow_style=False)


def _canonical_after_removals(path: Path, skills: set[str]) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReconcileError(f"unable to read canonical skill table {path}: {error}") from error
    lines = content.splitlines(keepends=True)
    has_skill_table = any(re.search(r"\|\s*`[^`]+`\s*\|", line) for line in lines)
    if not has_skill_table:
        return content

    changed = False
    output: list[str] = []
    for line in lines:
        if any(re.search(rf"\|\s*`{re.escape(skill)}`\s*\|", line) for skill in skills):
            changed = True
            continue
        output.append(line)
    return "".join(output) if changed else content


def _snapshot(paths: Iterable[Path], root: Path) -> Path:
    # Keep rollback artifacts outside the repository. Failed reconciliations
    # retain their backup for recovery, so placing it under ``root`` would
    # leave untracked files that block the next Agentbot update.
    backup = Path(tempfile.mkdtemp(prefix="agentbot-reconcile-"))
    manifest: list[str] = []
    for index, path in enumerate(dict.fromkeys(paths)):
        if not (path.exists() or path.is_symlink()):
            manifest.append(f"{index}\t{path}\tmissing")
            continue
        destination = backup / str(index)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            destination.symlink_to(path.readlink())
        elif path.is_dir():
            shutil.copytree(path, destination, symlinks=True)
        else:
            shutil.copy2(path, destination)
        manifest.append(f"{index}\t{path}\texists")
    (backup / "paths.tsv").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return backup


def _restore(snapshot: Path) -> None:
    manifest = snapshot / "paths.tsv"
    if not manifest.exists():
        return
    for line in manifest.read_text(encoding="utf-8").splitlines():
        index, raw_path, state = line.split("\t", 2)
        path = Path(raw_path)
        saved = snapshot / index
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        if state == "missing":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if saved.is_symlink():
            path.symlink_to(saved.readlink())
        elif saved.is_dir():
            shutil.copytree(saved, path, symlinks=True)
        else:
            shutil.copy2(saved, path)


def _tracked(path: Path, root: Path, runner: CommandRunner) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    result = runner.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", str(relative)],
        timeout_seconds=30,
    )
    return result.returncode == 0


def apply_reconcile_plan(
    paths: Any,
    config: SkillsSourcesConfig,
    plan: SkillReconcilePlan,
    *,
    checkouts: Mapping[str, Path] | None = None,
    confirm: bool | Callable[[SkillReconcilePlan], bool] = False,
    dry_run: bool = False,
    validate: Callable[[], None] | None = None,
    extra_affected: Iterable[Path] = (),
    runner: CommandRunner | None = None,
) -> ReconcileResult:
    """Apply source-owned skill deltas with a recoverable staged transaction."""
    command_runner = runner or CommandRunner()
    needs_confirmation = bool(
        plan.wildcard_additions
        or plan.wildcard_removals
        or plan.manifest_changes
    )
    approved = confirm(plan) if callable(confirm) else confirm
    if needs_confirmation and not approved:
        return ReconcileResult("confirmation_required", (), (), (), message="reconciliation was not confirmed")

    lock_path = paths.global_skill_lock
    lock = _lock_data(lock_path)
    added, removed = _approved_lock_mutations(config, plan, lock)
    if dry_run:
        return ReconcileResult(
            "preview",
            (lock_path,) if added or removed else (),
            tuple(sorted(removed)),
            tuple(sorted(added)),
            message="reconciliation preview only",
        )

    agents_home = paths.agents_skills_home
    codex_home = paths.codex_home / "skills"
    claude_home = paths.claude_skills_home
    explicit_removals = {
        (change.source_id, change.skill)
        for change in plan.manifest_changes
        if change.action == "remove"
    }
    lock_skills = lock["skills"]
    source_by_id = {source.id: source for source in config.active_sources()}
    owned_by_name = {
        str(name): str(entry.get("source"))
        for name, entry in lock_skills.items()
        if isinstance(entry, Mapping) and entry.get("source")
    }

    checkout_dirs: dict[str, Path] = {}
    checkout_owner: dict[str, str] = {}
    for source_id, checkout in (checkouts or {}).items():
        source_dirs = _checkout_dirs(checkout)
        checkout_dirs.update(source_dirs)
        for skill in source_dirs:
            checkout_owner.setdefault(skill, source_id)

    affected: list[Path] = [lock_path] if added or removed else []
    if explicit_removals:
        affected.append(paths.skills_sources_file)
        for canonical in (paths.root / "base" / "AGENTS.md", paths.root / "AGENTS.md"):
            if canonical.exists():
                affected.append(canonical)
    for skill in sorted(removed | added):
        affected.extend(
            [agents_home / skill, codex_home / skill, claude_home / skill]
        )
    affected.extend(extra_affected)
    affected = list(dict.fromkeys(affected))
    backup = _snapshot(affected, paths.root)
    changed: list[Path] = []
    try:
        if explicit_removals:
            manifest_text = _manifest_after_removals(paths.skills_sources_file, explicit_removals)
            write_text_atomic(paths.skills_sources_file, manifest_text)
            changed.append(paths.skills_sources_file)
            for canonical in (paths.root / "base" / "AGENTS.md", paths.root / "AGENTS.md"):
                if canonical.exists():
                    write_text_atomic(
                        canonical,
                        _canonical_after_removals(
                            canonical, {skill for _source, skill in explicit_removals}
                        ),
                    )
                    changed.append(canonical)

        for skill in sorted(removed):
            owner = owned_by_name.get(skill)
            if owner is None:
                continue
            lock_skills.pop(skill, None)
            source_dir = agents_home / skill
            if source_dir.is_dir() and not source_dir.is_symlink():
                shutil.rmtree(source_dir)
                changed.append(source_dir)
            for link in (codex_home / skill, claude_home / skill):
                if link.is_symlink() and link.resolve() == source_dir.resolve():
                    link.unlink()
                    changed.append(link)

        for skill in sorted(added):
            source_dir = checkout_dirs.get(skill)
            target = agents_home / skill
            if not (target.exists() or target.is_symlink()):
                if source_dir is None:
                    raise ReconcileError(
                        f"wildcard skill {skill!r} was discovered without a checkout directory"
                    )
                agents_home.mkdir(parents=True, exist_ok=True)
                # No symlinks=True here, unlike _snapshot/_restore above: the
                # source is a temporary checkout that is deleted afterwards, so
                # preserving links would leave them dangling. Materialize.
                shutil.copytree(source_dir, target)
                changed.append(target)
            owner_source = source_by_id.get(checkout_owner.get(skill, ""))
            if owner_source is None or owner_source.repo is None:
                raise ReconcileError(f"unable to determine source owner for wildcard skill {skill!r}")
            lock_skills[skill] = {
                "source": owner_source.repo,
                "sourceType": "github",
            }

        if added or removed:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(lock_path, json.dumps(lock, indent=2) + "\n")
            changed.append(lock_path)

        from .claude_bridge import bridge_claude_skills
        from .render import _sync_codex_skills

        _sync_codex_skills(paths)
        bridge_claude_skills(agents_home, claude_home)
        if validate is not None:
            validate()
        shutil.rmtree(backup)
        changed_paths = tuple(dict.fromkeys(changed))
        status = (
            "applied-with-local-changes"
            if any(_tracked(path, paths.root, command_runner) for path in changed_paths)
            else "applied"
        )
        return ReconcileResult(status, changed_paths, tuple(sorted(removed)), tuple(sorted(added)))
    except Exception as error:
        _restore(backup)
        return ReconcileResult(
            "failed",
            tuple(dict.fromkeys(changed)),
            tuple(sorted(removed)),
            tuple(sorted(added)),
            backup_path=backup,
            message=str(error),
        )
