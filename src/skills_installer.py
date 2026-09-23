from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .atomic_io import write_text_atomic
from .command_runner import CommandResult, CommandRunner
from .models import DoctorIssue
from .paths import AgentbotPaths
from .skill_catalog import (
    SourceCatalog,
    discover_remote_catalogs,
    skill_name_from_file,
    verified_source_checkouts,
)
from .skills_sources import (
    SkillSourceEntry,
    SkillsSourcesConfig,
    load_skills_sources,
    source_clone_url,
    source_type,
)


class SkillsInstallError(RuntimeError):
    """Raised when an npx skills subprocess fails."""


@dataclass(frozen=True)
class InstallResult:
    source_id: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    skipped: bool = False


@dataclass(frozen=True)
class SkillsUpdateReport:
    updated_skills: tuple[str, ...] = ()
    deleted_by_source: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def deleted_skills(self) -> tuple[str, ...]:
        return tuple(
            sorted({skill for _source, skills in self.deleted_by_source for skill in skills})
        )


@dataclass(frozen=True)
class InstallSummary:
    ok: int
    failed: int
    skipped: int


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    repo: str
    revision: str
    installed: tuple[str, ...]
    excluded: tuple[str, ...]


@dataclass(frozen=True)
class SkillUpdatePlan:
    catalogs: tuple[SourceCatalog, ...]
    previous: tuple[tuple[str, str | None], ...]
    manifest_sha256: str
    lock_sha256: str | None

    @property
    def review_id(self) -> str:
        payload = {
            "catalogs": [
                (catalog.source_id, catalog.repo, catalog.revision, catalog.skills)
                for catalog in self.catalogs
            ],
            "previous": self.previous,
            "manifest": self.manifest_sha256,
            "lock": self.lock_sha256,
        }
        return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


InstallProgress = Callable[[str], None]


def summarize_install_results(results: list[InstallResult]) -> InstallSummary:
    ok = failed = skipped = 0
    for result in results:
        if result.skipped:
            skipped += 1
        elif result.returncode == 0:
            ok += 1
        else:
            failed += 1
    return InstallSummary(ok=ok, failed=failed, skipped=skipped)


DEFAULT_NPX = "npx"
DEFAULT_NPX_TIMEOUT_SECONDS = 900
NPX_TIMEOUT_ENV = "AGENTBOT_NPX_TIMEOUT_SECONDS"
DEFAULT_GITHUB_CLONE_TIMEOUT_SECONDS = 300
GITHUB_CLONE_TIMEOUT_ENV = "AGENTBOT_GITHUB_CLONE_TIMEOUT_SECONDS"
MAX_COMMAND_ERROR_DETAIL_LENGTH = 240
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_UPDATE_LINE_RE = re.compile(r"^✓\s+Updated\s+(.+?)$")
_DELETED_HEADER_RE = re.compile(
    r"^Warning:\s+The following skills from (.+?) appear to have been deleted upstream:$"
)
_BULLET_RE = re.compile(r"^[•*-]\s+(.+?)$")


def _npx_timeout_seconds() -> int:
    raw_timeout = os.environ.get(NPX_TIMEOUT_ENV)
    if raw_timeout is None:
        return DEFAULT_NPX_TIMEOUT_SECONDS
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as error:
        raise SkillsInstallError(
            f"{NPX_TIMEOUT_ENV} must be a positive integer, got {raw_timeout!r}"
        ) from error
    if timeout_seconds <= 0:
        raise SkillsInstallError(
            f"{NPX_TIMEOUT_ENV} must be a positive integer, got {raw_timeout!r}"
        )
    return timeout_seconds


