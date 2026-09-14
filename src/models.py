from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .mcp_models import McpInstallOutcome

if TYPE_CHECKING:
    from .boost import BoostStatus
    from .graphify import GraphifyStatus
    from .skill_catalog import SourceCatalog
    from .skill_reconcile import ReconcileResult, SkillReconcilePlan
    from .skills_installer import InstallResult
    from .workspace_service import WorkspaceReport


@dataclass(frozen=True)
class DoctorIssue:
    level: str
    scope: str
    message: str


@dataclass(frozen=True)
class DiagnosticsSnapshot:
    installed_skills: tuple[str, ...]
    enabled_sources: int
    global_agents_exists: bool
    skills_sources_exists: bool
    global_lock_exists: bool
    global_lock_skills: int
    managed_skill_count: int
    manual_skill_count: int
    claude_bridge_links: int
    claude_statusline_state: str
    issues: tuple[DoctorIssue, ...]
    prune_candidate_count: int = 0
    mcp_catalog_count: int = 0
    mcp_managed_count: int = 0


@dataclass(frozen=True)
class TableSection:
    label: str
    rows: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Table:
    title: str
    breadcrumb: str
    sections: tuple[TableSection, ...]


@dataclass(frozen=True)
class OutputRefreshOutcome:
    claude_linked: int
    claude_updated: int
    claude_skipped: int


@dataclass(frozen=True)
class InstallOutcome:
    skills: tuple[InstallResult, ...]
    graphify: GraphifyStatus
    boost: BoostStatus
    outputs: OutputRefreshOutcome
    diagnostics: DiagnosticsSnapshot
    # The editor and CLI surfaces, which used to be reachable only from a
    # Platform submenu of their own. They are install components now, so their
    # state is part of what an install reports rather than somewhere else to
    # go and look. Optional because a run that did not select them still
    # reports on them, and a caller building an outcome by hand need not.
    platform: tuple[PlatformOutcome, ...] = ()
    #: What the MCP component registered, or read without registering when it
    #: was deselected. Optional for the same reason platform is: a caller
    #: building an outcome by hand need not supply it.
    mcp: McpInstallOutcome = field(default_factory=McpInstallOutcome)


@dataclass(frozen=True)
class PlatformOutcome:
    """One editor or CLI surface, as an install leaves it."""

    key: str
    label: str
    detail: str
    result: str


@dataclass(frozen=True)
class UpdateSnapshot:
    repository_head: str
    manifest_sha256: str
    global_lock_sha256: str | None


@dataclass(frozen=True)
class UpdatePlan:
    snapshot: UpdateSnapshot
    reconcile: SkillReconcilePlan
    graphify_action: str
    workspace_report: WorkspaceReport
    source_catalogs: tuple[SourceCatalog, ...] = ()
    #: What the MCP phase would register, read without registering it. The
    #: preview is the screen an operator confirms from, so it has to name every
    #: component the apply will touch -- and once a newly reviewed server
    #: reaches the catalog, this is the row that says so before the run.
    mcp: McpInstallOutcome = field(default_factory=McpInstallOutcome)


@dataclass(frozen=True)
class UpdateOutcome:
    status: str
    message: str = ""
    reconcile: ReconcileResult | None = None
    graphify: GraphifyStatus | None = None
    workspace_report: WorkspaceReport | None = None
    outputs: OutputRefreshOutcome | None = None
    diagnostics: DiagnosticsSnapshot | None = None
    #: What the update's MCP phase registered. None when the applier that ran
    #: does not report one, which the summary reads as "nothing to say".
    mcp: McpInstallOutcome | None = None
