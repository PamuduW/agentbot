#!/usr/bin/env bash
# shellcheck shell=bash

# The Command Lib lists what this build actually has, so the menu is derived
# from the command metadata rather than written down: src/ui/menus.py builds it
# from src/commands.py. It used to be assembled here from a tab-separated dump
# of that same metadata, which meant the data left Python, became Bash arrays,
# and went back to Python to be drawn.

_agentbot_command_lib_detail() {
	local command="$1"
	(cd "$AGENTBOT_HOME" && AGENTBOT_MENU_COLS="$(tui_cols)" AGENTBOT_TUI=1 \
		python3 -m src.cli --root "$AGENTBOT_HOME" help "$command" --format tui)
}

_agentbot_command_lib_browse() {
	local menu_name="$1" choice
	while true; do
		agentbot_menu_run "$menu_name" || return 1
		choice="${MENU_SIMPLE_RESULT:-}"
		# A key with no command behind it: the bootstrap list is a menu, not a
		# help topic, so it is the caller's to open.
		if [[ "$choice" == __bootstrap__ ]]; then
			_agentbot_command_lib_browse command_lib_bootstrap
			continue
		fi
		tui_clear
		_agentbot_command_lib_detail "$choice"
		tui_wait_back
	done
}

agentbot_menu_command_lib() {
	_agentbot_command_lib_browse command_lib
	return 0
}
