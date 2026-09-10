#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_SIMPLE_* globals are consumed by the menu runner

# The three platform features shipped as CLI commands with nothing that could
# reach them: not the menu, not install, not update. Read-only actions run
# directly; every mutating one confirms first, because apply installs editor
# extensions and writes into the Windows profile.

agentbot_menu_platform_confirm() {
	local prompt="$1"
	tui_confirm "${C_YELLOW}${prompt}${C_RESET}"
}

agentbot_menu_platform_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	vscode-status) agentbot_run_backend vscode status || rc=$? ;;
	vscode-seed) agentbot_run_backend vscode seed || rc=$? ;;
	vscode-apply)
		agentbot_menu_platform_confirm 'Install missing extensions and merge owned settings?' ||
			return 0
		agentbot_run_backend vscode apply || rc=$?
		;;
	cursor-status) agentbot_run_backend cursor status || rc=$? ;;
	cursor-install)
		agentbot_menu_platform_confirm 'Install the managed Cursor statusline?' || return 0
		agentbot_run_backend cursor statusline || rc=$?
		;;
	cli-config-status) agentbot_run_backend cli-config status || rc=$? ;;
	cli-config-apply)
		agentbot_menu_platform_confirm 'Merge the declared keys into each CLI config?' || return 0
		agentbot_run_backend cli-config apply || rc=$?
		;;
	*)
		printf 'Unknown Platform menu action: %s\n' "$choice" >&2
		rc=2
		;;
	esac
	return "$rc"
}

agentbot_menu_platform() {
	# This loop pauses after every action, so the parent must not add a second
	# one. Without the declaration `q` out of here cost two keystrokes: the
	# parent paused on a screen the operator had already left.
	tui_menu_declare_owns_pause
	local choice rc
	while true; do
		if ! agentbot_menu_run platform; then
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		# `q` leaves the menu through the runner returning non-zero above,
		# the same way every other submenu exits.
		[[ -z "$choice" ]] && return 0
		rc=0
		agentbot_menu_platform_dispatch "$choice" || rc=$?
		if ((rc != 0)); then
			printf '  %sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
		fi
		tui_pause
	done
}
