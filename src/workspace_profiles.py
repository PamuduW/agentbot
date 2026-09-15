from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_VERSION = 1
WORKSPACE_TARGETS = frozenset({"agents", "claude", "cursor"})


@dataclass(frozen=True)
class WorkspaceProfile:
    name: str
    description: str
    default_targets: tuple[str, ...]
    allowed_targets: tuple[str, ...]
    allow_community_skill_scripts: bool


@dataclass(frozen=True)
class WorkspaceProfiles:
    version: int
    active_profile: str
    profiles: dict[str, WorkspaceProfile]


def load_workspace_profiles(path: Path) -> WorkspaceProfiles:
    if not path.is_file():
        raise ValueError(f"workspace profiles file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid workspace profiles: {path}: {error}") from error

    if not isinstance(raw, dict):
        raise ValueError(f"workspace profiles must be a mapping: {path}")
    return _validate_profiles(raw, label=str(path))


def select_workspace_profile(
    config: WorkspaceProfiles,
    requested_name: str | None,
) -> WorkspaceProfile:
    name = requested_name or config.active_profile
    try:
        return config.profiles[name]
    except KeyError as error:
        raise ValueError(f"unknown workspace profile: {name}") from error


def _validate_profiles(raw: dict[str, Any], *, label: str) -> WorkspaceProfiles:
    version = raw.get("version")
    if version != SUPPORTED_VERSION:
        raise ValueError(f"{label}: unsupported version {version!r} (expected {SUPPORTED_VERSION})")

    active_profile = raw.get("active_profile")
    if not isinstance(active_profile, str) or not active_profile.strip():
        raise ValueError(f"{label}: active_profile must be a non-empty string")

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError(f"{label}: profiles must be a non-empty mapping")

    profiles: dict[str, WorkspaceProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{label}: profile names must be non-empty strings")
        if not isinstance(raw_profile, dict):
            raise ValueError(f"{label}: profile {name!r} must be a mapping")
        profiles[name] = _validate_profile(name, raw_profile, label=label)

    if active_profile not in profiles:
        raise ValueError(f"{label}: active_profile {active_profile!r} is not defined")

    return WorkspaceProfiles(
        version=version,
        active_profile=active_profile,
        profiles=profiles,
    )


def _validate_profile(
    name: str,
    raw: dict[str, Any],
    *,
    label: str,
) -> WorkspaceProfile:
    default_targets = _target_tuple(
        raw.get("default_targets"),
        field_name=f"profile {name!r}.default_targets",
        label=label,
        allow_empty=False,
    )
    allowed_targets = _target_tuple(
        raw.get("allowed_targets"),
        field_name=f"profile {name!r}.allowed_targets",
        label=label,
        allow_empty=False,
    )

    missing_allowed = [target for target in default_targets if target not in allowed_targets]
    if missing_allowed:
        raise ValueError(
            f"{label}: profile {name!r}.default_targets contains targets not allowed: "
            + ", ".join(missing_allowed)
        )
    if "agents" not in allowed_targets:
        raise ValueError(f"{label}: profile {name!r}.allowed_targets must include agents")
    if "agents" not in default_targets:
        raise ValueError(f"{label}: profile {name!r}.default_targets must include agents")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{label}: profile {name!r}.description must be a string")

    allow_scripts = raw.get("allow_community_skill_scripts", False)
    if not isinstance(allow_scripts, bool):
        raise ValueError(
            f"{label}: profile {name!r}.allow_community_skill_scripts must be a boolean"
        )
    if allow_scripts:
        raise ValueError(
            f"{label}: profile {name!r} cannot enable community skill scripts in Phase 2"
        )

    return WorkspaceProfile(
        name=name,
        description=description.strip(),
        default_targets=default_targets,
        allowed_targets=allowed_targets,
        allow_community_skill_scripts=False,
    )


def _target_tuple(
    value: Any,
    *,
    field_name: str,
    label: str,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label}: {field_name} must be a list")
    if not value and not allow_empty:
        raise ValueError(f"{label}: {field_name} must not be empty")

    targets: list[str] = []
    for target in value:
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"{label}: {field_name} contains an invalid target")
        normalized = target.strip()
        if normalized not in WORKSPACE_TARGETS:
            raise ValueError(f"{label}: unsupported workspace target: {normalized}")
        if normalized in targets:
            raise ValueError(f"{label}: duplicate workspace target: {normalized}")
        targets.append(normalized)
    return tuple(targets)
