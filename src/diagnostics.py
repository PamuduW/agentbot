from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .boost import BoostIntegration, BoostStatus
from .claude_statusline import (
    StatuslineState,
    doctor_claude_statusline,
    inspect_claude_statusline,
)
from .cli_config import doctor_cli_configs
from .cursor_statusline import doctor_cursor_statusline
from .graphify import GraphifyIntegration, GraphifyStatus
from .models import DiagnosticsSnapshot, DoctorIssue
from .paths import AgentbotPaths
from .render import installed_skill_dirs, managed_skill_names
from .skill_prune import PruneCandidate, plan_prune
from .skills_installer import doctor_skills, list_installed_skills
from .skills_sources import load_skills_sources
from .vscode import doctor_vscode


def _github_token_is_valid(value: str) -> bool:
    if value.startswith("ghs_"):
        return re.fullmatch(r"ghs_[A-Za-z0-9._-]{36,}", value) is not None
    return len(value) >= 20 and re.fullmatch(r"[A-Za-z0-9_]+", value) is not None


@dataclass(frozen=True)
class _DiagnosticsFacts:
    enabled_sources: int
    managed_names: frozenset[str]
    declared_names: frozenset[str]
    prune_candidates: tuple[PruneCandidate, ...]
    statusline: StatuslineState
    graphify_status: GraphifyStatus
    graphify_official: bool
    boost_status: BoostStatus


