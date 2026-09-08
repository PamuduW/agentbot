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
		printf '%sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
	fi
	return "$rc"
}

_agentbot_menu_libraries_setup() {
	MENU_SIMPLE_TITLE='Libraries'
	MENU_SIMPLE_BREADCRUMB='Agentbot › Libraries'
	MENU_SIMPLE_LABELS=('Command Lib' 'Graphify Lib')
	MENU_SIMPLE_KEYS=(command_lib graphify_lib)
	MENU_SIMPLE_DESCS=(
		$'Show Agentbot commands and whether they read or mutate state.\nUse this as the local command reference.'
		$'Show Graphify assistant and shell commands plus safety boundaries.\nRead-only; Install and Update own generic skill synchronization.'
	)
}

agentbot_menu_libraries() {
	tui_menu_declare_owns_pause
	tui_submenu_loop _agentbot_menu_libraries_setup agentbot_menu_libraries_dispatch
	MENU_SIMPLE_TITLE='Agentbot'
	MENU_SIMPLE_BREADCRUMB='Agentbot'
}
