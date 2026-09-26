#!/usr/bin/env bash
# shellcheck shell=bash

# Memory: choose this machine's vault, check it, or validate it. Every setup
# flow runs the backend's preview first and applies only after confirmation;
# the backend owns validation, so these screens only collect paths and URLs.

agentbot_menu_memory_confirm() {
	tui_confirm "${C_YELLOW}Apply this memory setup?${C_RESET}"
}

agentbot_menu_memory_ask() {
	local __name="$1" prompt="$2" __value=''
	read_tty_line __value "  ${prompt}" || return 1
	__value="${__value#"${__value%%[![:space:]]*}"}"
	__value="${__value%"${__value##*[![:space:]]}"}"
	printf -v "$__name" '%s' "$__value"
}

agentbot_menu_memory_setup() {
	local mode="$1" first='' dest='' remote='' rc=0
	local -a args=()
	case "$mode" in
	path)
		agentbot_menu_memory_ask first 'Vault checkout path: ' || return 0
		args=(--path "$first")
		;;
	clone)
		agentbot_menu_memory_ask first 'Vault Git URL: ' || return 0
		[[ -n "$first" ]] && agentbot_menu_memory_ask dest 'Destination folder: ' || return 0
		args=(--clone "$first" --dest "$dest")
		;;
	new)
		agentbot_menu_memory_ask first 'New vault folder: ' || return 0
		[[ -n "$first" ]] && agentbot_menu_memory_ask remote 'Remote URL (optional, Enter to skip): ' || return 0
		args=(--new "$first")
		[[ -z "$remote" ]] || args+=(--remote "$remote")
		;;
	remove) args=(--remove) ;;
	*) return 2 ;;
	esac
	if [[ "$mode" != remove && -z "$first" ]]; then
		printf '  %sSetup cancelled.%s\n' "$C_DIM" "$C_RESET"
		return 0
	fi
	agentbot_run_backend memory setup "${args[@]}" || return $?
	if agentbot_menu_memory_confirm; then
		agentbot_run_backend memory setup "${args[@]}" --yes || rc=$?
	else
		printf '  %sSetup cancelled.%s\n' "$C_DIM" "$C_RESET"
	fi
	return "$rc"
}

agentbot_menu_memory_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	status) agentbot_run_backend memory status || rc=$? ;;
	validate) agentbot_run_backend memory validate || rc=$? ;;
	setup-path) agentbot_menu_memory_setup path || rc=$? ;;
	setup-clone) agentbot_menu_memory_setup clone || rc=$? ;;
	setup-new) agentbot_menu_memory_setup new || rc=$? ;;
	remove) agentbot_menu_memory_setup remove || rc=$? ;;
	*)
		printf 'Unknown Memory action: %s\n' "$choice" >&2
		rc=2
		;;
	esac
	# Exit 2 from status or validate means "no vault configured", which the
	# screen already says; it is not a failed action.
	if [[ "$choice" == status || "$choice" == validate ]] && ((rc == 2)); then
		rc=0
	fi
	if ((rc != 0)); then
		printf '  %sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
	fi
	return "$rc"
}

agentbot_menu_memory() {
	tui_menu_declare_owns_pause
	local choice
	while true; do
		if ! agentbot_menu_run memory; then
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		tui_clear
		agentbot_menu_memory_dispatch "$choice" || true
		tui_pause
	done
}
