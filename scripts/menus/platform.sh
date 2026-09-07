#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_SIMPLE_* globals are consumed by menu_simple_run

# The three platform features shipped as CLI commands with nothing that could
# reach them: not the menu, not install, not update. Read-only actions run
# directly; every mutating one confirms first, because apply installs editor
# extensions and writes into the Windows profile.

_agentbot_platform_menu() {
	MENU_SIMPLE_TITLE='Platform'
	MENU_SIMPLE_BREADCRUMB='Agentbot › Platform'
	MENU_SIMPLE_LABELS=(
		'VS Code status'
		'VS Code seed'
		'VS Code apply'
		'Cursor statusline status'
		'Cursor statusline install'
		'CLI config status'
		'CLI config apply'
		'Back'
	)
	MENU_SIMPLE_KEYS=(
		vscode-status vscode-seed vscode-apply
		cursor-status cursor-install
		cli-config-status cli-config-apply
		back
	)
	MENU_SIMPLE_DESCS=(
		$'Preview the selected extensions and owned settings for each host.\nRead-only; writes nothing.'
		$'Record the currently installed extensions into vscode.yaml.\nWrites the manifest in this repository, not your editor.'
		$'Install missing extensions and merge owned settings into each host.\nBacks each settings file up first. Requires confirmation.'
		$'Report whether the managed Cursor statusline is installed and current.\nRead-only; writes nothing.'
		$'Install the managed statusline and point the Cursor CLI at it.\nWrites ~/.cursor and the statusLine block. Requires confirmation.'
		$'Preview the declared Claude, Codex, and Cursor CLI configuration keys.\nRead-only; writes nothing.'
		$'Merge the declared keys into each CLI config, rolling back on failure.\nBacks each config up first. Requires confirmation.'
		$'Return to the Agentbot menu.\nNo action is taken.'
	)
}

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
	local choice rc
	while true; do
		_agentbot_platform_menu
		if ! menu_simple_run; then
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		[[ "$choice" == back || -z "$choice" ]] && return 0
		rc=0
		agentbot_menu_platform_dispatch "$choice" || rc=$?
		if ((rc != 0)); then
			printf '%sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
		fi
		tui_pause
	done
}
