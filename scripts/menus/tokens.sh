#!/usr/bin/env bash
# shellcheck shell=bash

# Token Config: the provider list, and the GitLab screen behind it.
#
# This used to be one main-menu entry wired straight to the GitHub screen. Two
# credentials exist now -- GitHub's, shared verbatim with the sibling product,
# and the GitLab read_api token the Agentbot facade uses -- so the entry became
# a list and each provider keeps its own screen.
#
# The GitHub screen lives in the shared github_token_menu.sh, bound by
# menus/github_token.sh. This file reuses its descriptor and prompt helpers
# rather than opening a second seam: those
# read a choice, a secret and a confirmation from one stream, and a second
# reader on the same terminal would steal each other's input.
#
# Nothing here ever puts a token in an argument vector. The secret reaches the
# backend on stdin, because an argv is world-readable in /proc for as long as
# the process lives.

_agentbot_gitlab_token_backend() {
	(cd "$AGENTBOT_HOME" && "${AGENTBOT_PYTHON:-python3}" -m src.cli \
		--root "$AGENTBOT_HOME" gitlab-token "$@")
}

_agentbot_gitlab_token_render() {
	local current status_line rc=0 cols="${AGENTBOT_TOKEN_TTY_COLS:-}"
	local current_color="${C_DIM:-}"
	[[ -n "$cols" ]] || cols="$(tui_cols)"
	status_line="$(_agentbot_gitlab_token_backend status 2>&1)" || rc=$?
	current="${status_line#"${status_line%%[![:space:]]*}"}"
	if ((rc == 0)); then
		current_color="${C_GREEN:-}"
	elif [[ "$current" == "not configured" ]]; then
		current_color="${C_DIM:-}"
	else
		current_color="${C_RED:-}"
	fi
	tui_header "GitLab Token" "Agentbot › Token Config › GitLab" "$cols" \
		>&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  %sCurrent:%s %s%s%s\n' \
		"${C_BOLD:-}" "${C_RESET:-}" "$current_color" "$current" "${C_RESET:-}" \
		>&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  %sSaved outside this repository:%s %s%s%s\n\n' \
		"${C_DIM:-}" "${C_RESET:-}" "${C_CYAN:-}" \
		"${XDG_CONFIG_HOME:-$HOME/.config}/agentbot/gitlab.env" "${C_RESET:-}" \
		>&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  %sread_api only.%s The facade issues typed GET requests and nothing else.\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  %sWhat it may read is decided by the token; no project is stored here.%s\n\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  %s\n' \
		"$(ui_format_shortcuts s 'Save or replace' r 'Reveal once' \
			c 'Check with GitLab' d Remove q Back)${C_RESET:-}" \
		>&"$GITHUB_TOKEN_MENU_OUT_FD"
	if [[ -n "$_GITHUB_TOKEN_MENU_STATUS" ]]; then
		printf '\n  %s\n' "$_GITHUB_TOKEN_MENU_STATUS" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	fi
	printf '\n' >&"$GITHUB_TOKEN_MENU_OUT_FD"
}

# Ask GitLab before asking the operator, which is the order the GitHub screen
# uses. This screen used to confirm first and report the scopes afterwards, so a
# token that could write was already on disk -- and already exported to every
# client -- by the time it said so.
_agentbot_gitlab_token_save() {
	local token='' output='' rc=0
	printf '  %sInput is hidden; only its fingerprint will be shown.%s\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	_github_token_menu_secret token "  ${C_CYAN:-}GitLab read_api token${C_RESET:-} (q cancels): "
	[[ "$token" != q && "$token" != Q && -n "$token" ]] || return 0
	# Through stdin, never an argument.
	output="$(printf '%s\n' "$token" | _agentbot_gitlab_token_backend verify 2>&1)" || rc=$?
	output="${output#"${output%%[![:space:]]*}"}"
	case "$rc" in
	0)
		printf '  %s%s%s\n\n' "${C_GREEN:-}" "$output" "${C_RESET:-}" \
			>&"$GITHUB_TOKEN_MENU_OUT_FD"
		;;
	2)
		printf '  %s%s; saving without a check.%s\n\n' \
			"${C_YELLOW:-}" "$output" "${C_RESET:-}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
		;;
	*)
		_github_token_menu_say "${C_RED:-}${output:-Token was not saved.} Nothing was saved.${C_RESET:-}"
		return 0
		;;
	esac
	if ! _github_token_menu_confirm "  Save this token?"; then
		return 0
	fi
	rc=0
	output="$(printf '%s\n' "$token" | _agentbot_gitlab_token_backend set 2>&1)" || rc=$?
	output="${output#"${output%%[![:space:]]*}"}"
	if ((rc == 0)); then
		_github_token_menu_say "${C_GREEN:-}GitLab token ${output,}${C_RESET:-}"
	else
		_github_token_menu_say "${C_RED:-}${output:-Token was not saved.}${C_RESET:-}"
	fi
}

