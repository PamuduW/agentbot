#!/usr/bin/env bash
# shellcheck shell=bash
set -uo pipefail

# The same three-column report is rendered twice: in Bash
# (scripts/lib/shared/tui/report_table.sh) and in Python (src/ui/table.py).
# ADR-0001 consolidates on the Python one, and until that migration finishes
# both must produce identical bytes -- otherwise a `dotfiles full-update`, which
# prints tables from both tools in one session, shows two subtly different
# designs.
#
# This already caught three divergences: the Python side did not shorten $HOME
# to ~, did not pad the result cell, and did not pad the result header.
#
# Every replacement the migration makes is verifiable against this, rather than
# hopeful.

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$TEST_DIR/.." && pwd)"

# Both renderers live in this repository: the Bash one in
# scripts/lib/shared/tui/report_table.sh, and the Python one in src/ui/table.py.
# sync-shared.sh keeps the Bash copy byte-identical with Dotfiles, so checking
# this copy covers both repositories without either looking at the other.
if ! command -v python3 >/dev/null 2>&1; then
	printf 'ok - renderer parity skipped; python3 unavailable\n'
	exit 0
fi
sibling="$REPO_DIR"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

# Rows chosen for the cases the two renderers disagreed on: a home path (~
# shortening), a path long enough to need the middle ellipsis paths get rather
# than the trailing one text gets, an over-long label, and plain short values.
cat >"$work/rows" <<ROWS
Installed skills|60|ok
Global AGENTS.md|global/AGENTS.md|ok
wsl host|$HOME/.vscode-server|ok
CLI version|boost v0.13.11|ok
A very long component name that must truncate|short|check
Statusline|ready: $HOME/.cursor/statusline-command.sh|missing
Home itself|$HOME|ok
ROWS

NO_COLOR=1 bash -c '
	source "'"$REPO_DIR"'/scripts/lib/shared/tui/report_table.sh"
	rt_print_table_columns
	while IFS="|" read -r component detail result; do
		rt_print_table_row "$component" "$detail" "$result"
	done
' <"$work/rows" >"$work/bash.out" 2>&1

NO_COLOR=1 python3 -c "
import sys
sys.path.insert(0, '$sibling')
from src.ui import print_table_columns, print_table
rows = [tuple(line.rstrip('\n').split('|')) for line in open('$work/rows')]
print_table_columns()
print_table(rows, show_header=False)
" >"$work/py.out" 2>&1

failures=0
if diff -u "$work/bash.out" "$work/py.out" >"$work/diff" 2>&1; then
	printf 'ok - Bash and Python renderers produce identical layout\n'
else
	printf 'not ok - Bash and Python renderers disagree on layout\n'
	cat "$work/diff"
	failures=$((failures + 1))
fi

# Colour is a second contract and was the one that drifted: `skipped` rendered
# dim in Bash and yellow in Python, and every row compared above runs under
# NO_COLOR, so nothing caught it. Every state either mapping knows is listed.
cat >"$work/crows" <<'ROWS'
ok|x|ok
installed|x|installed
configured|x|configured
linked|x|linked
current|x|current
applied|x|applied
read-only|x|read-only
missing|x|missing
failed|x|failed
error|x|error
conflict|x|conflict
check|x|check
drift|x|drift
extra|x|extra
warn|x|warn
warning|x|warning
partial|x|partial
mutating|x|mutating
applied-with-local-changes|x|applied-with-local-changes
skipped|x|skipped
skipped (nothing declared)|x|skipped (nothing declared)
info|x|info
dry-run|x|dry-run
preview|x|preview
unmapped|x|something-nobody-maps
ROWS

FORCE_COLOR=1 bash -c '
	source "'"$REPO_DIR"'/scripts/lib/shared/tui/report_table.sh"
	while IFS="|" read -r component detail result; do
		rt_print_table_row "$component" "$detail" "$result"
	done
' <"$work/crows" >"$work/cbash.out" 2>&1

AGENTBOT_TUI=1 AGENTBOT_MENU_COLS=80 python3 -c "
import sys
sys.path.insert(0, '$sibling')
from src.ui import print_table
rows = [tuple(line.rstrip(chr(10)).split('|')) for line in open('$work/crows')]
print_table(rows, show_header=False)
" >"$work/cpy.out" 2>&1

if diff -u "$work/cbash.out" "$work/cpy.out" >"$work/cdiff" 2>&1; then
	printf 'ok - Bash and Python renderers colour every result state alike\n'
else
	printf 'not ok - Bash and Python renderers disagree on colour\n'
	cat -v "$work/cdiff" | tail -12
	failures=$((failures + 1))
fi

printf '\nRan 2 renderer-parity test(s); %d failure(s).\n' "$failures"
((failures == 0))
