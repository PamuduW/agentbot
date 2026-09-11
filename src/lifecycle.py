from __future__ import annotations

import json
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import cast

from .boost import BoostIntegration, BoostStatus
from .claude_bridge import BridgeResult, bridge_claude_skills
from .command_runner import CommandRunner
from .diagnostics import Diagnostics
from .graphify import GraphifyIntegration, GraphifyStatus
from .models import (
    DiagnosticsSnapshot,
    InstallOutcome,
    OutputRefreshOutcome,
    UpdateOutcome,
    UpdatePlan,
    UpdateSnapshot,
)
from .paths import AgentbotPaths
from .render import render_global_outputs, resync_global_outputs
from .skill_catalog import (
    SourceCatalog,
    StaleSourceCatalogError,
    discover_remote_catalogs,
    verified_source_checkouts,
)
from .skill_reconcile import apply_reconcile_plan, build_reconcile_plan
from .skills_installer import InstallResult, list_installed_skills, migrate_renamed_lock_sources
from .skills_installer import install_skills as install_skills_default
from .skills_installer import update_skills as update_skills_default
from .skills_sources import SkillsSourcesConfig, load_skills_sources
from .workspace_service import WorkspaceReport, WorkspaceService
from .workspace_state import WorkspaceRecord


class Lifecycle:
    def __init__(
        self,
        paths: AgentbotPaths,
        *,
        diagnostics: Diagnostics | None = None,
        graphify: GraphifyIntegration | None = None,
        boost: BoostIntegration | None = None,
        workspace_service: WorkspaceService | None = None,
        # Called two ways: with just paths for a plain install, and with a
        # `checkouts` mapping on the planned-update path (see
        # _planned_installer below), so the signature stays open.
        install_skills: Callable[..., list[InstallResult]] | None = None,
        update_skills: Callable[[AgentbotPaths], InstallResult] | None = None,
        refresh_outputs: Callable[[], OutputRefreshOutcome] | None = None,
        bridge_skills: Callable[..., BridgeResult] = bridge_claude_skills,
        render_global: Callable[[AgentbotPaths], None] = render_global_outputs,
        catalog_discoverer: Callable[[SkillsSourcesConfig], tuple[SourceCatalog, ...]] | None = None,
        repository_head: Callable[[Path], str] | None = None,
        workspace_preview: Callable[[], WorkspaceReport] | None = None,
        update_applier: Callable[[UpdatePlan], UpdateOutcome] | None = None,
        checkout_provider: Callable | None = None,
        reconcile_applier: Callable | None = None,
        planned_installer: Callable | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.paths = paths
        self._command_runner = command_runner or CommandRunner()
        self.diagnostics = diagnostics or Diagnostics(paths)
        self.graphify = graphify or GraphifyIntegration(paths, runner=self._command_runner)
        self.boost = boost or BoostIntegration(paths, runner=self._command_runner)
        self.workspace_service = workspace_service or WorkspaceService(
            paths,
            command_runner=self._command_runner,
        )
        self._install_skills = install_skills or (
            lambda paths, **kwargs: install_skills_default(
                paths,
                runner=self._command_runner,
                **kwargs,
            )
        )
        self._update_skills = update_skills or (
            lambda paths: update_skills_default(paths, runner=self._command_runner)
        )
        self._refresh_outputs = refresh_outputs
        self._bridge_skills = bridge_skills
        self._render_global = render_global
        self._catalog_discoverer = catalog_discoverer or (
            lambda config: discover_remote_catalogs(config, runner=self._command_runner)
        )
        self._repository_head = repository_head or self._read_repository_head
        self._workspace_preview = workspace_preview
        self._update_applier = update_applier
        self._checkout_provider = checkout_provider or (
            lambda config, expected: verified_source_checkouts(
                config,
                expected,
                runner=self._command_runner,
            )
        )
        self._reconcile_applier = reconcile_applier or (
            lambda *args, **kwargs: apply_reconcile_plan(
                *args,
                runner=self._command_runner,
                **kwargs,
            )
        )
        self._planned_installer = planned_installer or (
            lambda paths, checkouts: install_skills_default(
                paths,
                checkouts=checkouts,
                runner=self._command_runner,
            )
        )

    #: What an install may be narrowed to. Managed outputs and diagnostics are
    #: not here on purpose: they are the baseline every install leaves behind,
    #: and an install that skipped them would report on a state it had not
    #: established.
    SELECTABLE_COMPONENTS = ("skills", "graphify", "boost")

    def install(
        self,
        *,
        progress: Callable[[str, float], None] | None = None,
        components: tuple[str, ...] | None = None,
    ) -> InstallOutcome:
        # Every stage reports only after all of them finish, so without this an
        # install is minutes of silence with no way to tell work from a hang.
        # Each stage also reports how long the previous one took, because
        # "which stage is slow" is the question a long install actually raises.
        started = time.monotonic()

        def stage(message: str) -> None:
            nonlocal started
            if progress is None:
                return
            elapsed = time.monotonic() - started
            started = time.monotonic()
            progress(message, elapsed)

        stage_start = time.monotonic()

        # Before anything reads ownership: a lock still pinned to a renamed
        # upstream repository makes its skills look unowned, which shows up as
        # prune candidates rather than as an error.
        migrate_renamed_lock_sources(self.paths.global_skill_lock)
        started = stage_start

        # None means every component, so an install that was never narrowed
        # behaves exactly as it did before selection existed.
        selected = set(components if components is not None else self.SELECTABLE_COMPONENTS)

        if "skills" in selected:
            stage("Installing skill sources")
            skills = tuple(self._install_skills(self.paths))
        else:
            stage("Skipping skill sources")
            skills = ()

        # A deselected integration is still *inspected*, never configured: the
        # operator declining to set Boost up is not a reason to stop reporting
        # what Boost is currently doing.
        if "graphify" in selected:
            stage("Refreshing Graphify integration")
            graphify = self.graphify.refresh_if_enabled()
        else:
            stage("Skipping Graphify integration")
            graphify = self.graphify.status()

        if "boost" in selected:
            stage("Configuring Boost integration")
            boost = self.boost.setup_if_cli_available()
        else:
            stage("Skipping Boost integration")
            boost = self.boost.status()

        stage("Refreshing managed outputs")
        outputs = self.refresh_outputs()
        stage("Running diagnostics")
        diagnostics = self.diagnostics.collect()
        stage("Install complete")
        return InstallOutcome(skills, graphify, boost, outputs, diagnostics)

    def render_global(self) -> None:
        self._render_global(self.paths)

    def install_skills(self) -> list[InstallResult]:
        return self._install_skills(self.paths)

    def update_skills(self) -> InstallResult:
        return self._update_skills(self.paths)

    def plan_update(
        self, *, progress: Callable[[str, float], None] | None = None
    ) -> UpdatePlan:
        config = load_skills_sources(self.paths.skills_sources_file)
        # Planning shallow-clones every source to see what it holds, which was
        # about thirty seconds of silence before anything reached the screen --
        # and then a prompt. Each phase says what it is doing first.
        started = time.monotonic()

        def stage_begins(message: str) -> None:
            nonlocal started
            if progress is None:
                return
            elapsed = time.monotonic() - started
            started = time.monotonic()
            progress(message, elapsed)

        active = [source for source in config.active_sources() if source.repo is not None]
        stage_begins(f"Checking {len(active)} skill source(s) for changes")
        catalogs = self._catalog_discoverer(config)
        discovered = {catalog.source_id: catalog.skills for catalog in catalogs}
        lock: dict = {}
        if self.paths.global_skill_lock.exists():
            lock = json.loads(self.paths.global_skill_lock.read_text(encoding="utf-8"))
        reconcile = build_reconcile_plan(config, discovered=discovered, lock=lock)
        stage_begins("Reading the Graphify integration")
        graphify_status = self.graphify.status()
        if graphify_status.cli_path is None:
            graphify_action = "skip"
        elif graphify_status.skill_path.is_file():
            graphify_action = "refresh"
        else:
            graphify_action = "setup"
        stage_begins("Previewing managed workspaces")
        workspace_report = (
            self._workspace_preview()
            if self._workspace_preview is not None
            else self.resync_workspaces(apply=False)
        )
        stage_begins("Plan complete")
        return UpdatePlan(
            snapshot=self._update_snapshot(),
            reconcile=reconcile,
            graphify_action=graphify_action,
            workspace_report=workspace_report,
            source_catalogs=catalogs,
        )

    def apply_update(
        self,
        plan: UpdatePlan,
        *,
        progress: Callable[[str, float], None] | None = None,
    ) -> UpdateOutcome:
        if self._update_snapshot() != plan.snapshot:
            return UpdateOutcome(
                "stale-plan",
                "Managed state changed after preview; preview again before applying.",
            )
        if self._update_applier is None:
            return self._apply_planned_update(plan, progress=progress)
        return self._update_applier(plan)

    def _apply_planned_update(
        self,
        plan: UpdatePlan,
        *,
        progress: Callable[[str, float], None] | None = None,
    ) -> UpdateOutcome:
        config = load_skills_sources(self.paths.skills_sources_file)
        # The same shape install reports: each call names the phase starting and
        # how long the one before it took. An apply was four phases of silence
        # punctuated only by the skills installer's own per-source lines, so
        # everything after the skills went unreported.
        started = time.monotonic()

        def stage_begins(message: str) -> None:
            nonlocal started
            if progress is None:
                return
            elapsed = time.monotonic() - started
            started = time.monotonic()
            progress(message, elapsed)

        try:
            with self._checkout_provider(config, plan.source_catalogs) as checkouts:
                stage: dict[str, object] = {}

                def validate_transaction() -> None:
                    stage_begins("Reconciling skill sources")
                    self._planned_installer(self.paths, checkouts)
                    graphify = self.graphify.status()
                    if plan.graphify_action in {"setup", "refresh"}:
                        stage_begins("Refreshing Graphify integration")
                        graphify = self.graphify.setup()
                        if graphify.state == "broken":
                            raise RuntimeError(f"Graphify: {graphify.message}")
                    else:
                        stage_begins("Skipping Graphify integration")
                    stage["graphify"] = graphify
                    stage_begins("Refreshing managed workspaces")
                    workspace_report = self.resync_workspaces(apply=True)
                    if any(
                        result.status in {"conflict", "failed"}
                        for result in workspace_report.results
                    ) or any(
                        action.kind == "conflict" for action in workspace_report.global_actions
                    ):
                        raise RuntimeError("managed workspace or global output refresh failed")
                    stage["workspace_report"] = workspace_report
                    stage_begins("Running diagnostics")
                    diagnostics = self.diagnostics.collect()
                    errors = [
                        issue for issue in diagnostics.issues if issue.level.lower() == "error"
                    ]
                    if errors:
                        raise RuntimeError(
                            "post-update Doctor found errors: "
                            + "; ".join(issue.message for issue in errors)
                        )
                    stage["diagnostics"] = diagnostics
                    stage_begins("Update complete")

                reconcile = self._reconcile_applier(
                    self.paths,
                    config,
                    plan.reconcile,
                    checkouts=checkouts,
                    confirm=True,
                    dry_run=False,
                    validate=validate_transaction,
                    extra_affected=self._update_transaction_paths(plan),
                )
                if reconcile.status not in {
                    "applied",
                    "applied-with-local-changes",
                }:
                    return UpdateOutcome(
                        reconcile.status,
                        reconcile.message,
                        reconcile=reconcile,
                    )
                return UpdateOutcome(
                    reconcile.status,
                    "Update plan applied.",
                    reconcile=reconcile,
                    # `stage` is a loosely-typed staging dict; these three keys
                    # are populated by the update applier with the matching
                    # dataclasses, which the dict type cannot express.
                    graphify=cast("GraphifyStatus | None", stage.get("graphify")),
                    workspace_report=cast("WorkspaceReport | None", stage.get("workspace_report")),
                    diagnostics=cast("DiagnosticsSnapshot | None", stage.get("diagnostics")),
                )
        except StaleSourceCatalogError as error:
            return UpdateOutcome("stale-plan", str(error))
        except Exception as error:
            return UpdateOutcome("failed", str(error))

    def _update_transaction_paths(self, plan: UpdatePlan) -> tuple[Path, ...]:
        affected = [
            self.paths.agents_skills_home,
            self.paths.codex_home / "skills",
            self.paths.claude_skills_home,
            self.paths.workspace_state_file,
            self.paths.codex_home / "AGENTS.md",
            self.paths.claude_home / "CLAUDE.md",
            self.paths.claude_home / "statusline-command.sh",
            self.paths.claude_home / "settings.json",
        ]
        for result in plan.workspace_report.results:
            for action in result.actions:
                affected.append(result.path / action.relative_path)
        return tuple(dict.fromkeys(affected))

    def _update_snapshot(self) -> UpdateSnapshot:
        return UpdateSnapshot(
            repository_head=self._repository_head(self.paths.root),
            manifest_sha256=self._file_sha256(self.paths.skills_sources_file),
            global_lock_sha256=(
                self._file_sha256(self.paths.global_skill_lock)
                if self.paths.global_skill_lock.exists()
                else None
            ),
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        return sha256(path.read_bytes()).hexdigest()

    def _read_repository_head(self, root: Path) -> str:
        completed = self._command_runner.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            timeout_seconds=30,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError(f"unable to read repository HEAD for {root}")
        return completed.stdout.strip()

    def refresh_outputs(self) -> OutputRefreshOutcome:
        if self._refresh_outputs is not None:
            return self._refresh_outputs()
        bridge = self._bridge_skills(
            agents_home=self.paths.agents_skills_home,
            claude_home=self.paths.claude_skills_home,
        )
        already = sum(1 for action in bridge.actions if action.action == "already_linked")
        linked = sum(1 for action in bridge.actions if action.action == "linked")
        updated = sum(1 for action in bridge.actions if action.action == "updated")
        skipped = sum(1 for action in bridge.actions if action.action == "skip_existing")
        self._render_global(self.paths)
        return OutputRefreshOutcome(already + linked, updated, skipped)

    def resync_workspaces(self, *, apply: bool, paths: tuple[Path, ...] = ()) -> WorkspaceReport:
        report = self.workspace_service.resync(apply=apply, paths=paths)
        global_actions = resync_global_outputs(self.paths, apply=apply)
        return WorkspaceReport(results=report.results, global_actions=global_actions)

    def preview_workspace(
        self,
        path: Path,
        *,
        profile: str | None,
        targets: tuple[str, ...] | None,
    ):
        return self.workspace_service.preview(path, profile_name=profile, targets=targets)

    def apply_workspace(
        self,
        path: Path,
        *,
        profile: str | None,
        targets: tuple[str, ...] | None,
        register: bool,
    ):
        return self.workspace_service.apply(
            path,
            profile_name=profile,
            targets=targets,
            register=register,
        )

    def list_workspaces(self) -> tuple[WorkspaceRecord, ...]:
        return self.workspace_service.store.load()

    @property
    def command_runner(self) -> CommandRunner:
        """The one runner this lifecycle injects everywhere.

        Exposed so a command handler reuses it rather than constructing a
        second one, which is the arrangement tests/test_lifecycle.py pins.
        """
        return self._command_runner

    def remove_workspace(self, path: Path) -> WorkspaceRecord:
        return self.workspace_service.remove(path)

    def graphify_status(self) -> GraphifyStatus:
        return self.graphify.status()

    def setup_graphify(self) -> GraphifyStatus:
        status = self.graphify.setup()
        if status.cli_path is not None and status.skill_path.is_file() and status.state != "broken":
            self.refresh_outputs()
            return self.graphify.status()
        return status

    def boost_status(self) -> BoostStatus:
        return self.boost.status()

    def setup_boost(self) -> BoostStatus:
        status = self.boost.setup()
        if status.cli_path is not None and status.state != "broken":
            self.refresh_outputs()
            return self.boost.status()
        return status

    def disable_boost(self) -> BoostStatus:
        status = self.boost.off()
        if status.cli_path is not None and status.state != "broken":
            self.refresh_outputs()
            return self.boost.status()
        return status

    def sync_graphify_if_cli_available(self, *, refresh_outputs: bool = True) -> GraphifyStatus:
        current = self.graphify.status()
        if current.cli_path is None:
            return current
        status = self.graphify.setup()
        if refresh_outputs and status.skill_path.is_file() and status.state != "broken":
            self.refresh_outputs()
        return status

    def list_skills(self) -> list[str]:
        return list_installed_skills(self.paths)