_agentbot_gitlab_token_check() {
	local output='' rc=0
	printf '  %sChecking the saved token with GitLab...%s\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	output="$(_agentbot_gitlab_token_backend check 2>&1)" || rc=$?
	output="${output#"${output%%[![:space:]]*}"}"
	case "$rc" in
	0) _github_token_menu_say "${C_GREEN:-}${output}${C_RESET:-}" ;;
	2) _github_token_menu_say "${C_YELLOW:-}${output}${C_RESET:-}" ;;
	*) _github_token_menu_say "${C_RED:-}${output}${C_RESET:-}" ;;
	esac
}

_agentbot_gitlab_token_reveal() {
	local token='' rc=0
	token="$(_agentbot_gitlab_token_backend reveal 2>/dev/null)" || rc=$?
	if ((rc != 0)) || [[ -z "$token" ]]; then
		_github_token_menu_say "${C_YELLOW:-}No saved token is available to reveal.${C_RESET:-}"
		return 0
	fi
	printf '  %sWARNING: the full token will be printed once on this terminal.%s\n\n' \
		"${C_RED:-}" "${C_RESET:-}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	if _github_token_menu_confirm "  Reveal the full token once?"; then
		printf '\n  %s\n' "$token" >&"$GITHUB_TOKEN_MENU_OUT_FD"
		_github_token_menu_pause
	fi
}

_agentbot_gitlab_token_remove() {
	local output='' rc=0
	_github_token_menu_confirm "  Remove the saved token?" || return 0
	output="$(_agentbot_gitlab_token_backend remove 2>&1)" || rc=$?
	output="${output#"${output%%[![:space:]]*}"}"
	if ((rc == 0)); then
		_github_token_menu_say "${C_GREEN:-}${output}${C_RESET:-}"
	else
		_github_token_menu_say "${C_RED:-}${output}${C_RESET:-}"
	fi
}

agentbot_gitlab_token_menu() {
	local action=''
	_github_token_menu_open_fds || return 1
	_GITHUB_TOKEN_MENU_STATUS=''
	while true; do
		tui_clear
		_agentbot_gitlab_token_render
		_GITHUB_TOKEN_MENU_STATUS=''
		_github_token_menu_line action "  ${C_BOLD:-}Select action:${C_RESET:-} "
		printf '\n' >&"$GITHUB_TOKEN_MENU_OUT_FD"
		case "$action" in
		s | S) _agentbot_gitlab_token_save ;;
		r | R) _agentbot_gitlab_token_reveal ;;
		c | C) _agentbot_gitlab_token_check ;;
		d | D) _agentbot_gitlab_token_remove ;;
		q | Q) break ;;
		*) _github_token_menu_say "${C_YELLOW:-}Invalid choice.${C_RESET:-}" ;;
		esac
	done
	_github_token_menu_close_fds
}

agentbot_menu_tokens_dispatch() {
	local choice="$1" rc=0
	case "$choice" in
	github) agentbot_token_config_menu || rc=$? ;;
	gitlab) agentbot_gitlab_token_menu || rc=$? ;;
	*)
		printf 'Unknown Token Config action: %s\n' "$choice" >&2
		rc=2
		;;
	esac
	if ((rc != 0)); then
		printf '  %sAction failed (exit %d).%s\n' "$C_RED" "$rc" "$C_RESET" >&2
	fi
	return "$rc"
}

agentbot_menu_tokens() {
	tui_menu_declare_owns_pause
	agentbot_submenu_loop tokens agentbot_menu_tokens_dispatch
}
