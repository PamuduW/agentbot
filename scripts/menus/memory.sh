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

# Every open proposal, one at a time: its text, then approve, reject, skip,
# or stop. No path to type. Approve and reject still ask for the proposal's
# short code, the guard that keeps them a person's action.
agentbot_menu_memory_review() {
	local json='' path='' action='' rc=0 index=0
	local -a paths=()
	if ! json="$(agentbot_run_backend memory review --json)"; then
		# No vault, or a broken one: the ordinary screen says which.
		agentbot_run_backend memory review
		return 0
	fi
	mapfile -t paths < <("${AGENTBOT_PYTHON:-python3}" -c '
import json, sys
for item in json.loads(sys.argv[1]).get("proposals", []):
    print(item["path"])
' "$json")
	if ((${#paths[@]} == 0)); then
		printf '  No proposals are waiting for your review.\n'
		return 0
	fi
	for path in "${paths[@]}"; do
		index=$((index + 1))
		printf '\n  %sProposal %d of %d%s\n' "$C_BOLD" "$index" "${#paths[@]}" "$C_RESET"
		agentbot_run_backend memory review "$path" || rc=$?
		agentbot_menu_memory_ask action 'a approve, r reject, s skip, q stop: ' || return "$rc"
		case "$action" in
		a | A) agentbot_run_backend memory approve "$path" --yes || rc=$? ;;
		r | R) agentbot_run_backend memory reject "$path" --yes || rc=$? ;;
		q | Q)
			printf '  %sStopped; the rest are left for later.%s\n' "$C_DIM" "$C_RESET"
			return "$rc"
			;;
		*) printf '  %sLeft for later.%s\n' "$C_DIM" "$C_RESET" ;;
		esac
	done
	return "$rc"
}

agentbot_menu_memory_conflicts() {
	local op='' keep='' rc=0
	agentbot_run_backend memory conflict list || return $?
	agentbot_menu_memory_ask op 'Conflict to open (Enter to go back): ' || return 0
	[[ -n "$op" ]] || return 0
	agentbot_run_backend memory conflict show "$op" || return $?
	agentbot_menu_memory_ask keep 'm keep mine, t keep theirs, Enter to leave it: ' || return 0
	case "$keep" in
	m | M) agentbot_run_backend memory conflict resolve "$op" --keep mine || rc=$? ;;
	t | T) agentbot_run_backend memory conflict resolve "$op" --keep theirs || rc=$? ;;
	*) printf '  %sLeft for later.%s\n' "$C_DIM" "$C_RESET" ;;
	esac
	return "$rc"
}

agentbot_menu_memory_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	status) agentbot_run_backend memory status || rc=$? ;;
	validate) agentbot_run_backend memory validate || rc=$? ;;
	review) agentbot_menu_memory_review || rc=$? ;;
	conflicts) agentbot_menu_memory_conflicts || rc=$? ;;
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
