#!/usr/bin/env python3
"""Exercise real managed Python and Skills CLI installs without approving a catalog."""

from __future__ import annotations

import argparse
import json
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.shared_paths import shared_python_path  # noqa: E402 - Bootstrap source import path.

sys.path.insert(0, str(shared_python_path(ROOT)))
from catalog_context import load_qualification_context  # noqa: E402 - Shared source path.
from catalog_dependencies import validate_python_lock  # noqa: E402 - Shared source path.
from software_catalog import CatalogError  # noqa: E402 - Shared source path.


def smoke(manifest: Path) -> dict:
    from src.command_runner import CommandRunner
    from src.software_versions import (
        approved_npm_executable,
        qualification_manifest_operation,
        resolve_entry,
    )

    _snapshot, sources, _ = load_qualification_context(manifest)
    if sources["agentbot"] != ROOT:
        raise CatalogError("tooling smoke requires its frozen Agentbot checkout")
    operating_system = platform.freedesktop_os_release()
    if (
        sys.version_info[:2] != (3, 12)
        or platform.machine() != "x86_64"
        or operating_system.get("ID") != "ubuntu"
        or operating_system.get("VERSION_ID") != "24.04"
        or "microsoft" in platform.release().lower()
    ):
        raise CatalogError("tooling smoke requires native Ubuntu 24.04 AMD64 and Python 3.12")
    home = Path.home()
    if home.stat().st_uid != os.geteuid() or stat.S_IMODE(home.stat().st_mode) != 0o700:
        raise CatalogError("tooling smoke needs a private qualification home")
    if (ROOT / ".venv").exists() or (ROOT / ".venv").is_symlink():
        raise CatalogError("fresh tooling smoke requires an unused Agentbot environment")
    command = [
        "bash",
        "-c",
        'set -euo pipefail; AGENTBOT_HOME="$1"; AGENTBOT_QUALIFICATION_CONTEXT="$2"; '
        'source "$1/scripts/lib/shared_resolve.sh"; dotfiles_shared_require "$1"; '
        'source "$1/scripts/lib/python_env.sh"; agentbot_python_ensure',
        "bash",
        str(ROOT),
        str(manifest),
    ]
    runner = CommandRunner()
    first = runner.run(command, timeout_seconds=300)
    if not first.ok:
        raise CatalogError("fresh Python provisioning failed: " + first.detail())
    release = (ROOT / ".venv").resolve()
    interpreter = release / "bin/python"
    repeated = runner.run(command, timeout_seconds=300)
    if not repeated.ok or (ROOT / ".venv").resolve() != release:
        raise CatalogError("repeat provisioning did not retain the Python release")
    with qualification_manifest_operation(manifest, ROOT) as snapshot:
        lock = snapshot.directory / resolve_entry(snapshot, "python.pip", "linux-amd64")["lock"]
        expected = validate_python_lock(lock.read_bytes())
        observed = runner.run(
            [
                str(interpreter),
                "-I",
                "-c",
                'import importlib.metadata as m,json,re; print(json.dumps({re.sub(r"[-_.]+","-",d.metadata["Name"]).lower():d.version for d in m.distributions()}))',
            ],
            timeout_seconds=30,
        )
        if not observed.ok:
            raise CatalogError("cannot inspect installed Python distributions")
        packages = json.loads(observed.stdout)
        if any(packages.get(key) != value["version"] for key, value in expected.items()):
            raise CatalogError("installed Python distributions differ from the selected lock")
        executable = approved_npm_executable(ROOT, "skills_cli", runner)
        same = approved_npm_executable(ROOT, "skills_cli", runner)
        if executable != same:
            raise CatalogError("repeat Skills CLI provisioning changed its path")
        result = runner.run([str(executable), "--version"], timeout_seconds=60)
        version = resolve_entry(snapshot, "skills_cli", "linux-amd64")["version"]
        if not result.ok or result.stdout.strip().removeprefix("v") != version:
            raise CatalogError("Skills CLI version probe differs from the candidate")
    load_qualification_context(manifest)
    return {
        "schema_version": 1,
        "scope": "tooling-smoke-only",
        "catalog_digest": snapshot.digest,
        "sources": dict(snapshot.data["sources"]),
        "python_packages": {key: packages[key] for key in expected},
        "skills_cli_version": version,
        "fresh": "success",
        "repeat": "success",
        "approved": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise CatalogError("preserving an existing tooling evidence path")
        result = smoke(args.context.resolve())
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    except (CatalogError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
