"""Stage exact VSIX dependency closures before changing a VS Code host."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from collections import deque
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .command_runner import CommandRunner
from .software_versions import operation_snapshot, operation_target, resolve_entry

_ID = re.compile(r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9._-]*\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLATFORMS = frozenset({"linux-x64", "linux-arm64", "win32-x64", "win32-arm64"})
# Shipped by microsoft/vscode under extensions/; never Marketplace packages.
BUILTIN_EXTENSIONS = {
    "vscode.github-authentication": (),
    "vscode.git": ("vscode.git-base",),
    "vscode.git-base": (),
    "vscode.docker": (),
    "vscode.powershell": (),
    "vscode.yaml": (),
    "vscode.json": (),
}


class ExtensionCatalogError(ValueError):
    """A selected extension cannot be installed from approved bytes."""


class UnqualifiedExtensionHost(ExtensionCatalogError):
    """This host is explicitly outside automatic catalog qualification."""


def validate_extension_lock(data: object, identity: str, version: str) -> dict[str, dict]:
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "extensions"}
        or type(data["schema_version"]) is not int
        or data["schema_version"] not in {1, 2}
    ):
        raise ExtensionCatalogError("unsupported VSIX closure schema")
    entries = data["extensions"]
    if not isinstance(entries, dict) or not entries or len(entries) > 1000:
        raise ExtensionCatalogError("empty or oversized VSIX closure")
    for key, entry in entries.items():
        if not isinstance(key, str) or not _ID.fullmatch(key) or not isinstance(entry, dict):
            raise ExtensionCatalogError("invalid extension identity")
        if entry.get("builtin") is True:
            if (
                data["schema_version"] != 2
                or key not in BUILTIN_EXTENSIONS
                or entry != {"builtin": True, "requires": list(BUILTIN_EXTENSIONS[key])}
                or any(dependency not in entries for dependency in BUILTIN_EXTENSIONS[key])
            ):
                raise ExtensionCatalogError("unknown or malformed built-in editor dependency")
            continue
        fields = {"version", "url", "digest", "requires"}
        if data["schema_version"] == 2:
            fields.add("pack")
        if set(entry) != fields:
            raise ExtensionCatalogError("VSIX closure entry schema mismatch")
        if any(not isinstance(entry[field], str) for field in ("version", "url", "digest")):
            raise ExtensionCatalogError("VSIX version, URL and integrity must be text")
        parsed = urlsplit(entry["url"])
        publisher, name = key.split(".", 1)
        expected_path = (
            f"/_apis/public/gallery/publishers/{publisher}/vsextensions/"
            f"{name}/{entry['version']}/vspackage"
        )
        if (
            not _VERSION.fullmatch(entry["version"])
            or not _DIGEST.fullmatch(entry["digest"])
            or parsed.scheme != "https"
            or parsed.netloc != "marketplace.visualstudio.com"
            or parsed.username
            or parsed.password
            or parsed.path != expected_path
            or (
                parsed.query
                and parsed.query not in {f"targetPlatform={value}" for value in _PLATFORMS}
            )
            or parsed.fragment
        ):
            raise ExtensionCatalogError("VSIX closure lacks exact publisher version/URL/integrity")
        dependencies = entry["requires"]
        pack = entry.get("pack", [])
        if (
            not isinstance(pack, list)
            or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in pack)
            or len(set(pack)) != len(pack)
        ):
            raise ExtensionCatalogError("VSIX extension pack is malformed")
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or any(dependency not in entries for dependency in [*dependencies, *pack])
        ):
            raise ExtensionCatalogError("VSIX dependency closure is incomplete")
    if identity not in entries or entries[identity].get("version") != version:
        raise ExtensionCatalogError("VSIX root differs from the catalog version")
    return entries


def verify_builtin_extensions(entries: Mapping[str, dict], editor_cli: Path | None) -> None:
    required = {key for key, entry in entries.items() if entry.get("builtin") is True}
    if not required:
        return
    if editor_cli is None:
        raise ExtensionCatalogError(
            "cannot verify built-in dependencies without the selected editor"
        )
    editor = editor_cli.resolve().parent.parent
    roots = (editor / "extensions", editor / "resources/app/extensions")
    for identity in sorted(required):
        if identity not in BUILTIN_EXTENSIONS:
            raise ExtensionCatalogError(f"unknown built-in editor dependency: {identity}")
        manifests = [root / identity.split(".", 1)[1] / "package.json" for root in roots]
        manifests = [path for path in manifests if path.is_file() and not path.is_symlink()]
        if len(manifests) != 1 or manifests[0].stat().st_size > 1_048_576:
            raise ExtensionCatalogError(
                f"selected editor lacks its built-in dependency: {identity}"
            )
        try:
            manifest = json.loads(manifests[0].read_text())
        except (OSError, ValueError) as error:
            raise ExtensionCatalogError(
                f"cannot read built-in editor dependency: {identity}"
            ) from error
        if (
            not isinstance(manifest, dict)
            or f"{manifest.get('publisher')}.{manifest.get('name')}".lower() != identity
            or not isinstance(manifest.get("version"), str)
            or not _VERSION.fullmatch(manifest["version"])
        ):
            raise ExtensionCatalogError(
                f"selected editor has an invalid built-in dependency: {identity}"
            )


def _download_vsix(
    identity: str, entry: Mapping[str, str], destination: Path, runner: CommandRunner
) -> None:
    result = runner.run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--retry",
            "2",
            "--connect-timeout",
            "15",
            "--max-time",
            "120",
            "--max-filesize",
            "268435456",
            "--output",
            str(destination),
            entry["url"],
        ],
        timeout_seconds=390,
    )
    if not result.ok or not destination.is_file():
        raise ExtensionCatalogError(f"approved VSIX is unavailable: {identity}")
    if "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest() != entry["digest"]:
        raise ExtensionCatalogError(f"approved VSIX integrity mismatch: {identity}")
    try:
        with zipfile.ZipFile(destination) as archive:
            manifest_info = archive.getinfo("extension/package.json")
            if manifest_info.file_size > 1_048_576:
                raise ExtensionCatalogError("VSIX manifest is oversized")
            manifest = json.loads(archive.read(manifest_info))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise ExtensionCatalogError(f"cannot verify VSIX manifest: {identity}") from error
    package_id = f"{manifest.get('publisher')}.{manifest.get('name')}".lower()
    requirements = {name.lower() for name in manifest.get("extensionDependencies", [])}
    pack = {name.lower() for name in manifest.get("extensionPack", [])}
    if "pack" not in entry:
        requirements |= pack
        pack = set()
    if (
        package_id != identity
        or manifest.get("version") != entry["version"]
        or requirements != set(entry["requires"])
        or pack != set(entry.get("pack", []))
    ):
        raise ExtensionCatalogError(f"VSIX manifest differs from its frozen closure: {identity}")


def _closure_order(entries: Mapping[str, dict], requested: tuple[str, ...]) -> tuple[str, ...]:
    reachable: dict[str, None] = {}
    pending = deque(requested)
    while pending:
        identity = pending.popleft()
        if identity in reachable:
            continue
        if identity not in entries:
            raise ExtensionCatalogError("VSIX dependency closure is incomplete")
        reachable[identity] = None
        pending.extend(entries[identity]["requires"])
        pending.extend(entries[identity].get("pack", []))
    ordered: list[str] = []
    active: set[str] = set()

    def visit(identity: str) -> None:
        if identity in active:
            raise ExtensionCatalogError("cyclic VSIX dependency closure")
        if identity in ordered:
            return
        active.add(identity)
        for dependency in entries[identity]["requires"]:
            visit(dependency)
        active.remove(identity)
        ordered.append(identity)

    for identity in reachable:
        visit(identity)
    return tuple(ordered)


@contextmanager
def staged_extensions(
    root: Path,
    requested: tuple[str, ...],
    host: str,
    runner: CommandRunner,
    *,
    extensions_directory: Path | None = None,
    editor_cli: Path | None = None,
):
    """Yield dependency-first VSIX files. Callers own host selection and CLI execution."""
    snapshot = operation_snapshot(root)
    if host == "windows" and not any(
        target.startswith(("windows-", "wsl2-")) for target in snapshot.data["required_targets"]
    ):
        raise UnqualifiedExtensionHost(
            "Windows extensions are managed manually in this Linux-only catalog"
        )
    target = (
        "windows-" + operation_target().rsplit("-", 1)[-1]
        if host == "windows"
        else operation_target()
    )
    entries: dict[str, dict] = {}
    for identifier in requested:
        identity = identifier.lower()
        artifact = resolve_entry(snapshot, "extension." + identity, target)
        if "lock" not in artifact:
            raise ExtensionCatalogError(
                "VS Code installation requires a frozen VSIX dependency closure"
            )
        closure = validate_extension_lock(
            json.loads((snapshot.directory / artifact["lock"]).read_text()),
            identity,
            artifact["version"],
        )
        for key, entry in closure.items():
            if key in entries and entries[key] != entry:
                raise ExtensionCatalogError("VSIX closures disagree about a dependency")
            entries[key] = entry
    order = _closure_order(entries, tuple(identifier.lower() for identifier in requested))
    verify_builtin_extensions(entries, editor_cli)
    entries = {key: entry for key, entry in entries.items() if entry.get("builtin") is not True}
    order = tuple(key for key in order if key in entries)
    pending = (
        set(preflight_installs(extensions_directory, entries))
        if extensions_directory is not None
        else set(order)
    )
    with tempfile.TemporaryDirectory(prefix="agentbot-vsix-") as temporary:
        paths: dict[str, Path] = {}
        for identity in order:
            if identity not in pending:
                continue
            path = Path(temporary) / f"{identity}-{entries[identity]['version']}.vsix"
            _download_vsix(identity, entries[identity], path, runner)
            paths[identity] = path
        yield paths, entries


def installed_version(directory: Path, identity: str) -> str | None:
    versions: set[str] = set()
    if not directory.is_dir():
        return None
    for path in directory.iterdir():
        if not path.name.lower().startswith(identity + "-") or not path.is_dir():
            continue
        manifest_path = path / "package.json"
        if path.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            raise ExtensionCatalogError(f"cannot verify installed extension: {identity}")
        manifest = json.loads(manifest_path.read_text())
        found = f"{manifest.get('publisher')}.{manifest.get('name')}".lower()
        version = manifest.get("version", "")
        if found != identity or not _VERSION.fullmatch(version):
            raise ExtensionCatalogError(
                f"installed extension has inconsistent provenance: {identity}"
            )
        versions.add(version)
    if not versions:
        return None
    return max(
        versions,
        key=lambda version: (
            tuple(map(int, version.split("-", 1)[0].split("+", 1)[0].split("."))),
            "-" not in version,
            version,
        ),
    )


def read_ownership(directory: Path) -> dict[str, dict]:
    path = directory / ".agentbot-catalog.json"
    if not path.exists() and not path.is_symlink():
        return {}
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_048_576:
        raise ExtensionCatalogError("extension ownership receipt must be a bounded regular file")
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "extensions"}
        or data["schema_version"] != 1
        or not isinstance(data["extensions"], dict)
    ):
        raise ExtensionCatalogError("invalid extension ownership receipt")
    return data["extensions"]


def preflight_installs(directory: Path, entries: Mapping[str, dict]) -> tuple[str, ...]:
    owned = read_ownership(directory)
    pending: list[str] = []
    for identity, artifact in entries.items():
        installed = installed_version(directory, identity)
        if installed is None:
            pending.append(identity)
            continue
        if installed == artifact["version"]:
            continue
        receipt = owned.get(identity)
        if not isinstance(receipt, dict) or receipt.get("version") != installed:
            raise ExtensionCatalogError(f"extension is externally managed: {identity}")
        installed_core = tuple(map(int, installed.split("-", 1)[0].split("+", 1)[0].split(".")))
        desired_core = tuple(
            map(int, artifact["version"].split("-", 1)[0].split("+", 1)[0].split("."))
        )
        if installed_core > desired_core:
            raise ExtensionCatalogError(
                f"installed extension is newer than approved; review drift: {identity}"
            )
        pending.append(identity)
    return tuple(pending)


def record_installs(
    directory: Path, entries: Mapping[str, dict], installed: tuple[str, ...]
) -> None:
    from .atomic_io import write_text_atomic

    owned = read_ownership(directory)
    for identity in installed:
        if installed_version(directory, identity) != entries[identity]["version"]:
            raise ExtensionCatalogError(f"extension version verification failed: {identity}")
        owned[identity] = {
            "version": entries[identity]["version"],
            "digest": entries[identity]["digest"],
        }
    write_text_atomic(
        directory / ".agentbot-catalog.json",
        json.dumps({"schema_version": 1, "extensions": owned}, indent=2) + "\n",
    )
