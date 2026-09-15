#!/usr/bin/env bash
# Every menu helper a menu file calls must exist once the menu stack is loaded.
#
# Break caught: the GitLab token screen reuses the GitHub screen's descriptor
# and prompt helpers. Those helpers moved to dotfiles-shared and were renamed,
# and this file's callers were not. Nothing failed, because the one test that
# touches them stubs them by name -- so the suite defined the old names itself
# and never asked whether the real ones existed. The menu died on the operator's
# terminal with "command not found".
set -uo pipefail

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

test_every_menu_helper_a_menu_calls_is_defined() (
	# Load the stack the way scripts/menu.sh does, then ask Bash what exists.
	local missing=()
	local defined called name
	# shellcheck disable=SC1091
	defined="$(
		set +u
		AGENTBOT_TUI=1 source "$ROOT/scripts/menu.sh" >/dev/null 2>&1
		declare -F | awk '{print $3}'
	)"
	# Helper-shaped names the menu files call: the token screens' internals and
	# the shared TUI seams they are built on.
	called="$(grep -rhoE '\b_(github_token_menu|agentbot_token_menu)_[a-z_]+' \
		"$ROOT/scripts/menus" "$ROOT/scripts/menu.sh" 2>/dev/null | sort -u)"
	while read -r name; do
		[[ -n "$name" ]] || continue
		grep -qx -- "$name" <<<"$defined" || missing+=("$name")
	done <<<"$called"
	if ((${#missing[@]} > 0)); then
		printf '  undefined after loading the menu stack: %s\n' "${missing[*]}" >&2
		return 1
	fi
)

check 'every menu helper a menu calls is defined' test_every_menu_helper_a_menu_calls_is_defined

printf '\nRan %d menu-closure test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