def _github_clone_timeout_seconds() -> int:
    raw_timeout = os.environ.get(GITHUB_CLONE_TIMEOUT_ENV)
    if raw_timeout is None:
        return DEFAULT_GITHUB_CLONE_TIMEOUT_SECONDS
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as error:
        raise SkillsInstallError(
            f"{GITHUB_CLONE_TIMEOUT_ENV} must be a positive integer, got {raw_timeout!r}"
        ) from error
    if timeout_seconds <= 0:
        raise SkillsInstallError(
            f"{GITHUB_CLONE_TIMEOUT_ENV} must be a positive integer, got {raw_timeout!r}"
        )
    return timeout_seconds


def _command_error_detail(stdout: str, stderr: str) -> str:
    """Keep command failures useful without replaying a child CLI transcript."""
    return CommandResult(1, stdout, stderr).detail(MAX_COMMAND_ERROR_DETAIL_LENGTH)


def parse_update_output(*outputs: str) -> SkillsUpdateReport:
    """Extract the stable skill delta lines emitted by ``npx skills update``."""
    updated: set[str] = set()
    deleted: dict[str, set[str]] = {}
    pending_source: str | None = None

    for output in outputs:
        for raw_line in output.splitlines():
            line = _ANSI_ESCAPE_RE.sub("", raw_line).strip()
            if not line:
                continue

            deleted_header = _DELETED_HEADER_RE.match(line)
            if deleted_header:
                pending_source = deleted_header.group(1).strip()
                deleted.setdefault(pending_source, set())
                continue

            if pending_source is not None:
                bullet = _BULLET_RE.match(line)
                if bullet:
                    deleted[pending_source].add(bullet.group(1).strip())
                    continue
                pending_source = None

            update_line = _UPDATE_LINE_RE.match(line)
            if update_line:
                skill = update_line.group(1).strip()
                if not re.fullmatch(r"\d+\s+skill\(s\)", skill, flags=re.IGNORECASE):
                    updated.add(skill)

    return SkillsUpdateReport(
        updated_skills=tuple(sorted(updated)),
        deleted_by_source=tuple(
            (source, tuple(sorted(skills))) for source, skills in sorted(deleted.items()) if skills
        ),
    )


def _clone_remote_source(
    repo: str,
    destination: Path,
    *,
    runner: CommandRunner | None = None,
) -> None:
    clone_url = source_clone_url(repo)
    if clone_url is None:
        raise ValueError(f"not a supported owner/repository source: {repo!r}")
    if shutil.which("git") is None:
        raise SkillsInstallError("git is required to install remote skill sources")

    timeout_seconds = _github_clone_timeout_seconds()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    completed = (runner or CommandRunner()).run(
        ["git", "clone", "--depth=1", clone_url, str(destination)],
        timeout_seconds=timeout_seconds,
        env=env,
    )
    if completed.timed_out:
        raise SkillsInstallError(
            f"clone for source {repo!r} timed out after {timeout_seconds} seconds"
        )
    if completed.returncode != 0:
        detail = completed.detail(MAX_COMMAND_ERROR_DETAIL_LENGTH)
        raise SkillsInstallError(f"failed to clone skill source {repo!r}: {detail}")


