"""First-party skills adapted from an upstream skill, and whether upstream moved.

A skill source may list adaptations in an `adaptations.yaml` at its root: the
upstream repository and path, the commit the adaptation was made from, and the
Git tree hash of that path at that commit. Update already holds a verified
clone of every source, so it compares the recorded hash with the same path in
the upstream clone. A different hash means upstream changed the skill since the
adaptation; the operator decides whether to redo it (the source's
adaptations/README.md says how). Nothing here changes a skill.

The result is recorded under private state, so `agentbot status` can show it
without going to the network.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

from .atomic_io import write_text_atomic
from .paths import AgentbotPaths
from .skills_sources import SkillsSourcesConfig

MANIFEST = "adaptations.yaml"
STATE_FILE = "skill-adaptations.json"

State = Literal["current", "changed", "unknown"]


@dataclass(frozen=True)
class Adaptation:
    skill: str
    upstream: str
    base: str
    state: State
    detail: str = ""

    @property
    def upstream_name(self) -> str:
        return self.upstream.rstrip("/").rsplit("/", 1)[-1]


def _tree(checkout: Path, path: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", f"HEAD:{path}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    tree = completed.stdout.strip()
    return tree if completed.returncode == 0 and tree else None


def _entries(checkout: Path) -> list[dict[str, object]]:
    manifest = checkout / MANIFEST
    if not manifest.is_file():
        return []
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    entries = data.get("adaptations") if isinstance(data, dict) else None
    return [entry for entry in entries or [] if isinstance(entry, dict)]


def check(config: SkillsSourcesConfig, checkouts: Mapping[str, Path]) -> list[Adaptation]:
    """Every listed adaptation in every checked-out source, against its upstream."""
    by_repo = {
        source.repo: source.id
        for source in config.sources
        if source.repo and source.id in checkouts
    }
    results: list[Adaptation] = []
    for checkout in checkouts.values():
        for entry in _entries(checkout):
            skill = str(entry.get("skill") or "")
            upstream = str(entry.get("upstream") or "")
            path = str(entry.get("path") or "")
            base = str(entry.get("commit") or "")[:7]
            recorded = str(entry.get("tree") or "")
            if not (skill and upstream and path and recorded):
                results.append(
                    Adaptation(skill or "?", upstream, base, "unknown", "incomplete entry")
                )
                continue
            source_id = by_repo.get(upstream)
            if source_id is None:
                results.append(
                    Adaptation(skill, upstream, base, "unknown", "upstream is not a managed source")
                )
                continue
            current = _tree(checkouts[source_id], path)
            if current is None:
                results.append(
                    Adaptation(skill, upstream, base, "unknown", f"{path} not found upstream")
                )
            elif current == recorded:
                results.append(Adaptation(skill, upstream, base, "current"))
            else:
                results.append(Adaptation(skill, upstream, base, "changed"))
    return sorted(results, key=lambda item: item.skill)


def record(paths: AgentbotPaths, results: list[Adaptation], now: datetime | None = None) -> None:
    paths.config_home.mkdir(parents=True, exist_ok=True)
    state = {
        "checked": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "adaptations": [asdict(item) for item in results],
    }
    write_text_atomic(paths.config_home / STATE_FILE, json.dumps(state, indent=2) + "\n")


def summary(paths: AgentbotPaths) -> tuple[str, str]:
    """One status row: (detail, result). Reads only the last recorded check."""
    try:
        state = json.loads((paths.config_home / STATE_FILE).read_text(encoding="utf-8"))
        items = [Adaptation(**item) for item in state.get("adaptations", [])]
    except (OSError, ValueError, TypeError):
        return "not checked yet; `agentbot update` checks them", "info"
    if not items:
        return "none", "ok"
    changed = [item for item in items if item.state == "changed"]
    unknown = [item for item in items if item.state == "unknown"]
    if changed:
        return (
            "; ".join(
                f"{item.skill}: {item.upstream_name} changed since {item.base}" for item in changed
            ),
            "check",
        )
    if unknown:
        return "; ".join(f"{item.skill}: {item.detail}" for item in unknown), "check"
    return f"{len(items)} current with upstream", "ok"
