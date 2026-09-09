#!/usr/bin/env bash
# shellcheck shell=bash
set -uo pipefail

# A menu is now written in two places on purpose: what it says is Python
# (src/ui/menus.py), what its choices do is Bash. That split is only safe if the
# two cannot drift, and this is what makes drift a test failure instead of an
# "Unknown menu action" in front of an operator.
#
# Both directions matter. A key with no branch is a menu entry that does
# nothing; a branch with no key is either dead code or a rename half-finished.

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$TEST_DIR/.." && pwd)"
export AGENTBOT_HOME="$REPO_DIR"
export PYTHONDONTWRITEBYTECODE=1

if ! command -v python3 >/dev/null 2>&1; then
	printf 'ok - menu definitions skipped; python3 unavailable\n'
	exit 0
fi

AGENTBOT_MENU_SOURCE_ONLY=1 source "$REPO_DIR/scripts/menu.sh"

passed=0
failed=0

fail() {
	printf 'not ok - %s\n' "$*"
	failed=$((failed + 1))
}

# The keys a dispatch function actually handles, read out of the function
# itself rather than from a list kept beside it.
dispatch_keys() {
	declare -f "$1" |
		sed -n 's/^[[:space:]]*\([a-z0-9_| -]*\))[[:space:]]*$/\1/p' |
		tr '|' '\n' |
		sed 's/^[[:space:]]*//; s/[[:space:]]*$//' |
		grep -v '^$' | sort -u
}

menu_keys() {
	(cd "$REPO_DIR" && python3 -c "
import sys
from src.ui.menus import menu
print('\n'.join(menu(sys.argv[1]).keys))
" "$1") | sort -u
}

# menu|dispatch function|keys the loop handles rather than the dispatch
while IFS='|' read -r name dispatch loop_keys; do
	[[ -n "$name" ]] || continue
	defined="$(menu_keys "$name")"
	handled="$(dispatch_keys "$dispatch")"
	if [[ -z "$defined" ]]; then
		fail "$name: no keys defined"
		continue
	fi

	missing=''
	for key in $defined; do
		[[ " $loop_keys " == *" $key "* ]] && continue
		grep -qx -- "$key" <<<"$handled" || missing+="$key "
	done
	[[ -z "$missing" ]] || fail "$name: keys with no dispatch branch: $missing"

	extra=''
	for key in $handled; do
		[[ "$key" == '*' ]] && continue
		grep -qx -- "$key" <<<"$defined" || extra+="$key "
	done
	[[ -z "$extra" ]] || fail "$name: dispatch branches with no key: $extra"

	if [[ -z "$missing" && -z "$extra" ]]; then
		printf 'ok - every %s key dispatches, and every branch has a key\n' "$name"
		passed=$((passed + 1))
	fi
done <<'MENUS'
main|agentbot_menu_dispatch|quit
libraries|agentbot_menu_libraries_dispatch|back quit
platform|agentbot_menu_platform_dispatch|back
workspaces|agentbot_menu_workspaces_dispatch|back
MENUS

# The definitions are also checked as they are built: a label without its key or
# its description shifts every entry below it onto the wrong action, so the
# dataclass refuses to exist rather than drawing a menu that lies.
if (cd "$REPO_DIR" && python3 -c "
from src.ui.menus import Menu
try:
    Menu(title='T', breadcrumb='B', labels=('a', 'b'), keys=('a',))
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"); then
	printf 'ok - a definition with mismatched labels and keys refuses to exist\n'
	passed=$((passed + 1))
else
	fail 'a definition with mismatched labels and keys refuses to exist'
fi

printf '\nRan %d menu-definition test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
