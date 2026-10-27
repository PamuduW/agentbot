#!/usr/bin/env python3
"""Bootstrap only the selected private Python runtime, without ambient Python packages."""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.shared_paths import shared_python_path  # noqa: E402

sys.path.insert(0, str(shared_python_path(ROOT)))
from software_catalog import CatalogError  # noqa: E402

from src.catalog_python_runtime import (  # noqa: E402
    private_python_runtime,
    runtime_identity,
    verify_python_environment,
)
from src.command_runner import CommandRunner  # noqa: E402
from src.shared_paths import shared_root  # noqa: E402
from src.software_versions import (  # noqa: E402
    operation_snapshot,
    operation_target,
    qualification_manifest_operation,
    resolve_entry,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-context", type=Path)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--verify-environment", type=Path)
    args = parser.parse_args()
    try:
        context = (
            qualification_manifest_operation(args.qualification_context, ROOT)
            if args.qualification_context
            else nullcontext(operation_snapshot(ROOT))
        )
        with context as snapshot:
            if snapshot.digest != args.digest:
                raise CatalogError("Python runtime and dependency lock must use the same catalog")
            if args.verify_environment:
                verify_python_environment(
                    snapshot,
                    args.verify_environment,
                    CommandRunner(),
                    qualification=args.qualification_context is not None,
                )
                return 0
            sources = None
            if args.qualification_context:
                from catalog_context import load_qualification_context

                _snapshot, sources, _digest = load_qualification_context(args.qualification_context)
            artifact = resolve_entry(snapshot, "python_runtime", operation_target())
            binary = private_python_runtime(
                snapshot, shared_root(ROOT), CommandRunner(), qualification_sources=sources
            )
            print(f"{binary}\t{runtime_identity(artifact)}")
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
