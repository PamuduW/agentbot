#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034

agentbot_menu_workspaces_confirm() {
	tui_confirm "${C_YELLOW}Apply Agentbot-managed workspace changes?${C_RESET}"
}

agentbot_menu_workspaces_remove_confirm() {
	local path="$1"
	tui_print '%sStop managing this workspace?%s\n' "$C_YELLOW" "$C_RESET"
	tui_print '%s%s%s\n' "$C_CYAN" "$path" "$C_RESET"
	tui_confirm 'No workspace files will be changed.'
}

agentbot_menu_workspaces_remove_recorded() {
	local paths_file='' choice='' rc=0
	local -a recorded_paths=() labels=() keys=() descriptions=()
	local index path

	while true; do
		paths_file="$(mktemp)" || return 1
		if agentbot_run_backend workspaces --paths0 >"$paths_file"; then
			mapfile -d '' -t recorded_paths <"$paths_file"
			rm -f -- "$paths_file"
			paths_file=''
		else
			rc=$?
			rm -f -- "$paths_file"
			return "$rc"
		fi

		if ((${#recorded_paths[@]} == 0)); then
			printf '%sNo recorded workspaces to remove.%s\n' "$C_DIM" "$C_RESET"
			tui_pause
			return 0
		fi

		labels=()
		keys=()
		descriptions=()
		for index in "${!recorded_paths[@]}"; do
			labels+=("${recorded_paths[$index]}")
			keys+=("$index")
			descriptions+=('Stop managing this recorded path. No workspace files will be changed.')
		done
		MENU_SIMPLE_TITLE='Remove recorded workspaces'
		MENU_SIMPLE_BREADCRUMB='Agentbot › Workspaces › Remove'
		MENU_SIMPLE_LABELS=("${labels[@]}")
		MENU_SIMPLE_KEYS=("${keys[@]}")
		MENU_SIMPLE_DESCS=("${descriptions[@]}")

		if ! agentbot_menu_run; then
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		[[ "$choice" =~ ^[0-9]+$ ]] || return 2
		path="${recorded_paths[$choice]:-}"
		[[ -n "$path" ]] || return 2
		tui_clear
		if agentbot_menu_workspaces_remove_confirm "$path"; then
			agentbot_run_backend workspaces --remove "$path" || return $?
		else
			printf '%sWorkspace removal cancelled.%s\n' "$C_DIM" "$C_RESET"
		fi
		tui_pause
	done
}

agentbot_menu_workspaces_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	list) agentbot_run_backend workspaces || rc=$? ;;
	preview) agentbot_run_backend resync --all || rc=$? ;;
	apply)
		if agentbot_menu_workspaces_confirm; then
			agentbot_run_backend resync --all --yes || rc=$?
		else
			printf '%sApply cancelled.%s\n' "$C_DIM" "$C_RESET"
		fi
		;;
	remove) agentbot_menu_workspaces_remove_recorded || rc=$? ;;
	*)
		printf 'Unknown Workspaces action: %s\n' "$choice" >&2
		rc=2
		;;
	esac
	if ((rc != 0)); then
		printf '%sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
	fi
	return "$rc"
}

agentbot_menu_workspaces() {
	tui_menu_declare_owns_pause
	local choice rc

	MENU_SIMPLE_TITLE='Workspaces'
	MENU_SIMPLE_BREADCRUMB='Agentbot › Workspaces'
	MENU_SIMPLE_LABELS=(
		'List recorded workspaces'
		'Preview resync (all)'
		'Apply resync (all)'
		'Remove recorded workspaces'
	)
	MENU_SIMPLE_KEYS=(list preview apply remove)
	MENU_SIMPLE_DESCS=(
		$'Read the private local workspace registry.\nNo repository or state changes are performed.'
		$'Preview managed changes for every enabled workspace plus global Codex/Claude outputs.\nNo files are written.'
		$'Apply managed workspace changes and refresh global AGENTS/CLAUDE/statusline outputs.\nConfirmation is required before mutation.'
		$'Select one recorded path and stop managing it.\nNo workspace files are changed or removed.'
	)

	while true; do
		if ! agentbot_menu_run; then
			MENU_SIMPLE_TITLE='Agentbot'
			MENU_SIMPLE_BREADCRUMB='Agentbot'
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		tui_clear
		rc=0
		agentbot_menu_workspaces_dispatch "$choice" || rc=$?
		if ((rc != 0)) || [[ "$choice" != remove ]]; then
			tui_pause
		fi
	done
}
