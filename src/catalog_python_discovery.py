"""Resolve Agentbot runtime/development profiles using its source constraints."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from .shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_dependencies import generate_python_lock, validate_python_lock
from software_catalog import CatalogError, _read


class PythonCandidateResolver:
    def __init__(self, root: Path, locks: dict[str, bytes], *, uv: Path, uv_version: str):
        self.root = root
        self.locks = locks
        self.tools = {
            "uv": uv,
            "uv_version": uv_version,
            "python_version": "3.12",
            "target": "linux-amd64",
        }
        self.profiles: dict[str, tuple[dict, str]] = {}

    def _prepare(self) -> None:
        runtime = _read(self.root / "requirements.txt", 1_048_576).decode() + "\npip\n"
        development = _read(self.root / "requirements-dev.txt", 1_048_576).decode()
        full = generate_python_lock(runtime + "\n" + development, **self.tools)
        minimal = generate_python_lock(runtime, constraints=full, **self.tools)
        dev_packages, runtime_packages = validate_python_lock(full), validate_python_lock(minimal)
        if any(dev_packages.get(name) != entry for name, entry in runtime_packages.items()):
            raise CatalogError("Python runtime and development profiles selected different bytes")
        for profile, raw, packages in (
            ("runtime", minimal, runtime_packages),
            ("development", full, dev_packages),
        ):
            digest = hashlib.sha256(raw).hexdigest()
            path = f"locks/python/agentbot-{profile}-py312-linux-amd64-{digest}.txt"
            self.locks[path] = raw
            self.profiles[profile] = (packages, path)

    def resolve(self, package: str, target: str) -> dict[str, str]:
        if target != "linux-amd64":
            raise CatalogError("Python candidate profiles qualify Linux AMD64 only")
        if not self.profiles:
            self._prepare()
        package = re.sub(r"[-_.]+", "-", package).lower()
        for profile in ("runtime", "development"):
            packages, path = self.profiles[profile]
            if package in packages:
                return {
                    "version": packages[package]["version"],
                    "package": package,
                    "lock": path,
                    "digest": "sha256:" + hashlib.sha256(self.locks[path]).hexdigest(),
                    "archive": "python-lock",
                }
        raise CatalogError("Python catalog package is missing from the declared profiles")
