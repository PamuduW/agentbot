#!/usr/bin/env bash
# shellcheck shell=bash

_agentbot_token_menu_open_fds() {
	local in_path out_path
	if [[ -n "${AGENTBOT_TOKEN_TTY_INPUT:-}" ]]; then
		in_path="$AGENTBOT_TOKEN_TTY_INPUT"
		exec {AGENTBOT_TOKEN_MENU_IN_FD}<"$in_path"
	else
		tui_refresh_tty_seam
		if tty_use_input_fd; then
			exec {AGENTBOT_TOKEN_MENU_IN_FD}<&"$DOTFILES_TTY_IN_FD"
		else
			in_path="$(tty_input_path)"
			exec {AGENTBOT_TOKEN_MENU_IN_FD}<"$in_path"
		fi
	fi
	if [[ -n "${AGENTBOT_TOKEN_TTY_OUTPUT:-}" ]]; then
		out_path="$AGENTBOT_TOKEN_TTY_OUTPUT"
		exec {AGENTBOT_TOKEN_MENU_OUT_FD}>"$out_path"
	elif tty_use_output_fd; then
		exec {AGENTBOT_TOKEN_MENU_OUT_FD}>&"$DOTFILES_TTY_OUT_FD"
	else
		out_path="$(tty_output_path)"
		exec {AGENTBOT_TOKEN_MENU_OUT_FD}>"$out_path"
	fi
}

_agentbot_token_menu_close_fds() {
	exec {AGENTBOT_TOKEN_MENU_IN_FD}<&-
	exec {AGENTBOT_TOKEN_MENU_OUT_FD}>&-
}

_agentbot_token_menu_line() {
	local out_var="$1" prompt="$2" value=''
	printf '%s' "$prompt" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	IFS= read -r value <&"$AGENTBOT_TOKEN_MENU_IN_FD" || value='q'
	printf -v "$out_var" '%s' "$value"
}

# This menu reads a choice, a secret, and a confirmation from one stream, so it
# hands the shared prompts its open descriptors rather than paths: reopening a
# path would restart it and re-read the choice.
_agentbot_token_menu_confirm() {
	AGENTBOT_TUI_IN_FD="$AGENTBOT_TOKEN_MENU_IN_FD" \
		AGENTBOT_TUI_OUT_FD="$AGENTBOT_TOKEN_MENU_OUT_FD" \
		tui_confirm "${C_YELLOW:-}$1${C_RESET:-}"
}

_agentbot_token_menu_pause() {
	AGENTBOT_TUI_IN_FD="$AGENTBOT_TOKEN_MENU_IN_FD" \
		AGENTBOT_TUI_OUT_FD="$AGENTBOT_TOKEN_MENU_OUT_FD" \
		tui_pause
}

_agentbot_token_menu_secret() {
	local out_var="$1" prompt="$2" value=''
	# `read -rs` shows nothing at all, so a mistyped token gives no feedback that
	# anything was typed. read_tty_secret masks with * and supports backspace.
	#
	# It reads the DOTFILES_TTY_* seam, which tui.sh already feeds from
	# AGENTBOT_TUI_*. These are `local`, which in Bash is dynamic scope: the
	# helper sees them and the exported values come back untouched afterwards.
	local DOTFILES_TTY_IN_FD="$AGENTBOT_TOKEN_MENU_IN_FD"
	local DOTFILES_TTY_OUT_FD="$AGENTBOT_TOKEN_MENU_OUT_FD"
	read_tty_secret value "$prompt" || value='q'
	printf -v "$out_var" '%s' "$value"
}

