"""Discover exact platform VSIX bytes and their complete dependency closures."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

from .catalog_extensions import (
    BUILTIN_EXTENSIONS,
    ExtensionCatalogError,
    _closure_order,
    validate_extension_lock,
)
from .software_versions import CatalogError

_ID = re.compile(r"[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9._-]*\Z")
_STABLE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+[A-Za-z0-9.-]+)?\Z")


def _url(identity: str, version: str, platform: str | None) -> str:
    publisher, name = identity.split(".", 1)
    return (
        "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
        f"{publisher}/vsextensions/{name}/{version}/vspackage"
        + (f"?targetPlatform={platform}" if platform is not None else "")
    )


def _manifest(
    path: Path, identity: str, platform: str
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = ("extension/package.json", "extension.vsixmanifest")
            if any(archive.namelist().count(name) != 1 for name in names):
                raise CatalogError("VSIX needs one unambiguous package and platform manifest")
            if any(archive.getinfo(name).file_size > 1_048_576 for name in names):
                raise CatalogError("VSIX manifest exceeds its size limit")
            manifest = json.loads(archive.read(names[0]))
            document = ElementTree.fromstring(archive.read(names[1]))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise CatalogError("cannot read VSIX package metadata") from error
    if not isinstance(manifest, dict):
        raise CatalogError("VSIX package manifest is not an object")
    version = manifest.get("version", "")
    actual_id = f"{manifest.get('publisher')}.{manifest.get('name')}".lower()
    if actual_id != identity or not isinstance(version, str) or not _STABLE.fullmatch(version):
        raise CatalogError("VSIX did not provide its exact declared stable package identity")
    properties = {}
    selected = {
        "Microsoft.VisualStudio.Code.TargetPlatform",
        "Microsoft.VisualStudio.Code.PreRelease",
    }
    for element in document.iter():
        key = element.attrib.get("Id", "")
        if element.tag.rsplit("}", 1)[-1] == "Property" and key in selected:
            if key in properties:
                raise CatalogError("VSIX platform metadata is ambiguous")
            properties[key] = element.attrib.get("Value", "")
    if properties.get("Microsoft.VisualStudio.Code.PreRelease", "false").lower() != "false":
        raise CatalogError("VSIX publisher selected a prerelease or unverifiable channel")
    if properties.get("Microsoft.VisualStudio.Code.TargetPlatform", "") not in {
        "",
        "universal",
        "undefined",
        platform,
    }:
        raise CatalogError("VSIX publisher returned another target platform")
    dependencies = []
    for field in ("extensionDependencies", "extensionPack"):
        values = manifest.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not _ID.fullmatch(value.lower()) for value in values
        ):
            raise CatalogError("VSIX has invalid dependency identities")
        dependencies.append(tuple(sorted({value.lower() for value in values})))
    if sum(map(len, dependencies)) > 1000:
        raise CatalogError("VSIX dependency list exceeds its size limit")
    return version, dependencies[0], dependencies[1]


class ExtensionCandidateResolver:
    def __init__(
        self,
        locks: dict[str, bytes],
        *,
        download: Callable[..., None] | None = None,
        release_resolver: Callable[[str, str], tuple[str, str | None]] | None = None,
    ):
        if download is None:
            from catalog_discovery import download_artifact

            download = download_artifact
        self._download = download
        self._release = (
            release_resolver if release_resolver is not None else query_extension_release
        )
        self._locks = locks
        self._entries: dict[tuple[str, str], dict] = {}

    def _entry(self, identity: str, platform: str) -> dict:
        if identity in BUILTIN_EXTENSIONS:
            return {"builtin": True, "requires": list(BUILTIN_EXTENSIONS[identity])}
        cached = self._entries.get((identity, platform))
        if cached is not None:
            return cached
        if len(self._entries) >= 4000:
            raise CatalogError("VSIX discovery exceeded its component/platform limit")
        with tempfile.TemporaryDirectory(prefix="catalog-vsix-") as temporary:
            exact = Path(temporary) / "exact.vsix"
            version, selected_platform = self._release(identity, platform)
            stable_url = _url(identity, version, selected_platform)
            self._download(stable_url, exact, publisher="vscode-marketplace")
            exact_digest = hashlib.sha256(exact.read_bytes()).hexdigest()
            actual_version, requires, pack = _manifest(
                exact, identity, selected_platform or "universal"
            )
            if actual_version != version:
                raise CatalogError("VSIX package version differs from its exact publisher URL")
        entry = {
            "version": version,
            "url": stable_url,
            "digest": "sha256:" + exact_digest,
            "requires": list(requires),
            "pack": list(pack),
        }
        self._entries[(identity, platform)] = entry
        return entry

    def resolve(self, identity: str, target: str) -> dict[str, str]:
        identity = identity.lower()
        if not _ID.fullmatch(identity) or not re.fullmatch(
            r"(?:linux|windows|wsl2)-(?:amd64|arm64)", target
        ):
            raise CatalogError("invalid extension discovery identity or target")
        system, architecture = target.rsplit("-", 1)
        platform = (
            ("win32" if system == "windows" else "linux")
            + "-"
            + ("x64" if architecture == "amd64" else "arm64")
        )
        entries: dict[str, dict] = {}
        pending = deque([identity])
        while pending:
            key = pending.popleft()
            if key in entries:
                continue
            if len(entries) >= 1000:
                raise CatalogError("VSIX closure exceeds its component limit")
            try:
                entry = self._entry(key, platform)
            except CatalogError as error:
                raise CatalogError(f"extension dependency {key}: {error}") from error
            entries[key] = entry
            pending.extend(entry["requires"])
            pending.extend(entry.get("pack", []))
        closure = {
            "schema_version": 2,
            "extensions": entries,
        }
        try:
            validate_extension_lock(closure, identity, entries[identity]["version"])
            _closure_order(entries, (identity,))
        except ExtensionCatalogError as error:
            raise CatalogError(str(error)) from error
        raw = (json.dumps(closure, indent=2, sort_keys=True) + "\n").encode()
        digest = hashlib.sha256(raw).hexdigest()
        lock = f"locks/vsix/{identity}-{platform}-{digest}.json"
        self._locks[lock] = raw
        return {
            "version": entries[identity]["version"],
            "package": identity,
            "archive": "vsix-lock",
            "lock": lock,
            "digest": "sha256:" + digest,
        }


def select_extension_release(data, identity: str, platform: str) -> tuple[str, str | None] | None:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("results"), list)
        or len(data["results"]) != 1
    ):
        raise CatalogError("Marketplace query did not return one bounded result set")
    result = data["results"][0]
    extensions = result.get("extensions") if isinstance(result, dict) else None
    if (
        not isinstance(extensions, list)
        or len(extensions) != 1
        or not isinstance(extensions[0], dict)
    ):
        raise CatalogError("Marketplace extension is unavailable or ambiguous")
    extension = extensions[0]
    publisher = extension.get("publisher", {})
    if (
        not isinstance(publisher, dict)
        or f"{publisher.get('publisherName')}.{extension.get('extensionName')}".lower() != identity
    ):
        raise CatalogError("Marketplace returned a different publisher/extension identity")
    flags = extension.get("flags", "")
    if (
        isinstance(flags, str)
        and set(flags.lower().split(",")) & {"blocked", "disabled", "unpublished"}
    ) or (type(flags) is int and flags & 4097):
        raise CatalogError("Marketplace extension is blocked, disabled or unpublished")
    versions = extension.get("versions")
    if not isinstance(versions, list) or len(versions) > 10000:
        raise CatalogError("Marketplace version metadata is missing or oversized")
    candidates = []
    for item in versions:
        if not isinstance(item, dict) or not isinstance(item.get("version"), str):
            raise CatalogError("Marketplace version record is malformed")
        version = item["version"]
        native = item.get("targetPlatform", "")
        if not _STABLE.fullmatch(version) or native not in {
            None,
            "",
            "universal",
            "undefined",
            platform,
        }:
            continue
        properties = item.get("properties", [])
        if not isinstance(properties, list) or any(
            not isinstance(value, dict) for value in properties
        ):
            raise CatalogError("Marketplace version properties are malformed")
        prerelease = [
            value.get("value")
            for value in properties
            if value.get("key") == "Microsoft.VisualStudio.Code.PreRelease"
        ]
        if prerelease and prerelease != ["false"]:
            continue
        numeric = tuple(int(part) for part in version.split("+", 1)[0].split("."))
        candidates.append(
            ((numeric, native == platform), version, platform if native == platform else None)
        )
    if not candidates:
        return None
    highest = max(rank for rank, _version, _native in candidates)
    selected = {(version, native) for rank, version, native in candidates if rank == highest}
    if len(selected) != 1:
        raise CatalogError("Marketplace has ambiguous stable build identities")
    return selected.pop()


def _query_gallery(identity: str, *, all_versions: bool):
    import time
    import urllib.error
    import urllib.request

    from catalog_discovery import public_response_stream
    from software_catalog import _object

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            raise CatalogError("Marketplace metadata query unexpectedly redirected")

    # GalleryInterfaces.ts: Name=7, InstallationTarget=8, ExcludeWithFlags=12;
    # IncludeVersions=1, VersionProperties=16, ExcludeNonValidated=32,
    # IncludeLatestPrereleaseAndStableVersionOnly=65536.
    body = {
        "filters": [
            {
                "criteria": [
                    {"filterType": 7, "value": identity},
                    {"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                    {"filterType": 12, "value": "4097"},
                ],
                "pageNumber": 1,
                "pageSize": 1,
            }
        ],
        "flags": (1 if all_versions else 65536) | 16 | 32,
        "assetTypes": [],
    }
    request = urllib.request.Request(
        "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json;api-version=3.0-preview.1",
            "User-Agent": "agentbot-software-catalog/1",
        },
    )
    for attempt in range(3):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with (
                opener.open(request, timeout=30) as response,
                public_response_stream(response) as stream,
            ):
                raw = stream.read(8_388_609)
            if len(raw) > 8_388_608:
                raise CatalogError("Marketplace metadata exceeded its size limit")
            return json.loads(raw, object_pairs_hook=_object)
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == 2:
                raise CatalogError(
                    "Marketplace metadata unavailable after bounded retries"
                ) from error
            time.sleep(attempt + 1)
        except (ValueError, UnicodeError) as error:
            raise CatalogError("Marketplace metadata is not unique-key JSON") from error
    raise CatalogError("Marketplace metadata unavailable")


def query_extension_release(identity: str, platform: str) -> tuple[str, str | None]:
    for all_versions in (False, True):
        version = select_extension_release(
            _query_gallery(identity, all_versions=all_versions), identity, platform
        )
        if version is not None:
            return version
    raise CatalogError("Marketplace has no stable release for the required platform")
