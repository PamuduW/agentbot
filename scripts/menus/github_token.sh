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

# What the last action did, shown by the next frame.
#
# Every outcome here was printed and then wiped: the loop clears the screen and
# re-renders before the operator can read "GitHub token saved." or "Invalid
# token; nothing was saved." Only the reveal survived, because it pauses.
# Carried into the next frame instead, which is what the checkbox menu does
# with MENU_CB_STATUS_MESSAGE and costs no extra keystroke. Same fix, same
# shape, as the Dotfiles token menu.
_AGENTBOT_TOKEN_MENU_STATUS=''

_agentbot_token_menu_say() {
	_AGENTBOT_TOKEN_MENU_STATUS="$1"
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
	printf '  %s\n' \
		"$(ui_format_shortcuts s 'Save or replace' r 'Reveal once' d Remove q Back)${C_RESET:-}" \
		>&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	if [[ -n "$_AGENTBOT_TOKEN_MENU_STATUS" ]]; then
		printf '\n  %s\n' "$_AGENTBOT_TOKEN_MENU_STATUS" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	fi
	printf '\n' >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
}

_agentbot_token_menu_save() {
	local token=''
	printf '  %sInput is hidden; only its fingerprint will be shown.%s\n' \
		"${C_DIM:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	_agentbot_token_menu_secret token "  ${C_CYAN:-}GitHub token${C_RESET:-} (q cancels): "
	[[ "$token" != q && "$token" != Q && -n "$token" ]] || return 0
	if ! github_token_is_valid "$token"; then
		_agentbot_token_menu_say "${C_RED:-}Invalid token; nothing was saved.${C_RESET:-}"
		return 0
	fi
	printf '\n  %sProposed:%s %s%s%s\n\n' \
		"${C_DIM:-}" "${C_RESET:-}" "${C_CYAN:-}" \
		"$(github_token_fingerprint "$token")" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	if _agentbot_token_menu_confirm "  Save this token?"; then
		if github_token_write "$token"; then
			_agentbot_token_menu_say "${C_GREEN:-}GitHub token saved.${C_RESET:-}"
		else
			_agentbot_token_menu_say "${C_RED:-}GitHub token was not saved.${C_RESET:-}"
		fi
	fi
}

_agentbot_token_menu_reveal() {
	local token=''
	github_token_read token
	if [[ -z "$token" ]]; then
		_agentbot_token_menu_say "${C_YELLOW:-}No valid saved token is available to reveal.${C_RESET:-}"
		return 0
	fi
	printf '  %sWARNING: the full token will be printed once on this terminal.%s\n\n' \
		"${C_RED:-}" "${C_RESET:-}" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
	if _agentbot_token_menu_confirm "  Reveal the full token once?"; then
		# The secret gets space around it: it is the one line on this screen
		# the operator has to read off the terminal and type somewhere else.
		printf '\n  %s\n' "$token" >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		_agentbot_token_menu_pause
	fi
}

_agentbot_token_menu_remove() {
	local file
	file="$(github_token_file)"
	if [[ ! -e "$file" && ! -L "$file" ]]; then
		_agentbot_token_menu_say "${C_DIM:-}No saved token file exists.${C_RESET:-}"
		return 0
	fi
	if _agentbot_token_menu_confirm "  Remove the saved token?"; then
		if github_token_remove; then
			_agentbot_token_menu_say "${C_GREEN:-}Saved token removed.${C_RESET:-}"
		else
			_agentbot_token_menu_say "${C_RED:-}Saved token could not be removed safely.${C_RESET:-}"
		fi
	fi
}

agentbot_token_config_menu() {
	local action=''
	_agentbot_token_menu_open_fds || return 1
	_github_token_warning_scope_begin
	_AGENTBOT_TOKEN_MENU_STATUS=''
	while true; do
		tui_clear
		_agentbot_token_menu_render
		# Shown once: it describes what just happened, not what is true.
		_AGENTBOT_TOKEN_MENU_STATUS=''
		_agentbot_token_menu_line action "  ${C_BOLD:-}Select action:${C_RESET:-} "
		# One blank below the answer, so an action's output starts on its own
		# rather than running straight on from the line it was asked on.
		printf '\n' >&"$AGENTBOT_TOKEN_MENU_OUT_FD"
		case "$action" in
		s | S) _agentbot_token_menu_save ;;
		r | R) _agentbot_token_menu_reveal ;;
		d | D) _agentbot_token_menu_remove ;;
		q | Q) break ;;
		*) _agentbot_token_menu_say "${C_YELLOW:-}Invalid choice.${C_RESET:-}" ;;
		esac
	done
	_github_token_warning_scope_end
	_agentbot_token_menu_close_fds
}

agentbot_menu_token() {
	tui_menu_declare_owns_pause
	agentbot_token_config_menu
}
