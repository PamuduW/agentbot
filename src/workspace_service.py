from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .command_runner import CommandRunner
from .paths import AgentbotPaths
from .workspace_profiles import (
    WorkspaceProfile,
    load_workspace_profiles,
    select_workspace_profile,
)
from .workspace_render import (
    OUTPUT_PATHS,
    RenderAction,
    WorkspaceRenderPlan,
    apply_workspace_render_plan,
    build_workspace_render_plan,
    is_review_template_path,
)
from .workspace_state import WorkspaceRecord, WorkspaceStore


@dataclass(frozen=True)
class WorkspaceIdentity:
    path: Path
    kind: Literal["git", "directory"]
    git_root: Path | None


@dataclass(frozen=True)
class WorkspaceResult:
    path: Path
    status: Literal["preview", "applied", "conflict", "failed"]
    actions: tuple[RenderAction, ...]
    message: str


@dataclass(frozen=True)
class WorkspaceReport:
    results: tuple[WorkspaceResult, ...]
    global_actions: tuple[RenderAction, ...] = ()


class WorkspaceConflict(ValueError):
    def __init__(self, relative_path: str, message: str) -> None:
        super().__init__(message)
        self.relative_path = relative_path


def resolve_workspace_identity(
    path: Path,
    *,
    runner: CommandRunner | None = None,
) -> WorkspaceIdentity:
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_dir():
        raise ValueError(f"workspace target is not a directory: {path}")

    completed = (runner or CommandRunner()).run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        timeout_seconds=30,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        root = Path(completed.stdout.strip()).resolve(strict=False)
        return WorkspaceIdentity(path=root, kind="git", git_root=root)
    return WorkspaceIdentity(path=target, kind="directory", git_root=None)


