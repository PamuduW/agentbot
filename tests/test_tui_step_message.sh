#!/usr/bin/env bash
# shellcheck shell=bash
set -uo pipefail

# What the progress animation says underneath a [STEP] line.
#
# The relay starts that animation by re-reading a line the backend has already
# printed, so it has to undo the backend's own formatting to recover the step
# name. It once undid only the indentation: install_log.py colours its markers
# -- so they match in a Dotfiles full update, where this relay is not running
# to paint them -- and the escape sequence in front of "[STEP]" made the marker
# strip miss. The animation then drew the marker a second time, under the line
# that already carried it:
#
#   [STEP] Installing skill source: deslop (owner/repo)
#     ⠋ [STEP] Installing skill source: deslop (owner/repo)

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/tui.sh"

passed=0
failed=0
esc=$'\033'
want='Installing skill source: deslop (owner/repo)'

check() {
	local label="$1" got="$2" expected="$3"
	if [[ "$got" == "$expected" ]]; then
		printf 'ok - %s\n' "$label"
		passed=$((passed + 1))
	else
		printf 'not ok - %s\n   want [%s]\n    got [%s]\n' "$label" "$expected" "$got"
		failed=$((failed + 1))
	fi
}

check 'plain line loses its indentation and marker' \
	"$(_tui_step_message "  [STEP] $want")" "$want"

check 'coloured marker loses its escapes too' \
	"$(_tui_step_message "  ${esc}[1m${esc}[36m[STEP]${esc}[0m $want")" "$want"

check 'a colour around the whole line is stripped as well' \
	"$(_tui_step_message "${esc}[36m  [STEP] $want${esc}[0m")" "$want"

# Not every animated line is a marker line; one without [STEP] keeps its text.
check 'an unmarked line survives intact' \
	"$(_tui_step_message "  ${esc}[2mResolving sources${esc}[0m")" 'Resolving sources'

printf '\n%s passed, %s failed\n' "$passed" "$failed"
[[ "$failed" -eq 0 ]]
