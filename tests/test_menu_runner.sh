#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_SIMPLE_* are read by the menu runner.
set -uo pipefail

# The seam between the Bash menus and the Python loop: the menu is described
# here, drawn and read there, and the choice comes back. What is worth pinning
# is the handover -- that the description built in Bash arrives whole, that a
# cancel stays cancelled rather than being redrawn by the other loop, and that
# a machine without python3 gets the same answer from the Bash one.

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$TEST_DIR/.." && pwd)"
export AGENTBOT_HOME="$REPO_DIR"
export PYTHONDONTWRITEBYTECODE=1

if ! command -v python3 >/dev/null 2>&1; then
	printf 'ok - menu runner skipped; python3 unavailable\n'
	exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
passed=0
failed=0

expect() {
	local label="$1" want="$2" got="$3"
	if [[ "$got" == "$want" ]]; then
		printf 'ok - %s\n' "$label"
		passed=$((passed + 1))
	else
		printf 'not ok - %s\n     want: %s\n     got:  %s\n' "$label" "$want" "$got"
		failed=$((failed + 1))
	fi
}

# run_menu <keys-as-printf-escapes> [extra env assignments...]
# Returns "<rc>|<result>" and leaves the drawn frames in $work/out.
run_menu() {
	local keys="$1"
	shift
	printf '%b' "$keys" >"$work/in"
	: >"$work/out"
	env "$@" NO_COLOR=1 DOTFILES_TTY_INPUT="$work/in" DOTFILES_TTY_OUTPUT="$work/out" \
		bash -c '
			source "$AGENTBOT_HOME/scripts/menu.sh"
			MENU_SIMPLE_TITLE="Agentbot"
			MENU_SIMPLE_BREADCRUMB="Agentbot"
			MENU_SIMPLE_TYPES=()
			MENU_SIMPLE_LABELS=("Check Status" "Install Agentbot" "Quit")
			MENU_SIMPLE_KEYS=(status install quit)
			MENU_SIMPLE_DESCS=(
				$'"'"'Check the installed Agentbot components and baseline.\nRead-only status; no updates or writes are performed.'"'"'
				$'"'"'Install.\nSecond line.'"'"'
				$'"'"'Leave.\nSecond line.'"'"'
			)
			agentbot_menu_run
			rc=$?
			printf "%s|%s\n" "$rc" "${MENU_SIMPLE_RESULT:-}"
		'
}

expect 'Enter takes the entry under the cursor' '0|status' "$(run_menu '\r')"
expect 'q cancels and reports no choice' '1|' "$(run_menu 'q')"

# The second line of a description is built in Bash and drawn in Python. It
# travels as an escaped newline, so losing it would show as a one-line footer
# rather than as an error.
run_menu '\r' >/dev/null
if grep -Fq 'Read-only status; no updates or writes are performed.' "$work/out"; then
	printf 'ok - a two-line description arrives whole\n'
	passed=$((passed + 1))
else
	printf 'not ok - a two-line description arrives whole\n'
	failed=$((failed + 1))
fi

# Without python3 the shared Bash loop answers instead, and must answer the
# same. This is the path a machine takes before it has a runtime -- and the one
# Dotfiles takes permanently.
stub="$work/nopy"
mkdir -p "$stub"
expect 'a machine without python3 gets the same answer' '0|status' \
	"$(PATH="$stub:/usr/bin:/bin" run_menu '\r' "PATH=$stub:/usr/bin:/bin")"

printf '\nRan %d menu-runner test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