def current_commit(
    identity: WorkspaceIdentity,
    *,
    runner: CommandRunner | None = None,
) -> str | None:
    if identity.kind != "git":
        return None
    completed = (runner or CommandRunner()).run(
        ["git", "-C", str(identity.path), "rev-parse", "--short", "HEAD"],
        timeout_seconds=30,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


class WorkspaceService:
    def __init__(
        self,
        paths: AgentbotPaths,
        *,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.paths = paths
        self.store = WorkspaceStore(paths.workspace_state_file)
        self._command_runner = command_runner or CommandRunner()

    def remove(self, path: Path) -> WorkspaceRecord:
        removed = self.store.remove(path)
        if removed is None:
            canonical = Path(path).expanduser().resolve(strict=False)
            raise ValueError(f"workspace is not registered: {canonical}")
        return removed

    def preview(
        self,
        path: Path,
        *,
        profile_name: str | None,
        targets: tuple[str, ...] | None,
    ) -> WorkspaceResult:
        identity = resolve_workspace_identity(path, runner=self._command_runner)
        try:
            plan, profile, dirty = self._build_plan(identity, profile_name, targets)
        except WorkspaceConflict as error:
            return self._conflict_result(identity, error)
        return self._preview_result(identity, plan, dirty, profile)

    def apply(
        self,
        path: Path,
        *,
        profile_name: str | None,
        targets: tuple[str, ...] | None,
        register: bool,
    ) -> WorkspaceResult:
        identity = resolve_workspace_identity(path, runner=self._command_runner)
        try:
            plan, profile, dirty = self._build_plan(identity, profile_name, targets)
        except WorkspaceConflict as error:
            return self._conflict_result(identity, error)
        if any(action.kind == "conflict" for action in plan.actions):
            return self._preview_result(identity, plan, dirty, profile)

        try:
            apply_workspace_render_plan(identity.path, plan)
        except WorkspaceConflict as error:
            action = RenderAction(
                error.relative_path,
                "conflict",
                None,
                str(error),
            )
            return WorkspaceResult(
                identity.path,
                "conflict",
                (action,),
                str(error),
            )
        except (OSError, ValueError) as error:
            return WorkspaceResult(
                identity.path,
                "failed",
                plan.actions,
                f"workspace render failed: {error}",
            )

        if register:
            self.store.upsert(
                self._record_for(
                    identity,
                    plan,
                    profile,
                    rendered_at=_utc_now(),
                )
            )
        message = self._message(identity, "applied", dirty, plan)
        return WorkspaceResult(identity.path, "applied", plan.actions, message)

    def _conflict_result(
        self,
        identity: WorkspaceIdentity,
        error: WorkspaceConflict,
    ) -> WorkspaceResult:
        action = RenderAction(
            error.relative_path,
            "conflict",
            None,
            str(error),
        )
        return WorkspaceResult(
            identity.path,
            "conflict",
            (action,),
            str(error),
        )

    def resync(
        self,
        *,
        apply: bool,
        paths: tuple[Path, ...] = (),
    ) -> WorkspaceReport:
        records = self.store.load()
        if paths:
            requested = {Path(path).expanduser().resolve(strict=False) for path in paths}
            records_by_path = {Path(record.path): record for record in records}
            selected: list[WorkspaceRecord] = []
            results: list[WorkspaceResult] = []
            for path in sorted(requested):
                record = records_by_path.get(path)
                if record is None:
                    results.append(
                        WorkspaceResult(
                            path,
                            "failed",
                            (),
                            f"workspace is not registered: {path}",
                        )
                    )
                else:
                    selected.append(record)
        else:
            selected = [record for record in records if record.enabled]
            results = []

        for record in sorted(selected, key=lambda item: item.path):
            results.extend(self._orphaned_output_results(record))
            try:
                results.append(self._resync_record(record, apply=apply))
            except (OSError, ValueError) as error:
                results.append(
                    WorkspaceResult(
                        Path(record.path),
                        "failed",
                        (),
                        f"workspace resync failed: {error}",
                    )
                )
        return WorkspaceReport(tuple(sorted(results, key=lambda result: str(result.path))))

    def _orphaned_output_results(self, record: WorkspaceRecord) -> list[WorkspaceResult]:
        """Report Agentbot-generated outputs that are no longer registered targets.

        Registering fewer targets than a directory already has leaves the extra
        files behind. They still carry the managed markers, so they look
        generated and current, but nothing refreshes them -- they silently drift
        away from the policy every other surface is following. Resync only walks
        registered targets, so without this the drift is invisible.
        """
        from .workspace_render import MANAGED_BEGIN, OUTPUT_PATHS, RETIRED_OUTPUT_PATHS

        root = Path(record.path)
        if not root.is_dir():
            return []

        orphans: list[WorkspaceResult] = []
        candidates = {**OUTPUT_PATHS, **RETIRED_OUTPUT_PATHS}
        for target, relative_path in sorted(candidates.items()):
            if target in record.targets:
                continue
            candidate = root / relative_path
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if MANAGED_BEGIN not in content:
                # No managed marker: user-authored, none of our business.
                continue
            orphans.append(
                WorkspaceResult(
                    root,
                    "conflict",
                    (),
                    f"{relative_path} is Agentbot-generated but {target!r} is not a "
                    "registered target; it will never be refreshed. "
                    + (
                        f"Support for {target!r} was removed, so delete the file."
                        if target in RETIRED_OUTPUT_PATHS
                        else f"Re-register with --{target}, or delete the file "
                        "if it is no longer wanted."
                    ),
                )
            )
        return orphans

    def _resync_record(self, record: WorkspaceRecord, *, apply: bool) -> WorkspaceResult:
        path = Path(record.path)
        if not path.is_dir():
            return WorkspaceResult(path, "failed", (), "recorded workspace path is missing")

        if record.kind == "git":
            identity = resolve_workspace_identity(path, runner=self._command_runner)
            if identity.kind != "git" or identity.path != path:
                return WorkspaceResult(
                    path,
                    "failed",
                    (),
                    "recorded workspace no longer resolves to the recorded Git root",
                )
        else:
            identity = WorkspaceIdentity(path=path, kind="directory", git_root=None)

        if apply:
            result = self.apply(
                path,
                profile_name=record.profile,
                targets=record.targets,
                register=False,
            )
            if result.status == "applied":
                self.store.upsert(
                    WorkspaceRecord(
                        path=record.path,
                        kind=record.kind,
                        policy_mode=record.policy_mode,
                        profile=record.profile,
                        targets=record.targets,
                        enabled=record.enabled,
                        last_commit=current_commit(identity, runner=self._command_runner),
                        last_rendered_at=_utc_now(),
                    )
                )
            return result

        return self.preview(
            path,
            profile_name=record.profile,
            targets=record.targets,
        )

    def _build_plan(
        self,
        identity: WorkspaceIdentity,
        profile_name: str | None,
        targets: tuple[str, ...] | None,
    ) -> tuple[WorkspaceRenderPlan, WorkspaceProfile, bool]:
        config = load_workspace_profiles(self.paths.workspace_profiles_file)
        profile = select_workspace_profile(config, profile_name)
        selected = self._select_targets(profile, targets)
        base_file = self.paths.root / "base" / "AGENTS.md"
        if not base_file.is_file():
            raise ValueError(f"missing base AGENTS.md template: {base_file}")
        base_template = base_file.read_text(encoding="utf-8")
        existing_files = self._read_existing_files(identity.path, selected)
        plan = build_workspace_render_plan(base_template, existing_files, selected)
        return plan, profile, self._dirty_before_render(identity)

    def _select_targets(
        self,
        profile: WorkspaceProfile,
        targets: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        selected = tuple(targets or profile.default_targets)
        normalized: list[str] = []
        for target in selected:
            canonical = "agents" if target == "codex" else target
            if canonical not in profile.allowed_targets:
                raise ValueError(
                    f"workspace target {canonical!r} is not allowed by profile {profile.name!r}"
                )
            if canonical in normalized:
                raise ValueError(f"duplicate workspace target: {canonical}")
            normalized.append(canonical)
        if "agents" not in normalized:
            normalized.insert(0, "agents")
        return tuple(normalized)

    def _read_existing_files(
        self,
        root: Path,
        targets: Sequence[str],
    ) -> dict[str, str]:
        selected_paths = {OUTPUT_PATHS["agents"]}
        selected_paths.update(OUTPUT_PATHS[target] for target in targets if target != "agents")
        existing: dict[str, str] = {}
        for relative_path in selected_paths:
            path = root / relative_path
            if path.is_symlink():
                raise WorkspaceConflict(
                    relative_path,
                    f"workspace output is a symlink: {path}",
                )
            if path.exists() and not path.is_file():
                raise WorkspaceConflict(
                    relative_path,
                    f"workspace output is not a regular file: {path}",
                )
            if path.is_file():
                existing[relative_path] = path.read_text(encoding="utf-8")
            if path.parent.is_dir():
                for candidate in sorted(path.parent.iterdir()):
                    candidate_relative = candidate.relative_to(root).as_posix()
                    if not is_review_template_path(relative_path, candidate_relative):
                        continue
                    if candidate.is_symlink():
                        raise WorkspaceConflict(
                            candidate_relative,
                            f"workspace output is a symlink: {candidate}",
                        )
                    if not candidate.is_file():
                        raise WorkspaceConflict(
                            candidate_relative,
                            f"workspace output is not a regular file: {candidate}",
                        )
                    existing[candidate_relative] = candidate.read_text(encoding="utf-8")
        return existing

    def _dirty_before_render(self, identity: WorkspaceIdentity) -> bool:
        if identity.kind != "git":
            return False
        completed = self._command_runner.run(
            ["git", "-C", str(identity.path), "status", "--porcelain"],
            timeout_seconds=30,
        )
        return bool(completed.stdout.strip())

    def _record_for(
        self,
        identity: WorkspaceIdentity,
        plan: WorkspaceRenderPlan,
        profile: WorkspaceProfile,
        *,
        rendered_at: str,
    ) -> WorkspaceRecord:
        return WorkspaceRecord(
            path=str(identity.path),
            kind=identity.kind,
            policy_mode=plan.policy_mode,
            profile=profile.name,
            targets=tuple(
                target
                for target, relative_path in OUTPUT_PATHS.items()
                if relative_path in plan.paths()
            ),
            enabled=True,
            last_commit=current_commit(identity, runner=self._command_runner),
            last_rendered_at=rendered_at,
        )

    def _preview_result(
        self,
        identity: WorkspaceIdentity,
        plan: WorkspaceRenderPlan,
        dirty: bool,
        profile: WorkspaceProfile,
    ) -> WorkspaceResult:
        status: Literal["preview", "conflict"] = (
            "conflict" if any(action.kind == "conflict" for action in plan.actions) else "preview"
        )
        return WorkspaceResult(
            identity.path,
            status,
            plan.actions,
            self._message(identity, status, dirty, plan, profile=profile),
        )

    def _message(
        self,
        identity: WorkspaceIdentity,
        status: str,
        dirty: bool,
        plan: WorkspaceRenderPlan,
        *,
        profile: WorkspaceProfile | None = None,
    ) -> str:
        profile_suffix = f", profile={profile.name}" if profile is not None else ""
        dirty_suffix = f", dirty_before_render={dirty}" if identity.kind == "git" else ""
        return (
            f"{status} {identity.kind} workspace {identity.path}"
            f"{profile_suffix}{dirty_suffix}; policy={plan.policy_mode}"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