_agentbot_token_menu_render() {
	local token='' current='not configured' current_color="${C_DIM:-}"
	local cols="${AGENTBOT_TOKEN_TTY_COLS:-}"
	[[ -n "$cols" ]] || cols="$(tui_cols)"
	github_token_read token
	if [[ -n "$token" ]]; then
		current="$(github_token_fingerprint "$token")"
		current_color="${C_GREEN:-}"
	elif [[ -e "$(github_token_file)" || -L "$(github_token_file)" ]]; then
		current='saved state is invalid or unsafe'
		current_color="${C_RED:-}"
	fi
	tui_header "GitHub Token Config" "Agentbot › GitHub Token Config" "$cols" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	printf '  %sCurrent:%s %s%s%s\n' \
		"${C_BOLD:-}" "${C_RESET:-}" "$current_color" "$current" "${C_RESET:-}" \
		>&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	printf '  %sSaved outside this repository:%s %s%s%s\n\n' \
		"${C_DIM:-}" "${C_RESET:-}" "${C_CYAN:-}" "$(github_token_file)" "${C_RESET:-}" \
		>&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	printf '  %sOptional:%s raises public-repository API rate limits.\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	printf '  %sNo repository scopes are needed for this workflow.%s\n\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	printf '  %s[s]%s Save or replace   %s[r]%s Reveal once   %s[d]%s Remove   %s[q]%s Back\n' \
		"${C_CYAN:-}" "${C_RESET:-}" "${C_CYAN:-}" "${C_RESET:-}" \
		"${C_CYAN:-}" "${C_RESET:-}" "${C_CYAN:-}" "${C_RESET:-}" \
		>&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	printf '\n' >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
}

_agentbot_token_menu_save() {
	local token=''
	printf '  %sInput is hidden; only its fingerprint will be shown.%s\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	_agentbot_token_menu_secret token "  ${C_CYAN:-}GitHub token${C_RESET:-} (q cancels): "
	[[ "$token" != q && "$token" != Q && -n "$token" ]] || return 0
	if ! github_token_is_valid "$token"; then
		printf '  %sInvalid token; nothing was saved.%s\n' \
			"${C_RED:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		return 0
	fi
	printf '  %sProposed:%s %s%s%s\n' \
		"${C_DIM:-}" "${C_RESET:-}" "${C_CYAN:-}" \
		"$(github_token_fingerprint "$token")" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	if _agentbot_token_menu_confirm "  Save this token?"; then
		if github_token_write "$token"; then
			printf '  %sGitHub token saved.%s\n' \
				"${C_GREEN:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		else
			printf '  %sGitHub token was not saved.%s\n' \
				"${C_RED:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		fi
	fi
}

_agentbot_token_menu_reveal() {
	local token=''
	github_token_read token
	if [[ -z "$token" ]]; then
		printf '  %sNo valid saved token is available to reveal.%s\n' \
			"${C_YELLOW:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		return 0
	fi
	printf '  %sWARNING: the full token will be printed once on this terminal.%s\n' \
		"${C_RED:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	if _agentbot_token_menu_confirm "  Reveal the full token once?"; then
		printf '  %s\n' "$token" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		_agentbot_token_menu_pause
	fi
}

_agentbot_token_menu_remove() {
	local file
	file="$(github_token_file)"
	if [[ ! -e "$file" && ! -L "$file" ]]; then
		printf '  %sNo saved token file exists.%s\n' \
			"${C_DIM:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		return 0
	fi
	if _agentbot_token_menu_confirm "  Remove the saved token?"; then
		if github_token_remove; then
			printf '  %sSaved token removed.%s\n' \
				"${C_GREEN:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		else
			printf '  %sSaved token could not be removed safely.%s\n' \
				"${C_RED:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		fi
	fi
}

agentbot_token_config_menu() {
	local action=''
	_agentbot_token_menu_open_fds || return 1
	_github_token_warning_scope_begin
	while true; do
		tui_clear
		_agentbot_token_menu_render
		_agentbot_token_menu_line action "  ${C_BOLD:-}Select action:${C_RESET:-} "
		case "$action" in
		s | S) _agentbot_token_menu_save ;;
		r | R) _agentbot_token_menu_reveal ;;
		d | D) _agentbot_token_menu_remove ;;
		q | Q) break ;;
		*) printf '  %sInvalid choice.%s\n' "${C_YELLOW:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD" ;;
		esac
	done
	_github_token_warning_scope_end
	_agentbot_token_menu_close_fds
}

agentbot_menu_token() {
	tui_menu_declare_owns_pause
	agentbot_token_config_menu
}
