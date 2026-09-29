"""The vault's Obsidian setup, delivered by the sync engine.

Obsidian keeps its settings in ``.obsidian/`` and its Bases views in
``views/``, and rewrites both itself: on open, as you zoom the graph, as you
resize a table. In Git every such rewrite read as a hand edit and paused
sync, so both folders are per machine: ignored, and no longer tracked.

A vault that has been opened in Obsidian gets:

- shared, through one idempotent sync operation: those folders and the trash
  in a managed ``.gitignore`` block, and anything in them untracked (each
  machine keeps its own copy);
- per machine, never committed, once: ``views/memory.base``, a Bases
  dashboard, unless one exists; the Bases core plugin on; new links as
  absolute vault paths (the form Agentbot writes), updated on rename without
  asking; ``templates/`` and ``exports/`` excluded from search and the graph;
  and core, projects, and proposals coloured in the graph if no colour
  groups exist yet.

``.obsidian/agentbot-device.json`` records that the per-machine setup ran, so
a dashboard the user deletes, or a setting the user changes, stays as the
user left it on that machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VIEW_PATH = "views/memory.base"
DEVICE_MARKER = ".obsidian/agentbot-device.json"
KIT_VERSION = 1
CORE_PLUGINS = ".obsidian/core-plugins.json"
APP = ".obsidian/app.json"
GRAPH = ".obsidian/graph.json"
# What the shared operation commits. Everything else here is per machine.
KIT_PATHS = (".gitignore",)

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

# The dashboard in the form Obsidian saves it, so opening it changes nothing.
VIEW = """\
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
      - type
      - date
      - tags
  - type: table
    name: Core memory
    filters:
      and:
        - file.inFolder("core")
        - status == "accepted"
    order:
      - file.name
      - type
      - scope
      - date
  - type: table
    name: Waiting for your approval
    filters: file.inFolder("proposals")
    order:
      - file.name
      - type
      - scope
      - date
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
      - review_after
      - valid_until
  - type: table
    name: Superseded and retired
    filters:
      and:
        - status != "accepted"
        - status != "draft"
    order:
      - file.name
      - status
      - date
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
    # Obsidian's own format (two-space indent, no final newline), so its next
    # save changes nothing.
    return json.dumps(data, indent=2).encode("utf-8")


def changes(root: Path) -> dict[str, bytes]:
    """The shared kit files whose content should change: the .gitignore block."""
    from .memory_sync import gitignore_with_device_state

    gitignore = gitignore_with_device_state(root)
    if gitignore is None or not _safe(root, ".gitignore"):
        return {}
    return {".gitignore": gitignore}


def device_changes(root: Path) -> dict[str, bytes]:
    """This machine's settings files to write, once, and never commit."""
    if not opened_in_obsidian(root):
        return {}
    marker = _read_json(root, DEVICE_MARKER)
    applied = marker.get("kit") if marker is not None else None
    if marker is None or (type(applied) is int and applied >= KIT_VERSION):
        return {}
    wanted: dict[str, bytes] = {}
    if _safe(root, VIEW_PATH) and not (root / VIEW_PATH).exists():
        wanted[VIEW_PATH] = VIEW.encode("utf-8")
    # A missing core-plugins.json is left to Obsidian: a file naming only
    # Bases could read as every other core plugin switched off.
    plugins = _read_json(root, CORE_PLUGINS, create=False)
    if plugins is not None and plugins.get("bases") is not True:
        wanted[CORE_PLUGINS] = _dumped({**plugins, "bases": True})

    app = _read_json(root, APP)
    excluded = (app.get("userIgnoreFilters") or []) if app is not None else None
    # A value Obsidian would not have written is the user's to fix: leave the
    # whole file alone rather than fail the memory command that got here.
    if (
        app is not None
        and isinstance(excluded, list)
        and all(isinstance(item, str) for item in excluded)
    ):
        updated = {**app, **APP_SETTINGS}
        updated["userIgnoreFilters"] = excluded + [
            item for item in EXCLUDED if item not in excluded
        ]
        if updated != app:
            wanted[APP] = _dumped(updated)

    graph = _read_json(root, GRAPH)
    if graph is not None and not graph.get("colorGroups"):
        wanted[GRAPH] = _dumped({**graph, "colorGroups": COLOR_GROUPS})
    wanted[DEVICE_MARKER] = _dumped({**marker, "kit": KIT_VERSION})
    return wanted


def opened_in_obsidian(root: Path) -> bool:
    return (root / ".obsidian").is_dir() and not (root / ".obsidian").is_symlink()


def _write(root: Path, wanted: dict[str, bytes]) -> bool:
    for relative, data in wanted.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bool(wanted)


def apply(root: Path) -> bool:
    """Write the shared kit file. True when anything was written."""
    return _write(root, changes(root))


def apply_device(root: Path) -> bool:
    """Write this machine's settings once. True when anything was written."""
    return _write(root, device_changes(root))