class Diagnostics:
    def __init__(self, paths: AgentbotPaths) -> None:
        self.paths = paths

    def collect(self) -> DiagnosticsSnapshot:
        facts = self._collect_facts()
        installed_skills = tuple(self.list_skills())
        claude_bridge_links = 0
        if self.paths.claude_skills_home.is_dir():
            claude_bridge_links = sum(
                1 for entry in self.paths.claude_skills_home.iterdir() if entry.is_symlink()
            )
        issues = tuple(self._doctor_issues(facts)) + tuple(self.skills_doctor_issues())

        return DiagnosticsSnapshot(
            installed_skills=installed_skills,
            enabled_sources=facts.enabled_sources,
            global_agents_exists=self.paths.global_agents.exists(),
            skills_sources_exists=self.paths.skills_sources_file.exists(),
            global_lock_exists=self.paths.global_skill_lock.exists(),
            global_lock_skills=self._count_global_lock_skills(self.paths),
            managed_skill_count=len(facts.managed_names),
            manual_skill_count=sum(
                1 for candidate in facts.prune_candidates if candidate.reason == "manual"
            ),
            prune_candidate_count=len(facts.prune_candidates),
            claude_bridge_links=claude_bridge_links,
            claude_statusline_state=facts.statusline.status_label,
            issues=issues,
        )

    def list_skills(self) -> list[str]:
        return list_installed_skills(self.paths)

    def skills_doctor_issues(self) -> list[DoctorIssue]:
        return doctor_skills(self.paths)

    def doctor_issues(self) -> list[DoctorIssue]:
        return self._doctor_issues(self._collect_facts())

    def _doctor_issues(self, facts: _DiagnosticsFacts) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        issues.extend(self._token_doctor_issues())

        if not self.paths.global_agents.exists():
            issues.append(
                DoctorIssue(
                    level="error",
                    scope="global",
                    message=f"Missing global baseline: {self.paths.global_agents}",
                )
            )
        if not self.paths.skills_sources_file.exists():
            issues.append(
                DoctorIssue(
                    level="error",
                    scope="skills",
                    message=f"Missing skills manifest: {self.paths.skills_sources_file}",
                )
            )

        issues.extend(doctor_claude_statusline(self.paths, state=facts.statusline))
        issues.extend(doctor_cursor_statusline(self.paths))
        for component, detail, result in doctor_cli_configs(self.paths):
            # Drift and unreadable configs are worth reporting; "nothing
            # declared" and "current" are not.
            if result == "check":
                issues.append(
                    DoctorIssue(level="warning", scope="cli-config", message=f"{component}: {detail}")
                )
        for component, detail, result in doctor_vscode(Path.home(), self.paths.root):
            if result == "check":
                issues.append(
                    DoctorIssue(level="warning", scope="vscode", message=f"{component}: {detail}")
                )
        managed_names = set(facts.managed_names)
        managed_dirs = {skill_dir.name: skill_dir for skill_dir in installed_skill_dirs(self.paths)}
        codex_skills = self.paths.codex_home / "skills"
        if facts.graphify_official and facts.graphify_status.state != "ready":
            level = "error" if facts.graphify_status.state == "broken" else "warning"
            issues.append(
                DoctorIssue(
                    level=level,
                    scope="graphify",
                    message=self._graphify_doctor_message(facts.graphify_status),
                )
            )
        if facts.boost_status.cli_path is not None and facts.boost_status.state in {
            "broken",
            "forbidden",
            "unsafe-config",
            "partial",
            "stale",
        }:
            level = (
                "error"
                if facts.boost_status.state in {"broken", "forbidden", "unsafe-config"}
                else "warning"
            )
            issues.append(
                DoctorIssue(level, "boost", self._boost_doctor_message(facts.boost_status))
            )
        if facts.boost_status.cli_path is not None:
            issues.extend(self._boost_flag_issues(facts.boost_status))

        for name in managed_names:
            source = self.paths.agents_skills_home / name
            if name not in managed_dirs:
                issues.append(
                    DoctorIssue(
                        level="error",
                        scope="skills",
                        message=f"Managed skill {name!r} is missing from {source}",
                    )
                )
                continue
            target = codex_skills / name
            if not target.is_symlink() or not target.exists():
                issues.append(
                    DoctorIssue(
                        level="error",
                        scope="codex",
                        message=(
                            f"Managed Codex skill link for {name!r} is missing or broken: {target}"
                        ),
                    )
                )
            elif target.resolve() != source.resolve():
                issues.append(
                    DoctorIssue(
                        level="warning",
                        scope="codex",
                        message=(
                            f"Managed Codex skill link for {name!r} points outside the "
                            f"managed source: {target}"
                        ),
                    )
                )

        for candidate in facts.prune_candidates:
            if candidate.reason == "manual" and candidate.directory is not None:
                target = codex_skills / candidate.name
                if (
                    not target.is_symlink()
                    or not target.exists()
                    or target.resolve() != candidate.directory.resolve()
                ):
                    message = (
                        f"Manual skill {candidate.name!r} is outside managed sources and has "
                        "no Codex link yet; select it under 'Prune Skills' or add a manifest "
                        "source to make it reproducible"
                    )
                else:
                    message = (
                        f"Manual skill {candidate.name!r} is available to Codex but outside "
                        "managed sources; select it under 'Prune Skills' or add a manifest "
                        "source to make it reproducible"
                    )
            elif candidate.reason == "orphaned":
                message = (
                    f"Orphaned skill {candidate.name!r} is {candidate.detail}; select it "
                    "under 'Prune Skills' or restore its manifest source"
                )
            elif candidate.reason == "excluded":
                message = (
                    f"Excluded skill {candidate.name!r} is still installed: {candidate.detail}; "
                    "select it under 'Prune Skills'"
                )
            else:
                message = (
                    f"Stale skill pin {candidate.name!r}: {candidate.detail}; select it "
                    "under 'Prune Skills'"
                )
            issues.append(DoctorIssue("warning", "reproducibility", message))
        return issues

    def graphify_status(self) -> GraphifyStatus:
        return GraphifyIntegration(self.paths).status()

    def _collect_facts(self) -> _DiagnosticsFacts:
        enabled_sources = 0
        declared_names: set[str] = set()
        prune_candidates: tuple[PruneCandidate, ...] = ()
        config = None
        if self.paths.skills_sources_file.exists():
            try:
                config = load_skills_sources(self.paths.skills_sources_file)
            except ValueError:
                enabled_sources = -1
            else:
                active_sources = config.active_sources()
                enabled_sources = len(active_sources)
                declared_names = {
                    skill for source in active_sources for skill in source.skills if skill != "*"
                }
                prune_candidates = plan_prune(self.paths, config).candidates

        managed = frozenset(managed_skill_names(self.paths))
        if config is None:
            prune_candidates = tuple(
                PruneCandidate(
                    name=source.name,
                    reason="manual",
                    detail="on disk, not in the lock; user-placed",
                    directory=source,
                    locked=False,
                )
                for source in self._unmanaged_skill_dirs(
                    set(managed), set(declared_names)
                )
            )

        graphify = GraphifyIntegration(self.paths)
        return _DiagnosticsFacts(
            enabled_sources=enabled_sources,
            managed_names=managed,
            declared_names=frozenset(declared_names),
            prune_candidates=prune_candidates,
            statusline=inspect_claude_statusline(self.paths),
            graphify_status=self.graphify_status(),
            graphify_official=graphify.version_path.is_file(),
            boost_status=BoostIntegration(self.paths).status(),
        )

    def status_summary(self) -> dict[str, object]:
        snapshot = self.collect()
        return {
            "installed_skills": len(snapshot.installed_skills),
            "enabled_sources": snapshot.enabled_sources,
            "global_agents_exists": snapshot.global_agents_exists,
            "skills_sources_exists": snapshot.skills_sources_exists,
            "global_lock_exists": snapshot.global_lock_exists,
            "global_lock_skills": snapshot.global_lock_skills,
            "managed_skill_count": snapshot.managed_skill_count,
            "manual_skill_count": snapshot.manual_skill_count,
            "prune_candidate_count": snapshot.prune_candidate_count,
            "claude_bridge_links": snapshot.claude_bridge_links,
            "claude_statusline_state": snapshot.claude_statusline_state,
            "doctor_issue_count": len(snapshot.issues),
        }

    def _unmanaged_skill_dirs(
        self, managed_names: set[str], declared_names: set[str]
    ) -> tuple[Path, ...]:
        if not self.paths.agents_skills_home.is_dir():
            return ()
        unmanaged: list[Path] = []
        for source in sorted(self.paths.agents_skills_home.iterdir()):
            if (
                not source.is_dir()
                or not (source / "SKILL.md").is_file()
                or source.name in managed_names
                or source.name in declared_names
            ):
                continue
            if source.name == "graphify" and (source / ".graphify_version").is_file():
                continue
            unmanaged.append(source)
        return tuple(unmanaged)

    @staticmethod
    def _graphify_doctor_message(status: GraphifyStatus) -> str:
        if status.state == "skill-without-cli":
            return f"{status.message} Install it through Dotfiles or run: uv tool install graphifyy"
        if status.state == "stale":
            return f"{status.message} Run `agentbot graphify setup` to refresh the skill."
        if status.state == "conflict":
            targets = [
                label
                for label, target_state in (
                    ("Codex", status.codex_state),
                    ("Claude", status.claude_state),
                )
                if target_state == "conflict"
            ]
            target_text = ", ".join(targets) or "an assistant"
            return f"{status.message} Preserved conflicting {target_text} target(s)."
        return status.message

    @staticmethod
    def _boost_flag_issues(status: BoostStatus) -> list[DoctorIssue]:
        """Report Boost feature flags that no longer match the declared policy.

        Setup writes the whole declared set, so divergence means something
        changed it afterwards -- almost always a toggle in Boost's report UI,
        which writes straight to config.toml. Reporting it rather than silently
        rewriting on read keeps the fix an explicit command.
        """
        from .boost import BOOST_FEATURE_POLICY, GRAPH_FEATURE_FLAG

        if not status.diverged_flags:
            return []
        detail = ", ".join(
            f"{flag} should be {'on' if BOOST_FEATURE_POLICY[flag] else 'off'}"
            for flag in status.diverged_flags
        )
        message = (
            f"Boost feature flags diverge from the declared policy ({detail}). "
            "Run `agentbot boost setup` to reapply it."
        )
        if GRAPH_FEATURE_FLAG in status.diverged_flags:
            message += (
                f" {GRAPH_FEATURE_FLAG} matters most: this pin is what keeps "
                "BoostGraph from installing, so a divergence here is the one "
                "that actually turns it on. Adopting it would need renderer "
                "support first, since it writes marker blocks into managed "
                "CLAUDE.md/AGENTS.md."
            )
        return [DoctorIssue(level="warning", scope="boost", message=message)]

    @staticmethod
    def _boost_doctor_message(status: BoostStatus) -> str:
        if status.state in {"cli-only", "partial"}:
            return f"{status.message} Run `agentbot boost setup`."
        if status.state == "unsafe-config":
            if status.shadowing_configs:
                # `boost setup` writes the global config, which is not the file
                # that is winning here. Pointing at it would be a dead end.
                return f"{status.message} Remove or correct each repository config."
            return f"{status.message} Run `agentbot boost setup` to restore safe flags."
        if status.state == "stale":
            # `dotfiles full-update` already does this by running agentbot
            # install after the upgrade; name the command for the paths that
            # do not, such as a bare `dotfiles update`.
            return f"{status.message} Run `agentbot boost setup` to rewrite them."
        if status.state == "forbidden":
            return f"{status.message} Run `agentbot boost off` and inspect the reported files."
        return status.message

    def _token_doctor_issues(self) -> list[DoctorIssue]:
        token_file = self.paths.config_home / "github.env"
        if not token_file.exists() and not token_file.is_symlink():
            return []
        if token_file.is_symlink() or not token_file.is_file():
            return [
                DoctorIssue(
                    "warning",
                    "token",
                    f"saved GitHub token path is not a regular file: {token_file}",
                )
            ]
        try:
            mode = stat.S_IMODE(token_file.stat().st_mode)
            content = token_file.read_text(encoding="utf-8")
        except OSError as error:
            return [DoctorIssue("warning", "token", f"saved GitHub token cannot be read: {error}")]
        if mode != 0o600:
            return [
                DoctorIssue(
                    "warning",
                    "token",
                    f"saved GitHub token must have mode 600: {token_file}",
                )
            ]
        lines = content.splitlines(keepends=True)
        if (
            len(lines) != 1
            or not lines[0].endswith("\n")
            or not lines[0].startswith("GITHUB_TOKEN=")
        ):
            return [DoctorIssue("warning", "token", "saved GitHub token has malformed assignment")]
        value = lines[0][len("GITHUB_TOKEN=") : -1]
        if not _github_token_is_valid(value):
            return [DoctorIssue("warning", "token", "saved GitHub token has an invalid value")]
        return []

    @staticmethod
    def _count_global_lock_skills(paths: AgentbotPaths) -> int:
        lock_path = paths.global_skill_lock
        if not lock_path.exists():
            return 0
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return -1
        if not isinstance(data, dict):
            return -1
        skills = data.get("skills")
        if isinstance(skills, (dict, list)):
            return len(skills)
        return 0
