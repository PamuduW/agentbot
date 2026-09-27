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

Only those settings are touched, and only once: ``.obsidian/agentbot.json``
records the setup version applied, so a dashboard the user deletes or a
setting the user changes afterwards stays as the user left it. Keeping
device state out of Git is not a preference, so that part is always checked.
Everything is recomputed from the current tree, so the operation converges
when several machines run it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VIEW_PATH = "views/memory.base"
MARKER = ".obsidian/agentbot.json"
KIT_VERSION = 1
CORE_PLUGINS = ".obsidian/core-plugins.json"
APP = ".obsidian/app.json"
GRAPH = ".obsidian/graph.json"
KIT_PATHS = (".gitignore", VIEW_PATH, CORE_PLUGINS, APP, GRAPH, MARKER)

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


def _safe(root: Path, relative: str) -> bool:
    """No part of the path is a symlink, so a write stays inside the vault."""
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            return False
    return True


def _read_json(root: Path, relative: str, *, create: bool = True) -> dict[str, Any] | None:
    """A settings file's object; {} when absent and creatable; None to leave it alone."""
    path = root / relative
    if not _safe(root, relative):
        return None
    if not path.exists():
        return {} if create else None
    if not path.is_file():
        return None
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
    if gitignore is not None and _safe(root, ".gitignore"):
        wanted[".gitignore"] = gitignore
    marker = _read_json(root, MARKER)
    applied = marker.get("kit") if marker is not None else None
    if marker is None or (type(applied) is int and applied >= KIT_VERSION):
        return wanted  # already set up once, or the marker is not ours to touch
    if _safe(root, VIEW_PATH) and not (root / VIEW_PATH).exists():
        wanted[VIEW_PATH] = VIEW.encode("utf-8")

    # A missing core-plugins.json is left to Obsidian: a file naming only
    # Bases could read as every other core plugin switched off.
    plugins = _read_json(root, CORE_PLUGINS, create=False)
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
    wanted[MARKER] = _dumped({**marker, "kit": KIT_VERSION})
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
