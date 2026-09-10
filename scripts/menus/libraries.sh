#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_SIMPLE_* globals are consumed by the menu runner.

agentbot_menu_libraries_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	command_lib) agentbot_menu_command_lib || rc=$? ;;
	graphify_lib) agentbot_menu_graphify_lib || rc=$? ;;
	*)
		printf 'Unknown Libraries action: %s\n' "$choice" >&2
		rc=2
		;;
	esac
	if ((rc != 0)); then
		printf '  %sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
	fi
	return "$rc"
}

agentbot_menu_libraries() {
	tui_menu_declare_owns_pause
	# No globals to put back: the parent menu is named, not built here.
	agentbot_submenu_loop libraries agentbot_menu_libraries_dispatch
}
