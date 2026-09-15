from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_VERSION = 1

# Manifest `repo` hosts, mapped to the `sourceType` recorded in the skill lock.
SOURCE_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}
DEFAULT_SOURCE_HOST = "github.com"


class SkillsSourcesError(ValueError):
    """Raised when skills.sources.yaml is missing or invalid."""


def split_source_repo(repo: str) -> tuple[str, str] | None:
    """Split a manifest `repo` value into `(host, repository path)`.

    A bare `owner/name` stays GitHub, so existing manifests are unaffected. A
    value may instead lead with a supported host -- `gitlab.com/group/sub/name`
    -- which is also the only form that accepts the nested paths GitLab allows
    and GitHub does not. Returns None when the value is not a usable source.
    """
    if not repo or any(character.isspace() for character in repo):
        return None
    for host in SOURCE_HOSTS:
        prefix = f"{host}/"
        if repo.startswith(prefix):
            path = repo[len(prefix) :]
            segments = path.split("/")
            if len(segments) < 2 or not all(segments):
                return None
            return host, path
    if repo.count("/") != 1:
        return None
    owner, name = repo.split("/", maxsplit=1)
    if not owner or not name:
        return None
    return DEFAULT_SOURCE_HOST, repo


def source_clone_url(repo: str) -> str | None:
    """Return the HTTPS clone URL for a manifest `repo` value, or None."""
    split = split_source_repo(repo)
    if split is None:
        return None
    host, path = split
    return f"https://{host}/{path}.git"


def source_type(repo: str) -> str | None:
    """Return the skill-lock `sourceType` for a manifest `repo` value."""
    split = split_source_repo(repo)
    return None if split is None else SOURCE_HOSTS[split[0]]


@dataclass(frozen=True)
class SkillSourceEntry:
    id: str
    repo: str | None
    skills: list[str] = field(default_factory=list)
    enabled: bool = True
    # Names to drop from this source. Mainly for `skills: all`, where an
    # upstream repository may ship things you do not want -- its own CLI test
    # fixtures, for instance -- and there is otherwise no way to say no.
    exclude: list[str] = field(default_factory=list)

    def excludes(self, skill: str) -> bool:
        return skill in self.exclude


@dataclass(frozen=True)
class SkillsSourcesConfig:
    version: int
    agents: list[str]
    scope: str
    sources: list[SkillSourceEntry]

    def active_sources(self) -> list[SkillSourceEntry]:
        return [
            source for source in self.sources if source.enabled and source.repo and source.skills
        ]


def load_skills_sources(path: Path) -> SkillsSourcesConfig:
    if not path.is_file():
        raise SkillsSourcesError(f"skills sources file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SkillsSourcesError(f"skills sources file must be a mapping: {path}")

    return validate_skills_sources(raw, path=path)


def validate_skills_sources(
    raw: dict[str, Any], *, path: Path | None = None
) -> SkillsSourcesConfig:
    label = str(path) if path is not None else "skills.sources.yaml"

    version = raw.get("version")
    if version != SUPPORTED_VERSION:
        raise SkillsSourcesError(
            f"{label}: unsupported version {version!r} (expected {SUPPORTED_VERSION})"
        )

    agents = _require_string_list(raw.get("agents"), field_name="agents", label=label)
    if not agents:
        raise SkillsSourcesError(f"{label}: agents must be a non-empty list")

    scope = raw.get("scope", "global")
    if not isinstance(scope, str) or not scope.strip():
        raise SkillsSourcesError(f"{label}: scope must be a non-empty string")

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, list):
        raise SkillsSourcesError(f"{label}: sources must be a list")

    sources: list[SkillSourceEntry] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(sources_raw):
        if not isinstance(item, dict):
            raise SkillsSourcesError(f"{label}: sources[{index}] must be a mapping")

        source_id = item.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise SkillsSourcesError(f"{label}: sources[{index}].id must be a non-empty string")
        if source_id in seen_ids:
            raise SkillsSourcesError(f"{label}: duplicate source id {source_id!r}")
        seen_ids.add(source_id)

        repo = item.get("repo")
        if repo is not None and not isinstance(repo, str):
            raise SkillsSourcesError(f"{label}: sources[{index}].repo must be a string or null")

        skills = _require_skills(item.get("skills", []), label=label, index=index)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SkillsSourcesError(f"{label}: sources[{index}].enabled must be a boolean")
        exclude = _require_string_list(
            item.get("exclude", []), field_name="exclude", label=label, index=index
        )
        for name in exclude:
            if skills != ["*"] and name not in skills:
                raise SkillsSourcesError(
                    f"{label}: sources[{index}].exclude names {name!r}, which this "
                    "source does not install"
                )

        sources.append(
            SkillSourceEntry(
                id=source_id,
                repo=repo.strip() if isinstance(repo, str) else None,
                skills=skills,
                enabled=enabled,
                exclude=exclude,
            )
        )

    config = SkillsSourcesConfig(
        version=version, agents=agents, scope=scope.strip(), sources=sources
    )
    _validate_active_skill_ownership(config, label=label)
    return config


def _validate_active_skill_ownership(config: SkillsSourcesConfig, *, label: str) -> None:
    """Ensure each installed skill has one unambiguous upstream owner."""
    owners: dict[str, str] = {}
    for source in config.active_sources():
        for skill in source.skills:
            # `skills: all` expands to the Skills CLI wildcard. Multiple
            # repositories may legitimately publish their complete catalogs;
            # only explicitly named skills have unambiguous local ownership.
            if skill == "*":
                continue
            owner = owners.get(skill)
            if owner is not None:
                raise SkillsSourcesError(
                    f"{label}: skill {skill!r} is declared by both active sources "
                    f"{owner!r} and {source.id!r}"
                )
            owners[skill] = source.id


def _require_string_list(
    value: Any,
    *,
    field_name: str,
    label: str,
    index: int | None = None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        location = f"sources[{index}].{field_name}" if index is not None else field_name
        raise SkillsSourcesError(f"{label}: {location} must be a list")

    items: list[str] = []
    for item_index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            location = (
                f"sources[{index}].{field_name}[{item_index}]"
                if index is not None
                else f"{field_name}[{item_index}]"
            )
            raise SkillsSourcesError(f"{label}: {location} must be a non-empty string")
        items.append(item.strip())
    return items


def _require_skills(value: Any, *, label: str, index: int) -> list[str]:
    """Normalize a selected skill list or the manifest shorthand ``all``."""
    if isinstance(value, str):
        if value.strip().lower() == "all":
            return ["*"]
        raise SkillsSourcesError(
            f"{label}: sources[{index}].skills must be a list or the string 'all'"
        )
    return _require_string_list(value, field_name="skills", label=label, index=index)
