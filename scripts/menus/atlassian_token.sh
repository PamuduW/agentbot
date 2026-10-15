#!/usr/bin/env bash
# shellcheck shell=bash

_agentbot_atlassian_token_backend() {
	(cd "$AGENTBOT_HOME" && "${AGENTBOT_PYTHON:-python3}" -m src.cli \
		--root "$AGENTBOT_HOME" atlassian-token "$@")
}

_agentbot_atlassian_token_render() {
	local current='' cols="${AGENTBOT_TOKEN_TTY_COLS:-}"
	[[ -n "$cols" ]] || cols="$(tui_cols)"
	current="$(_agentbot_atlassian_token_backend status 2>&1)" || true
	tui_header "Atlassian Tokens" "Agentbot › Token Config › Atlassian" "$cols" \
		>&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '%s\n' "$current" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  Saved outside this repository: %s/agentbot/atlassian.json\n\n' \
		"${XDG_CONFIG_HOME:-$HOME/.config}" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  Create separate scoped tokens with read permissions only.\n' >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  Scope selection must be confirmed when you create each token.\n\n' >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '  %s\n' "$(ui_format_shortcuts s 'Save or replace' c 'Check Cloud reads' d Remove q Back)" \
		>&"$GITHUB_TOKEN_MENU_OUT_FD"
	[[ -z "$_GITHUB_TOKEN_MENU_STATUS" ]] ||
		printf '\n  %s\n' "$_GITHUB_TOKEN_MENU_STATUS" >&"$GITHUB_TOKEN_MENU_OUT_FD"
	printf '\n' >&"$GITHUB_TOKEN_MENU_OUT_FD"
}

_agentbot_atlassian_token_save() {
	local email='' cloud_id='' jira_token='' confluence_token='' output='' rc=0
	_github_token_menu_line email '  Atlassian account email (q cancels): '
	[[ -n "$email" && "$email" != q && "$email" != Q ]] || return 0
	_github_token_menu_line cloud_id '  Site Cloud ID UUID (q cancels): '
	[[ -n "$cloud_id" && "$cloud_id" != q && "$cloud_id" != Q ]] || return 0
	_github_token_menu_secret jira_token '  Jira scoped read token (q cancels): '
	[[ -n "$jira_token" && "$jira_token" != q && "$jira_token" != Q ]] || return 0
	_github_token_menu_secret confluence_token '  Confluence scoped read token (q cancels): '
	[[ -n "$confluence_token" && "$confluence_token" != q && "$confluence_token" != Q ]] || return 0
	_github_token_menu_confirm 'Save these settings, with only read scopes selected for both tokens?' || return 0
	output="$(printf '%s\n' "$cloud_id" "$email" "$jira_token" "$confluence_token" |
		"${AGENTBOT_PYTHON:-python3}" -c '
import json, sys
keys = ("ATLASSIAN_MCP_CLOUD_ID", "ATLASSIAN_MCP_EMAIL", "JIRA_MCP_READ_TOKEN", "CONFLUENCE_MCP_READ_TOKEN")
print(json.dumps(dict(zip(keys, sys.stdin.read().splitlines()))))
' | _agentbot_atlassian_token_backend set 2>&1)" || rc=$?
	if ((rc == 0)); then
		_github_token_menu_say "${C_GREEN:-}${output}${C_RESET:-}"
	else
		_github_token_menu_say "${C_RED:-}${output}${C_RESET:-}"
	fi
}

agentbot_atlassian_token_menu() {
	local restore_trace=0
	if [[ "$-" == *x* ]]; then
		restore_trace=1
		set +x
	fi
	# shellcheck disable=SC2034 # Shared prompt helpers read these through dynamic scope.
	local GITHUB_TOKEN_TTY_INPUT="${AGENTBOT_TOKEN_TTY_INPUT:-}"
	# shellcheck disable=SC2034
	local GITHUB_TOKEN_TTY_OUTPUT="${AGENTBOT_TOKEN_TTY_OUTPUT:-}"
	local action='' output='' rc=0
	if ! _github_token_menu_open_fds; then
		((restore_trace == 0)) || set -x
		return 1
	fi
	_GITHUB_TOKEN_MENU_STATUS=''
	while true; do
		tui_clear
		_agentbot_atlassian_token_render
		_GITHUB_TOKEN_MENU_STATUS=''
		_github_token_menu_line action '  Select action: '
		case "$action" in
		s | S) _agentbot_atlassian_token_save ;;
		c | C)
			rc=0
			output="$(_agentbot_atlassian_token_backend check 2>&1)" || rc=$?
			if ((rc == 0)); then
				_github_token_menu_say "${C_GREEN:-}${output}${C_RESET:-}"
			else
				_github_token_menu_say "${C_RED:-}${output}${C_RESET:-}"
			fi
			;;
		d | D)
			if _github_token_menu_confirm 'Remove the saved Atlassian settings and both tokens?'; then
				output="$(_agentbot_atlassian_token_backend remove 2>&1)" || true
				_github_token_menu_say "$output"
			fi
			;;
		q | Q) break ;;
		*) _github_token_menu_say 'Invalid choice.' ;;
		esac
	done
	_github_token_menu_close_fds
	((restore_trace == 0)) || set -x
}
