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

check 'every public surface closes with a rollup' test_every_surface_closes_with_a_rollup
check 'section rules appear only where a surface has groups' test_section_rules_only_where_there_are_groups

printf '\nRan %d report-contract test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
