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

if diff -u "$work/bash.out" "$work/py.out" >"$work/diff" 2>&1; then
	printf 'ok - Bash and Python renderers produce identical output\n'
	printf '\nRan 1 renderer-parity test(s); 0 failure(s).\n'
	exit 0
fi

printf 'not ok - Bash and Python renderers disagree\n'
cat "$work/diff"
printf '\nRan 1 renderer-parity test(s); 1 failure(s).\n'
exit 1
