"""Names any result word on stdin that the result vocabulary does not know.

Reads a rendered surface, prints one line per unknown result. Used by
tests/test_report_contract.sh, which owns the list of surfaces to sweep.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ui.table import result_class


def main() -> int:
    unknown = []
    for line in sys.stdin.read().splitlines():
        if not line.startswith("  ") or " | " not in line:
            continue
        # A wrapped detail's continuation lines close on the separator with no
        # result cell after it, so they end with "|" once the padding is gone.
        if line.rstrip().endswith("|"):
            continue
        result = line.rsplit(" | ", 1)[-1].strip()
        # The column header and the rule beneath it carry no result either.
        if not result or result == "result" or set(result) <= set("-+ "):
            continue
        if result_class(result) == "unknown":
            unknown.append(result)
    for result in unknown:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
