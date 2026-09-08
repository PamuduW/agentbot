#!/usr/bin/env bash
# shellcheck shell=bash

# Takes the same arguments `agentbot install` does, so the component selector
# narrows an install without going around the two things this function owns:
# the TUI output seam, and the repository-change exit contract.
agentbot_menu_install() {
	local rc=0
	if [[ -n "${AGENTBOT_TUI:-}" ]]; then
		tui_run_to_output agentbot_run_backend install "$@" || rc=$?
	else
		agentbot_run_backend install "$@" || rc=$?
	fi
	if ((rc == 3)); then
		return 0
	fi
	return "$rc"
}
