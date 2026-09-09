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
# No bytecode in the shared tree. It is vendored code held byte-identical
# between two repositories, and a stale __pycache__ masked an edit during this
# test's own development -- the source was restored, the test kept failing, and
# the cache was why.
export PYTHONDONTWRITEBYTECODE=1

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

# Both sides pinned to the same width. Pinning only the Python side made this
# compare a 100-column table against an 80-column one the moment COLUMNS was
# set, which is a difference in the test, not in the colours it is about.
FORCE_COLOR=1 DOTFILES_REPORT_COLS=80 bash -c '
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

# The update report uses a four-column layout with its own width rule and, unlike
# the three-column table, no leading indent. Checked across a spread of terminal
# widths because the action column is pinned at 18 only once there is room, so
# the rule changes shape below 64 columns.
for cols in 32 48 60 80 100 120 200; do
	NO_COLOR=1 DOTFILES_REPORT_COLS="$cols" bash -c '
		source "'"$REPO_DIR"'/scripts/lib/shared/tui/report_table.sh"
		declare -a w=()
		rt_four_column_widths w
		rt_print_four_column_header "${w[0]}" component "${w[1]}" installed \
			"${w[2]}" available "${w[3]}" action
	' 2>/dev/null
done >"$work/4bash.out"

python3 - >"$work/4py.out" <<PYEOF
import sys
sys.path.insert(0, '$sibling/scripts/lib/shared/python')
import report_table as rt
for cols in (32, 48, 60, 80, 100, 120, 200):
    w = rt.four_column_widths(max(32, cols))
    head, rule = rt.format_four_column_header(w, ("component", "installed", "available", "action"))
    print(head)
    print(rule)
PYEOF

if diff -u "$work/4bash.out" "$work/4py.out" >"$work/4diff" 2>&1; then
	printf 'ok - four-column layout matches across seven terminal widths\n'
else
	printf 'not ok - four-column layout disagrees\n'
	cat "$work/4diff" | head -12
	failures=$((failures + 1))
fi

# Neither side pinned, at several widths: the two must agree on how to work out
# how wide a table is, not merely on how to draw one when told. They did not --
# Bash honoured COLUMNS and the terminal, Python looked at neither unless
# AGENTBOT_TUI was set, so `dotfiles full-update` drew an 80-column table beside
# a 100-column one on any machine that exports COLUMNS.
width_failures=0
for width in 60 80 100 132; do
	COLUMNS="$width" NO_COLOR=1 bash -c '
		source "'"$REPO_DIR"'/scripts/lib/shared/tui/report_table.sh"
		rt_print_table_columns
	' >"$work/wbash.out" 2>&1
	COLUMNS="$width" NO_COLOR=1 python3 -c "
import sys
sys.path.insert(0, '$sibling')
from src.ui import print_table_columns
print_table_columns()
" >"$work/wpy.out" 2>&1
	if ! diff -u "$work/wbash.out" "$work/wpy.out" >"$work/wdiff" 2>&1; then
		printf 'not ok - renderers disagree about width at COLUMNS=%s\n' "$width"
		cat "$work/wdiff"
		width_failures=$((width_failures + 1))
	fi
done
if ((width_failures == 0)); then
	printf 'ok - both renderers derive the same width from the environment\n'
else
	failures=$((failures + width_failures))
fi

printf '\nRan 4 renderer-parity test(s); %d failure(s).\n' "$failures"
((failures == 0))
