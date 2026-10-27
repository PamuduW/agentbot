"""Provision a private CPython build for the catalog's frozen wheel ABI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .atomic_io import write_text_atomic
from .command_runner import CommandRunner
from .shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import (
    CatalogError,
    CatalogSnapshot,
    _object,
    _read,
    load_snapshot,
    resolve_entry,
)

from .catalog_node_runtime import _lease_lock, lease_catalog_sources, private_directory


def runtime_identity(artifact: Mapping[str, str]) -> str:
    version = artifact.get("version", "")
    match = re.fullmatch(r"(3\.12\.[0-9]+)\+([0-9]{8})", version)
    if match is None:
        raise CatalogError("Python runtime must use the frozen CPython 3.12 ABI")
    expected = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{match[2]}/cpython-{match[1]}%2B{match[2]}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    )
    if (
        artifact.get("url") != expected
        or artifact.get("archive") != "tar.gz"
        or artifact.get("binary") != "python/bin/python3.12"
        or artifact.get("package") != "cpython"
        or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")) is None
    ):
        raise CatalogError("private Python archive differs from its declared publisher/build")
    return hashlib.sha256(json.dumps(dict(artifact), sort_keys=True).encode()).hexdigest()


def payload_fingerprint(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise CatalogError("private Python payload must be a real directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        metadata = path.lstat()
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(str(stat.S_IMODE(metadata.st_mode)).encode() + b"\0")
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):
                raise CatalogError("private Python payload contains an escaping symlink")
            digest.update(b"link\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as stream:
                while chunk := stream.read(1_048_576):
                    digest.update(chunk)
        elif not path.is_dir():
            raise CatalogError("unsupported private Python payload entry")
    return digest.hexdigest()


def _environment() -> dict[str, str]:
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"}
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


def _probe(binary: Path, version: str, runner: CommandRunner) -> None:
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise CatalogError("private CPython entrypoint is missing or externally linked")
    result = runner.run(
        [
            str(binary),
            "-I",
            "-B",
            "-c",
            'import json,platform,sys; print(json.dumps({"version":platform.python_version(),"implementation":sys.implementation.name,"executable_matches":sys.executable == sys.argv[1]}))',
            str(binary),
        ],
        timeout_seconds=30,
        env=_environment(),
    )
    expected = {
        "version": version.split("+", 1)[0],
        "implementation": "cpython",
        "executable_matches": True,
    }
    try:
        actual = json.loads(result.stdout)
    except ValueError as error:
        raise CatalogError("private CPython version probe failed") from error
    if not result.ok or actual != expected:
        raise CatalogError("private CPython executable differs from the selected runtime")


def private_python_runtime(
    snapshot: CatalogSnapshot,
    authority: Path,
    runner: CommandRunner,
    *,
    qualification_sources: Mapping[str, Path] | None = None,
    install: bool = True,
) -> Path:
    from .software_versions import operation_target

    artifact = resolve_entry(
        snapshot,
        "python_runtime",
        operation_target(),
        require_approved=qualification_sources is None,
    )
    identity = runtime_identity(artifact)
    if (
        load_snapshot(snapshot.directory, require_approved=qualification_sources is None).digest
        != snapshot.digest
    ):
        raise CatalogError("private Python catalog snapshot changed")
    cache = snapshot.directory.parent / "python-runtime"
    destination = cache / identity
    payload = destination / "payload"
    binary = payload / artifact["binary"]
    receipt = destination / "receipt.json"
    if not install:
        return binary
    private_directory(cache)
    with _lease_lock(cache / f"{identity}.lock"):
        if destination.exists() or destination.is_symlink():
            private_directory(destination)
            if (
                receipt.is_symlink()
                or not receipt.is_file()
                or receipt.stat().st_uid != os.geteuid()
                or receipt.stat().st_mode & 0o077
            ):
                raise CatalogError("preserving an unowned private Python installation")
            proof = json.loads(_read(receipt, 4096), object_pairs_hook=_object)
            if proof != {
                "schema_version": 1,
                "identity": identity,
                "integrity": payload_fingerprint(payload),
            }:
                raise CatalogError("private Python payload differs from its ownership receipt")
            _probe(binary, artifact["version"], runner)
            return binary
        with lease_catalog_sources(
            snapshot, authority, runner, qualification_sources=qualification_sources
        ) as sources:
            staging = Path(tempfile.mkdtemp(prefix=".python-", dir=cache))
            try:
                archive = staging / "archive.tar.gz"
                result = runner.run(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--proto",
                        "=https",
                        "--proto-redir",
                        "=https",
                        "--max-time",
                        "180",
                        "--max-filesize",
                        "134217728",
                        "--output",
                        str(archive),
                        artifact["url"],
                    ],
                    timeout_seconds=200,
                    env=_environment(),
                )
                if (
                    not result.ok
                    or not archive.is_file()
                    or archive.is_symlink()
                    or archive.stat().st_size > 134_217_728
                    or hashlib.sha256(archive.read_bytes()).hexdigest()
                    != artifact["digest"].removeprefix("sha256:")
                ):
                    raise CatalogError(
                        "private CPython download failed its pinned checksum/size check"
                    )
                staged_payload = staging / "payload"
                staged_payload.mkdir(mode=0o700)
                environment = _environment()
                environment["DOTFILES_SHARED_DIR"] = str(sources["dotfiles-shared"])
                extracted = runner.run(
                    [
                        "bash",
                        "-c",
                        'set -euo pipefail; umask 077; source "$1/scripts/lib/software_catalog.sh"; dotfiles_catalog_extract tar.gz "$2" "$3"',
                        "catalog-python",
                        str(sources["dotfiles"]),
                        str(archive),
                        str(staged_payload),
                    ],
                    timeout_seconds=120,
                    env=environment,
                )
                if not extracted.ok:
                    raise CatalogError("private CPython archive could not be safely extracted")
                archive.unlink()
                _probe(staged_payload / artifact["binary"], artifact["version"], runner)
                if (
                    load_snapshot(
                        snapshot.directory, require_approved=qualification_sources is None
                    ).digest
                    != snapshot.digest
                ):
                    raise CatalogError("Python catalog changed during provisioning")
                proof = {
                    "schema_version": 1,
                    "identity": identity,
                    "integrity": payload_fingerprint(staged_payload),
                }
                write_text_atomic(staging / "receipt.json", json.dumps(proof) + "\n")
                (staging / "receipt.json").chmod(0o600)
                staging.rename(destination)
                try:
                    _probe(binary, artifact["version"], runner)
                except BaseException:
                    shutil.rmtree(destination)
                    raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
    return binary


def verify_python_environment(
    snapshot: CatalogSnapshot, binary: Path, runner: CommandRunner, *, qualification: bool = False
) -> None:
    from catalog_dependencies import validate_python_lock

    from .software_versions import operation_target

    artifact = resolve_entry(
        snapshot, "python.pip", operation_target(), require_approved=not qualification
    )
    lock = validate_python_lock(_read(snapshot.directory / artifact["lock"], 16_777_216))
    expected = {name: entry["version"] for name, entry in lock.items()}
    result = runner.run(
        [
            str(binary),
            "-I",
            "-B",
            "-c",
            'import importlib.metadata as m,json,re; print(json.dumps({re.sub(r"[-_.]+","-",d.metadata["Name"]).lower():d.version for d in m.distributions()}))',
        ],
        timeout_seconds=30,
        env=_environment(),
    )
    try:
        actual = json.loads(result.stdout)
    except ValueError as error:
        raise CatalogError("cannot inspect the selected Python environment") from error
    if not result.ok or actual != expected:
        raise CatalogError("installed Python distributions differ from the frozen closure")
