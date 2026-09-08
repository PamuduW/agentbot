#!/usr/bin/env bash
# shellcheck shell=bash

_AGENTBOT_MENU_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/lib/tui.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/lib/github_token.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/status.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/install.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/update.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/prune_skills.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/github_token.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/workspaces.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/command_lib.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/graphify.sh"
# shellcheck source=scripts/menus/platform.sh
source "$_AGENTBOT_MENU_DIR/menus/platform.sh"
# shellcheck source=scripts/menus/components.sh
source "$_AGENTBOT_MENU_DIR/menus/components.sh"
# shellcheck disable=SC1091
source "$_AGENTBOT_MENU_DIR/menus/libraries.sh"

# shellcheck disable=SC2034
_agentbot_menu_setup() {
	MENU_SIMPLE_TITLE='Agentbot'
	MENU_SIMPLE_BREADCRUMB='Agentbot'
	MENU_SIMPLE_LABELS=(
		'Check Status'
		'Install Agentbot'
		'Update'
		'Prune Skills'
		'GitHub Token Config'
		'Workspaces'
		'Platform'
		'Libraries'
		'Quit'
	)
	MENU_SIMPLE_KEYS=(status install update prune-skills token workspaces platform libraries quit)
	MENU_SIMPLE_DESCS=(
		$'Check the installed Agentbot components and baseline.\nRead-only status; no updates or writes are performed.'
		$'Choose what to set up, then install: skills, Graphify, Boost.\nManaged outputs, Doctor and the launcher link always run.'
		$'Update the repository, reconcile skills, and refresh workspaces plus global outputs.\nA preview and explicit confirmation are required before mutation.'
		$'Select and permanently remove manual, orphaned, excluded, or stale skills.\nEach candidate shows its classification and source detail before confirmation.'
		$'Configure the optional shared GitHub API token.\nThe token is stored outside this repository.'
		$'List, preview, and resync locally registered workspaces.\nApply actions require explicit confirmation.'
		$'Manage VS Code extensions and settings, the Cursor statusline, and the agent CLI configs.\nPreviews are read-only; every apply confirms first.'
		$'Open the Agentbot and Graphify command reference libraries.\nRead-only command and safety information.'
		$'Exit the Agentbot menu.\nReturn to the calling process.'
	)
}

agentbot_menu_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	status) agentbot_menu_status || rc=$? ;;
	install) agentbot_menu_components || rc=$? ;;
	update) agentbot_menu_update || rc=$? ;;
	prune-skills) agentbot_menu_prune_skills || rc=$? ;;
	token) agentbot_menu_token || rc=$? ;;
	workspaces) agentbot_menu_workspaces || rc=$? ;;
	platform) agentbot_menu_platform || rc=$? ;;
	libraries) agentbot_menu_libraries || rc=$? ;;
	*)
		printf 'Unknown Agentbot menu action: %s\n' "$choice" >&2
		rc=2
		;;
	esac
	if ((rc != 0 && rc != 2)); then
		printf '%sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
	fi
	return "$rc"
}

agentbot_menu_loop() {
	local choice rc
	export AGENTBOT_TUI=1
	AGENTBOT_MENU_QUIT=false
	while true; do
		_agentbot_menu_setup
		if ! menu_simple_run; then
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		[[ "$choice" == quit ]] && return 0
		tui_clear
		rc=0
		# A nested menu that paused for its own actions sets MENU_OWNS_PAUSE, so
		# the parent does not add a stale second pause after it exits. A failed
		# child still pauses, so its error stays visible before the redraw.
		MENU_OWNS_PAUSE=false
		agentbot_menu_dispatch "$choice" || rc=$?
		((rc == 2)) && return 2
		[[ "${AGENTBOT_MENU_QUIT:-false}" == true ]] && return 0
		if ((rc != 0)) || [[ "${MENU_OWNS_PAUSE:-false}" != true ]]; then
			tui_pause
		fi
	done
}
