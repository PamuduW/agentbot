#!/usr/bin/env bash
# shellcheck shell=bash

# Setup review: choose the agent, confirm it, choose the scope, then run
# `agentbot setup-review`. The confirmation is asked here, so the backend runs
# with --yes; everything after that -- the checkout sync, the read-only run,
# filing the report -- is the command's own.

agentbot_menu_setup_review() {
	local agent scope label
	agentbot_menu_run review_agent || return 0
	agent="${MENU_SIMPLE_RESULT:-}"
	case "$agent" in
	claude) label='Claude Code' ;;
	codex) label='Codex' ;;
	cursor) label='Cursor CLI' ;;
	agy) label='the Antigravity CLI' ;;
	*) return 0 ;;
	esac
	tui_confirm "Review the setup with ${label}?" || return 0
	agentbot_menu_run review_scope || return 0
	scope="${MENU_SIMPLE_RESULT:-}"
	[[ -n "$scope" ]] || return 0
	tui_clear
	agentbot_run_backend setup-review --cli "$agent" --scope "$scope" --yes
}
