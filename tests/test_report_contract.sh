#!/usr/bin/env bash
# shellcheck shell=bash
set -uo pipefail

# The presentation contract for public report surfaces, decided during the L10
# review. Both rules already held on some surfaces by accident; this makes them
# rules, so a new surface cannot quietly opt out.
#
#   1. Every surface closes with a rollup, so the operator always knows whether
#      anything needs them without reading the table.
#   2. Section rules (-- Name --) appear only where a surface has two or more
#      groups. One group is its own section and a rule adds nothing.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
passed=0
failed=0

check() {
	local label="$1"
	shift
	if "$@"; then
		printf 'ok - %s\n' "$label"
		passed=$((passed + 1))
	else
		printf 'not ok - %s\n' "$label"
		failed=$((failed + 1))
	fi
}

surface_output() {
	AGENTBOT_TTY=0 NO_COLOR=1 "$ROOT/bin/agentbot" "$@" 2>&1
}

# 4.1's validation asks for both TTY and non-TTY. Every other test here runs
# headless, so the interactive path -- where width comes from the terminal and
# colour is on -- had no coverage at all.
surface_output_tty() {
	AGENTBOT_TTY=1 AGENTBOT_TUI=1 AGENTBOT_MENU_COLS=100 FORCE_COLOR=1 \
		"$ROOT/bin/agentbot" "$@" 2>&1
}

# Read-only surfaces only: this must never mutate the machine it runs on.
SURFACES=(
	'status'
	'doctor'
	'cli-config status'
	'cursor status'
	'vscode status'
	'boost status'
	'graphify'
)

test_every_surface_closes_with_a_rollup() {
	local surface output missing=''
	for surface in "${SURFACES[@]}"; do
		# shellcheck disable=SC2086  # Surfaces carry their subcommand.
		output="$(surface_output $surface)"
		grep -qE '^  ([0-9]+ ok|All [0-9]+ component)' <<<"$output" ||
			missing+=" [$surface]"
	done
	[[ -z "$missing" ]] || {
		printf '   surfaces with no rollup:%s\n' "$missing" >&2
		return 1
	}
}

test_section_rules_only_where_there_are_groups() {
	local surface output rules wrong=''
	for surface in "${SURFACES[@]}"; do
		# shellcheck disable=SC2086
		output="$(surface_output $surface)"
		rules="$(grep -c '──' <<<"$output" || true)"
		# A surface draws a rule per group, so one rule means one named group,
		# which is the case the contract forbids.
		((rules == 1)) && wrong+=" [$surface]"
	done
	[[ -z "$wrong" ]] || {
		printf '   surfaces with exactly one section rule:%s\n' "$wrong" >&2
		return 1
	}
}

test_the_contract_holds_under_a_tty_too() {
	local surface output missing='' uncoloured=''
	for surface in "${SURFACES[@]}"; do
		# shellcheck disable=SC2086
		output="$(surface_output_tty $surface)"
		grep -qE '[0-9]+ ok|All [0-9]+ component' <<<"$output" || missing+=" [$surface]"
		# Colour is the thing a TTY changes, so its absence here would mean the
		# interactive path is silently rendering the piped output.
		[[ "$output" == *$'\033'* ]] || uncoloured+=" [$surface]"
	done
	[[ -z "$missing" && -z "$uncoloured" ]] || {
		printf '   no rollup under a TTY:%s\n   no colour under a TTY:%s\n' \
			"${missing:- none}" "${uncoloured:- none}" >&2
		return 1
	}
}

test_tty_width_reaches_the_tables() {
	local output width
	# A wider terminal must produce wider tables: if it does not, the
	# interactive path is ignoring the width it was given.
	output="$(AGENTBOT_TTY=1 AGENTBOT_TUI=1 AGENTBOT_MENU_COLS=120 NO_COLOR=1 \
		"$ROOT/bin/agentbot" boost status 2>&1)"
	width="$(awk '/\|/ { print length($0); exit }' <<<"$output")"
	[[ "${width:-0}" -gt 100 ]] || {
		printf '   table width at 120 columns was %s\n' "${width:-none}" >&2
		return 1
	}
}

check 'the contract holds under a TTY as well' test_the_contract_holds_under_a_tty_too
check 'terminal width reaches the rendered tables' test_tty_width_reaches_the_tables
check 'every public surface closes with a rollup' test_every_surface_closes_with_a_rollup
check 'section rules appear only where a surface has groups' test_section_rules_only_where_there_are_groups

printf '\nRan %d report-contract test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
