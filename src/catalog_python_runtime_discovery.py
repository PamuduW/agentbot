"""Resolve the portable CPython ABI used by Agentbot's frozen dependency locks."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote

from .shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_discovery import DiscoveryError, fetch_metadata

_REPOSITORY = "astral-sh/python-build-standalone"


def resolve_python_runtime(discovery, target, *, fetch=fetch_metadata):
    if target != "linux-amd64" or any(
        discovery.get(key) != value
        for key, value in {
            "provider": "python-standalone",
            "locator": _REPOSITORY,
            "constraint": "3.12",
            "archive": "tar.gz",
            "binary": "python/bin/python3.12",
        }.items()
    ):
        raise DiscoveryError(
            "private Python discovery requires the declared CPython 3.12 Linux AMD64 build"
        )
    release = fetch(f"https://api.github.com/repos/{_REPOSITORY}/releases/latest")
    if (
        not isinstance(release, dict)
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(release.get("tag_name"), str)
        or not re.fullmatch(r"[0-9]{8}", release["tag_name"])
        or not isinstance(release.get("assets"), list)
    ):
        raise DiscoveryError("Python publisher did not provide an exact stable build")
    tag = release["tag_name"]
    pattern = re.compile(
        rf"cpython-(3\.12\.[0-9]+)\+{tag}-x86_64-unknown-linux-gnu-install_only_stripped\.tar\.gz\Z"
    )
    selected = [
        asset
        for asset in release["assets"]
        if isinstance(asset, dict)
        and isinstance(asset.get("name"), str)
        and pattern.fullmatch(asset["name"])
    ]
    if len(selected) != 1:
        raise DiscoveryError("Python build is missing or ambiguous for the selected ABI")
    asset = selected[0]
    url = (
        f"https://github.com/{_REPOSITORY}/releases/download/{tag}/{quote(asset['name'], safe='')}"
    )
    if (
        not isinstance(asset.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"])
        or type(asset.get("size")) is not int
        or not 0 < asset["size"] <= 134_217_728
        or asset.get("browser_download_url") != url
    ):
        raise DiscoveryError("Python archive identity, checksum or size differs from its publisher")
    version = pattern.fullmatch(asset["name"])[1]
    return {
        "version": f"{version}+{tag}",
        "url": url,
        "digest": asset["digest"],
        "archive": "tar.gz",
        "binary": "python/bin/python3.12",
        "package": "cpython",
    }