def _clone_pinned_source(
    repo: str,
    revision: str,
    destination: Path,
    *,
    runner: CommandRunner | None = None,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SkillsInstallError(f"invalid pinned revision for source {repo!r}")
    clone_url = source_clone_url(repo)
    if clone_url is None:
        raise SkillsInstallError(f"not a supported skill source: {repo!r}")
    command_runner = runner or CommandRunner()
    timeout = _github_clone_timeout_seconds()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for argv in (
        ["git", "clone", "--depth=1", "--no-checkout", clone_url, str(destination)],
        ["git", "-C", str(destination), "fetch", "--depth=1", "origin", revision],
        ["git", "-C", str(destination), "checkout", "--detach", revision],
    ):
        result = command_runner.run(argv, timeout_seconds=timeout, env=env)
        if result.returncode != 0 or result.timed_out:
            raise SkillsInstallError(f"unable to restore reviewed revision for source {repo!r}")
    if _checkout_revision(destination, runner=command_runner) != revision:
        raise SkillsInstallError(f"restored source {repo!r} did not match its reviewed revision")


def _checkout_revision(checkout: Path, *, runner: CommandRunner | None = None) -> str | None:
    if not (checkout / ".git").exists():
        return None
    command_runner = runner or CommandRunner()
    status = command_runner.run(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
        timeout_seconds=30,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise SkillsInstallError(f"skill source checkout {checkout.name!r} is not clean")
    result = command_runner.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], timeout_seconds=30
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SkillsInstallError(f"unable to read source revision for {checkout.name!r}")
    return revision


def _skill_folder_hash(skill_dir: Path) -> str:
    digest = sha256()
    for path in sorted(
        (path for path in skill_dir.rglob("*") if path.is_file() and ".git" not in path.parts),
        key=lambda path: path.relative_to(skill_dir).as_posix(),
    ):
        digest.update(path.relative_to(skill_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _require_global_lock(global_lock_file: Path | None) -> Path:
    """Refuse to guess the lock location.

    This used to fall back to ``Path.home()/".agents"/".skill-lock.json"``. A
    caller that forgot to pass a path — a test, for instance — then wrote pins
    into the real user lock instead of its own sandbox.
    """
    if global_lock_file is None:
        raise SkillsInstallError("global lock path is required to record a source-owned skill pin")
    return global_lock_file


def _record_checkout_lock(
    source: SkillSourceEntry,
    checkout: Path,
    lock_file: Path,
    *,
    source_revision: str | None = None,
) -> None:
    if source.repo is None:
        raise SkillsInstallError(f"source {source.id!r} has no repository to record")
    try:
        import json

        lock = json.loads(lock_file.read_text(encoding="utf-8")) if lock_file.is_file() else {}
    except (OSError, ValueError) as error:
        raise SkillsInstallError(
            f"unable to read global skill lock {lock_file}: {error}"
        ) from error
    if not isinstance(lock, dict):
        raise SkillsInstallError(f"global skill lock {lock_file} must be a JSON object")
    skills = lock.setdefault("skills", {})
    if not isinstance(skills, dict):
        raise SkillsInstallError(f"global skill lock {lock_file} has an invalid skills section")
    sources = lock.setdefault("agentbotSources", {})
    if not isinstance(sources, dict):
        raise SkillsInstallError(f"global skill lock {lock_file} has an invalid source section")
    previous_sources = lock.setdefault("agentbotPreviousSources", {})
    if not isinstance(previous_sources, dict):
        raise SkillsInstallError(f"global skill lock {lock_file} has an invalid previous source section")
    if source_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise SkillsInstallError(f"source {source.id!r} has an invalid Git revision")

    wanted = None if source.skills == ["*"] else set(source.skills)
    installed_skills_home = lock_file.parent / "skills"
    installed_names = (
        {
            entry.name
            for entry in installed_skills_home.iterdir()
            if entry.is_dir() and (entry / "SKILL.md").is_file()
        }
        if installed_skills_home.is_dir()
        else set()
    )

    checkout_skills: dict[str, Path] = {}
    for skill_file in sorted(
        checkout.rglob("SKILL.md"),
        key=lambda path: (
            len(path.relative_to(checkout).parts),
            path.relative_to(checkout).as_posix(),
        ),
    ):
        name = skill_name_from_file(skill_file)
        if (wanted is None or name in wanted) and name in installed_names and not source.excludes(name):
            checkout_skills.setdefault(name, skill_file)

    # A wildcard checkout can contain SKILL.md fixtures that npx deliberately
    # ignores. Only pin names that the successful command actually installed.
    # Reconcile earlier fallback pins for this source at the same time.
    for name, entry in list(skills.items()):
        if (
            isinstance(entry, dict)
            and entry.get("source") == source.repo
            and name not in checkout_skills
        ):
            del skills[name]

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    for name, skill_file in checkout_skills.items():
        relative_path = skill_file.relative_to(checkout).as_posix()
        existing = skills.get(name)
        skills[name] = {
            "source": source.repo,
            "sourceType": source_type(source.repo),
            "sourceUrl": source_clone_url(source.repo),
            "skillPath": relative_path,
            "skillFolderHash": _skill_folder_hash(skill_file.parent),
            **({"sourceRevision": source_revision} if source_revision else {}),
            "installedAt": existing.get("installedAt", now) if isinstance(existing, dict) else now,
            "updatedAt": now,
        }
    new_snapshot = {
        "repo": source.repo,
        "revision": source_revision,
        "installed": sorted(checkout_skills),
        "excluded": sorted(source.exclude),
    }
    old_snapshot = sources.get(source.id)
    if old_snapshot is not None and not isinstance(old_snapshot, dict):
        raise SkillsInstallError(f"source {source.id!r} has an invalid recorded snapshot")
    if old_snapshot is not None and old_snapshot != new_snapshot:
        previous_sources[source.id] = old_snapshot
    sources[source.id] = new_snapshot
    lock["version"] = 3
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(lock_file, json.dumps(lock, indent=2) + "\n")


# Sources whose upstream repository was renamed. Ownership is keyed on this
# exact string, so a lock still carrying the old name makes every skill from it
# look unowned: `plan_prune` classifies it `orphaned` and offers it for
# deletion, and `render.py` drops it from the managed name list. Renaming the
# manifest without migrating the lock is therefore not a cosmetic change.
RENAMED_SOURCE_REPOS: dict[str, str] = {
    "PamuduW/agent_bootstrap_skills": "PamuduW/agentbot_skills",
}


def migrate_renamed_lock_sources(
    lock_file: Path,
    renames: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Rewrite lock entries pinned to a renamed source. Returns migrated names.

    Idempotent, and deliberately fails closed: an unreadable or malformed lock
    is left untouched rather than rebuilt, matching how pruning treats an
    invalid lock. Callers that need the distinction should read the lock
    themselves.
    """
    mapping = dict(RENAMED_SOURCE_REPOS if renames is None else renames)
    if not mapping or not lock_file.is_file():
        return ()
    try:
        import json

        lock = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(lock, dict) or not isinstance(lock.get("skills"), dict):
        return ()

    migrated: list[str] = []
    for name, entry in lock["skills"].items():
        if not isinstance(entry, dict):
            continue
        current = entry.get("source")
        if not isinstance(current, str):
            continue
        replacement = mapping.get(current)
        if replacement is None:
            continue
        entry["source"] = replacement
        entry["sourceType"] = source_type(replacement)
        url = source_clone_url(replacement)
        if url is not None:
            entry["sourceUrl"] = url
        migrated.append(str(name))

    if not migrated:
        return ()
    import json

    write_text_atomic(lock_file, json.dumps(lock, indent=2) + "\n")
    return tuple(sorted(migrated))


def _lock_skill_names(lock_file: Path) -> set[str] | None:
    """Return names from a readable lock; None means unreadable or malformed."""
    if not lock_file.is_file():
        return set()
    try:
        import json

        data = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    skills = data.get("skills")
    if isinstance(skills, dict):
        return set(skills)
    if isinstance(skills, list):
        return {str(skill) for skill in skills}
    return set()


def build_add_argv(
    source: SkillSourceEntry,
    *,
    agents: list[str],
    global_scope: bool = True,
    npx: str = DEFAULT_NPX,
) -> list[str]:
    if not source.repo:
        raise ValueError(f"source {source.id!r} has no repo")

    argv = [npx, "--yes", "skills", "add", source.repo, "--full-depth"]
    for skill in source.skills:
        argv.extend(["--skill", skill])
    for agent in agents:
        argv.extend(["-a", agent])
    if global_scope:
        argv.append("-g")
    argv.append("-y")
    return argv


def build_update_argv(*, npx: str = DEFAULT_NPX, global_scope: bool = True) -> list[str]:
    argv = [npx, "--yes", "skills", "update"]
    if global_scope:
        argv.append("-g")
    argv.append("-y")
    return argv


def run_install_command(
    argv: list[str],
    *,
    source_id: str = "",
    dry_run: bool = False,
    cwd: Path | None = None,
    timeout_seconds: int | None = None,
    runner: CommandRunner | None = None,
) -> InstallResult:
    if dry_run:
        return InstallResult(
            source_id=source_id,
            command=argv,
            returncode=0,
            stdout="",
            stderr="",
        )

    if timeout_seconds is None:
        timeout_seconds = _npx_timeout_seconds()

    completed = (runner or CommandRunner()).run(
        argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    if completed.timed_out:
        source = f" source {source_id!r}" if source_id else ""
        raise SkillsInstallError(
            f"npx skills{source} timed out after {timeout_seconds} seconds: {' '.join(argv)}"
        )
    return InstallResult(
        source_id=source_id,
        command=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def install_source(
    source: SkillSourceEntry,
    *,
    agents: list[str],
    global_scope: bool = True,
    dry_run: bool = False,
    npx: str = DEFAULT_NPX,
    cwd: Path | None = None,
    global_lock_file: Path | None = None,
    progress: InstallProgress | None = None,
    checkout: Path | None = None,
    runner: CommandRunner | None = None,
) -> InstallResult:
    if not source.enabled or not source.repo or not source.skills:
        return InstallResult(
            source_id=source.id,
            command=[],
            returncode=0,
            stdout="",
            stderr="",
            skipped=True,
        )

    argv = build_add_argv(source, agents=agents, global_scope=global_scope, npx=npx)
    started = time.monotonic()
    if progress is not None:
        progress(f"[STEP] Installing skill source: {source.id} ({source.repo})")
    clone_url = source_clone_url(source.repo)
    try:
        if dry_run or clone_url is None:
            result = run_install_command(
                argv,
                source_id=source.id,
                dry_run=dry_run,
                cwd=cwd,
                runner=runner,
            )
        elif checkout is not None:
            source_revision = _checkout_revision(checkout, runner=runner)
            argv[4] = str(checkout)
            result = run_install_command(argv, source_id=source.id, cwd=cwd, runner=runner)
            if result.returncode == 0 and global_scope:
                _record_checkout_lock(
                    source, checkout, _require_global_lock(global_lock_file),
                    source_revision=source_revision,
                )
            result = InstallResult(
                source_id=result.source_id,
                command=build_add_argv(
                    source,
                    agents=agents,
                    global_scope=global_scope,
                    npx=npx,
                ),
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                skipped=result.skipped,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="agentbot-skill-") as temp_dir:
                checkout = Path(temp_dir) / source.id
                _clone_remote_source(source.repo, checkout, runner=runner)
                source_revision = _checkout_revision(checkout, runner=runner)
                argv[4] = str(checkout)
                result = run_install_command(argv, source_id=source.id, cwd=cwd, runner=runner)
                if result.returncode == 0 and global_scope:
                    _record_checkout_lock(
                        source, checkout, _require_global_lock(global_lock_file),
                        source_revision=source_revision,
                    )
                result = InstallResult(
                    source_id=result.source_id,
                    command=build_add_argv(
                        source, agents=agents, global_scope=global_scope, npx=npx
                    ),
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    skipped=result.skipped,
                )
        if not dry_run and result.returncode != 0:
            detail = _command_error_detail(result.stdout, result.stderr)
            if detail == "no diagnostic output":
                detail = f"exit code {result.returncode}"
            raise SkillsInstallError(f"failed to install source {source.id!r}: {detail}")
    except Exception:
        if progress is not None:
            progress(f"[FAIL] Skill source: {source.id} ({_elapsed(started)})")
        raise
    if progress is not None:
        progress(f"[OK] Skill source installed: {source.id} ({_elapsed(started)})")
    return result


def install_all(
    config: SkillsSourcesConfig,
    *,
    dry_run: bool = False,
    npx: str = DEFAULT_NPX,
    cwd: Path | None = None,
    global_lock_file: Path | None = None,
    progress: InstallProgress | None = None,
    checkouts: Mapping[str, Path] | None = None,
    runner: CommandRunner | None = None,
) -> list[InstallResult]:
    global_scope = config.scope == "global"
    return [
        install_source(
            source,
            agents=config.agents,
            global_scope=global_scope,
            dry_run=dry_run,
            npx=npx,
            cwd=cwd,
            global_lock_file=global_lock_file,
            progress=progress,
            checkout=(checkouts or {}).get(source.id),
            runner=runner,
        )
        for source in config.active_sources()
    ]


def update_all(
    config: SkillsSourcesConfig,
    *,
    dry_run: bool = False,
    npx: str = DEFAULT_NPX,
    cwd: Path | None = None,
    runner: CommandRunner | None = None,
) -> InstallResult:
    argv = build_update_argv(npx=npx, global_scope=config.scope == "global")
    result = run_install_command(
        argv,
        source_id="update",
        dry_run=dry_run,
        cwd=cwd,
        runner=runner,
    )
    if not dry_run and result.returncode != 0:
        detail = _command_error_detail(result.stdout, result.stderr)
        if detail == "no diagnostic output":
            detail = f"exit code {result.returncode}"
        raise SkillsInstallError(f"failed to update skills: {detail}")
    return result


def install_skills(
    paths: AgentbotPaths,
    *,
    dry_run: bool = False,
    checkouts: Mapping[str, Path] | None = None,
    runner: CommandRunner | None = None,
) -> list[InstallResult]:
    from .skill_prune import enforce_exclusions

    config = load_skills_sources(paths.skills_sources_file)
    # Each source is a network clone, so a dozen of them is minutes of silence
    # if nothing is emitted. Progress was gated on AGENTBOT_TUI, which the
    # install.sh path does not set, so exactly the long unattended run reported
    # nothing at all. Emit whenever someone is watching.
    progress = (
        _print_install_progress if os.environ.get("AGENTBOT_TUI") or sys.stdout.isatty() else None
    )
    results = install_all(
        config,
        dry_run=dry_run,
        cwd=paths.root,
        global_lock_file=paths.global_skill_lock,
        progress=progress,
        checkouts=checkouts,
        runner=runner,
    )
    if not dry_run:
        # `skills add` has no exclusion flag, so apply the manifest's exclusions
        # here -- before the Claude and Codex bridges run.
        for name in enforce_exclusions(paths, config):
            # Prefixed like everything else this run prints. It was the one
            # unmarked line in a column of [STEP] and [OK], which read as
            # output that had escaped rather than as a step's result.
            _print_install_progress(f"[OK] Excluded by manifest, removed: {name}")
    return results


def plan_skill_update(
    paths: AgentbotPaths, *, runner: CommandRunner | None = None
) -> SkillUpdatePlan:
    config = load_skills_sources(paths.skills_sources_file)
    manifest_digest = sha256(paths.skills_sources_file.read_bytes()).hexdigest()
    lock_file = paths.global_skill_lock
    if lock_file.is_symlink():
        raise SkillsInstallError("global skill lock must not be a symlink")
    lock_digest = sha256(lock_file.read_bytes()).hexdigest() if lock_file.is_file() else None
    if lock_file.is_file():
        try:
            lock = json.loads(lock_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SkillsInstallError(f"unable to read global skill lock: {error}") from error
        if not isinstance(lock, dict) or not isinstance(lock.get("skills"), dict):
            raise SkillsInstallError("global skill lock has an invalid skills section")
        recorded = lock.get("agentbotSources", {})
        if not isinstance(recorded, dict):
            raise SkillsInstallError("global skill lock has an invalid source section")
    else:
        recorded = {}
    previous: list[tuple[str, str | None]] = []
    for source in config.active_sources():
        entry = recorded.get(source.id)
        if entry is None:
            previous.append((source.id, None))
            continue
        revision = entry.get("revision") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or entry.get("repo") != source.repo
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
        ):
            raise SkillsInstallError(f"source {source.id!r} has an invalid recorded revision")
        previous.append((source.id, revision))
    catalogs = discover_remote_catalogs(config, runner=runner)
    return SkillUpdatePlan(catalogs, tuple(previous), manifest_digest, lock_digest)


def apply_skill_update(
    paths: AgentbotPaths,
    plan: SkillUpdatePlan,
    *,
    runner: CommandRunner | None = None,
) -> list[InstallResult]:
    current_manifest = sha256(paths.skills_sources_file.read_bytes()).hexdigest()
    lock_file = paths.global_skill_lock
    if lock_file.is_symlink():
        raise SkillsInstallError("global skill lock must not be a symlink")
    current_lock = sha256(lock_file.read_bytes()).hexdigest() if lock_file.is_file() else None
    if current_manifest != plan.manifest_sha256 or current_lock != plan.lock_sha256:
        raise SkillsInstallError("managed skill state changed after preview; preview again")
    config = load_skills_sources(paths.skills_sources_file)
    with verified_source_checkouts(config, plan.catalogs, runner=runner) as checkouts:
        return install_skills(paths, checkouts=checkouts, runner=runner)


def restore_skills(
    paths: AgentbotPaths,
    *,
    apply: bool = False,
    previous: bool = False,
    runner: CommandRunner | None = None,
) -> tuple[SourceSnapshot, ...]:
    config = load_skills_sources(paths.skills_sources_file)
    lock_file = paths.global_skill_lock
    if lock_file.is_symlink():
        raise SkillsInstallError("global skill lock must not be a symlink")
    try:
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SkillsInstallError(f"unable to read reviewed skill lock: {error}") from error
    if not isinstance(lock, dict) or not isinstance(lock.get("skills"), dict):
        raise SkillsInstallError("reviewed skill lock has an invalid skills section")
    current_sources = lock.get("agentbotSources")
    if not isinstance(current_sources, dict):
        raise SkillsInstallError("reviewed skill lock has no selected source revisions")
    if previous:
        previous_sources = lock.get("agentbotPreviousSources")
        if not isinstance(previous_sources, dict) or not previous_sources:
            raise SkillsInstallError("reviewed skill lock has no previous source revisions")
    else:
        previous_sources = {}

    snapshots: list[SourceSnapshot] = []
    for source in config.active_sources():
        historical = previous and source.id in previous_sources
        saved = previous_sources.get(source.id) if historical else current_sources.get(source.id)
        if not isinstance(saved, dict) or saved.get("repo") != source.repo:
            raise SkillsInstallError(f"source {source.id!r} has no matching reviewed snapshot")
        revision = saved.get("revision")
        installed = saved.get("installed")
        excluded = saved.get("excluded")
        if (
            not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(installed, list)
            or not installed
            or not all(isinstance(name, str) and name for name in installed)
            or len(set(installed)) != len(installed)
            or not isinstance(excluded, list)
            or not all(isinstance(name, str) and name for name in excluded)
        ):
            raise SkillsInstallError(f"source {source.id!r} has an invalid reviewed snapshot")
        for name in installed:
            entry = lock["skills"].get(name)
            if historical:
                if entry is not None and (
                    not isinstance(entry, dict) or entry.get("source") != source.repo
                ):
                    raise SkillsInstallError(f"skill {name!r} is owned by another source")
                manual = lock_file.parent / "skills" / name / "SKILL.md"
                if entry is None and manual.exists():
                    raise SkillsInstallError(f"skill {name!r} is now manually installed")
            elif (
                not isinstance(entry, dict)
                or entry.get("source") != source.repo
                or entry.get("sourceRevision") != revision
            ):
                raise SkillsInstallError(f"skill {name!r} does not match its reviewed source pin")
        snapshots.append(SourceSnapshot(
            source.id, source.repo or "", revision, tuple(installed), tuple(excluded)
        ))

    if not apply:
        return tuple(snapshots)
    with tempfile.TemporaryDirectory(prefix="agentbot-skill-restore-") as temp_dir:
        checkouts: dict[str, Path] = {}
        for snapshot in snapshots:
            checkout = Path(temp_dir) / snapshot.source_id
            _clone_pinned_source(snapshot.repo, snapshot.revision, checkout, runner=runner)
            checkouts[snapshot.source_id] = checkout
        for snapshot in snapshots:
            source = SkillSourceEntry(
                id=snapshot.source_id,
                repo=snapshot.repo,
                skills=list(snapshot.installed),
                exclude=list(snapshot.excluded),
            )
            install_source(
                source,
                agents=config.agents,
                checkout=checkouts[source.id],
                global_lock_file=lock_file,
                runner=runner,
            )
    return tuple(snapshots)


def _elapsed(started: float) -> str:
    seconds = int(time.monotonic() - started)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


#: A progress line written as "[LEVEL] text", which is how the callers above
#: build them.
_PREFIXED = re.compile(r"^\[(?P<level>[A-Z]+)\]\s+(?P<message>.*)$", re.DOTALL)


def _print_install_progress(message: str) -> None:
    """Print a progress line, coloured like every other one the run prints.

    These carry their marker inside the message string, so they went through
    the plain print below and stayed uncoloured wherever the menu's Bash relay
    was not there to paint them -- a terminal, or a Dotfiles full update.
    """
    from .ui.install_log import log_line

    match = _PREFIXED.match(message)
    if match is None:
        print(f"  {message}", flush=True)
        return
    log_line(match["level"], match["message"])


def update_skills(
    paths: AgentbotPaths,
    *,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
) -> InstallResult:
    config = load_skills_sources(paths.skills_sources_file)
    return update_all(config, dry_run=dry_run, cwd=paths.root, runner=runner)


def list_installed_skills(paths: AgentbotPaths) -> list[str]:
    home = paths.agents_skills_home
    if not home.is_dir():
        return []
    return sorted(skill_dir.name for skill_dir in home.iterdir() if skill_dir.is_dir())


def doctor_skills(paths: AgentbotPaths) -> list[DoctorIssue]:
    issues: list[DoctorIssue] = []
    config: SkillsSourcesConfig | None = None

    if not paths.skills_sources_file.is_file():
        issues.append(
            DoctorIssue(
                level="error",
                scope="skills",
                message=f"Missing skills sources file: {paths.skills_sources_file}",
            )
        )
    else:
        try:
            config = load_skills_sources(paths.skills_sources_file)
        except ValueError as error:
            issues.append(
                DoctorIssue(
                    level="error", scope="skills", message=f"Invalid skills sources file: {error}"
                )
            )

    if shutil.which("npx") is None:
        issues.append(
            DoctorIssue(
                level="error",
                scope="skills",
                message="npx is not available in PATH",
            )
        )

    for label, lock_file in (
        ("project", paths.skills_lock_file),
        ("global", paths.global_skill_lock),
    ):
        if not lock_file.is_file():
            continue
        try:
            import json

            lock_root = json.loads(lock_file.read_text(encoding="utf-8"))
            if not isinstance(lock_root, dict):
                raise ValueError("lock root must be a JSON object")
        except (OSError, ValueError) as error:
            issues.append(
                DoctorIssue(
                    level="warning",
                    scope="skills",
                    message=f"Unable to read {label} skills lock file: {error}",
                )
            )

    if config is not None and config.scope == "global":
        locked = _lock_skill_names(paths.global_skill_lock)
        if locked is not None and paths.global_skill_lock.is_file():
            declared = {skill for source in config.active_sources() for skill in source.skills}
            declared.discard("*")
            for skill in sorted(declared - locked):
                issues.append(
                    DoctorIssue(
                        level="warning",
                        scope="skills",
                        message=f"Manifest skill {skill!r} is absent from the global skill lock",
                    )
                )

    return issues
