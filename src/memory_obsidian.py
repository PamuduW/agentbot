"""The vault's Obsidian setup, delivered by the sync engine.

A vault that has been opened in Obsidian (it has an ``.obsidian/`` folder) gets
one idempotent operation that:

- ignores and stops tracking Obsidian's per-device files;
- adds ``views/memory.base``, a Bases dashboard, unless one exists;
- enables the Bases core plugin, makes new links absolute vault paths (the
  form Agentbot writes), updates links on rename without asking, and excludes
  ``templates/`` and ``exports/`` from search and the graph;
- colours core, projects, and proposals in the graph, if no colour groups
  exist yet.

Only those settings are touched. Everything is recomputed from the current
tree, so the operation converges when several machines run it, and it never
overwrites a dashboard or colour groups the user already has.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VIEW_PATH = "views/memory.base"
CORE_PLUGINS = ".obsidian/core-plugins.json"
APP = ".obsidian/app.json"
GRAPH = ".obsidian/graph.json"
KIT_PATHS = (".gitignore", VIEW_PATH, CORE_PLUGINS, APP, GRAPH)

APP_SETTINGS: dict[str, Any] = {
    "newLinkFormat": "absolute",
    "useMarkdownLinks": False,
    "alwaysUpdateLinks": True,
}
EXCLUDED = ("templates/", "exports/")
COLOR_GROUPS = [
    {"query": "path:core", "color": {"a": 1, "rgb": 5431473}},
    {"query": "path:projects", "color": {"a": 1, "rgb": 3066993}},
    {"query": "path:proposals", "color": {"a": 1, "rgb": 15105570}},
]

VIEW = """\
# Memory dashboard, added by Agentbot. Edit freely: Agentbot never overwrites it.
filters:
  and:
    - file.ext == "md"
    - file.hasProperty("schema")
properties:
  note.type:
    displayName: Type
  note.status:
    displayName: Status
  note.date:
    displayName: Date
views:
  - type: table
    name: Project memory
    filters:
      and:
        - file.inFolder("projects")
        - status == "accepted"
    groupBy:
      property: file.folder
      direction: ASC
    order:
      - file.name
      - note.type
      - note.date
      - note.tags
  - type: table
    name: Core memory
    filters:
      and:
        - file.inFolder("core")
        - status == "accepted"
    order:
      - file.name
      - note.type
      - note.scope
      - note.date
  - type: table
    name: Waiting for your approval
    filters: file.inFolder("proposals")
    order:
      - file.name
      - note.type
      - note.scope
      - note.date
  - type: table
    name: Due for review
    filters:
      or:
        - and:
            - file.hasProperty("review_after")
            - date(review_after) <= today()
        - and:
            - file.hasProperty("valid_until")
            - date(valid_until) < today()
    order:
      - file.name
      - note.review_after
      - note.valid_until
  - type: table
    name: Superseded and retired
    filters:
      and:
        - status != "accepted"
        - status != "draft"
    order:
      - file.name
      - note.status
      - note.date
"""


def _read_json(root: Path, relative: str) -> dict[str, Any] | None:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        return {} if not path.exists() else None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None  # the user's file is broken: leave it alone
    return data if isinstance(data, dict) else None


def _dumped(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def changes(root: Path) -> dict[str, bytes]:
    """The kit files whose content should change, and their new bytes."""
    from .memory_sync import gitignore_with_device_state

    wanted: dict[str, bytes] = {}
    gitignore = gitignore_with_device_state(root)
    if gitignore is not None:
        wanted[".gitignore"] = gitignore
    if not (root / VIEW_PATH).exists() and not (root / VIEW_PATH).is_symlink():
        wanted[VIEW_PATH] = VIEW.encode("utf-8")

    plugins = _read_json(root, CORE_PLUGINS)
    if plugins is not None and plugins.get("bases") is not True:
        wanted[CORE_PLUGINS] = _dumped({**plugins, "bases": True})

    app = _read_json(root, APP)
    if app is not None:
        updated = {**app, **APP_SETTINGS}
        excluded = list(app.get("userIgnoreFilters") or [])
        updated["userIgnoreFilters"] = excluded + [
            item for item in EXCLUDED if item not in excluded
        ]
        if updated != app:
            wanted[APP] = _dumped(updated)

    graph = _read_json(root, GRAPH)
    if graph is not None and not graph.get("colorGroups"):
        wanted[GRAPH] = _dumped({**graph, "colorGroups": COLOR_GROUPS})
    return wanted


def opened_in_obsidian(root: Path) -> bool:
    return (root / ".obsidian").is_dir() and not (root / ".obsidian").is_symlink()


def apply(root: Path) -> bool:
    """Write the kit files. True when anything was written."""
    wanted = changes(root)
    for relative, data in wanted.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bool(wanted)
