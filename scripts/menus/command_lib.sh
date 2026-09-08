#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_SIMPLE globals are consumed by the menu runner

_agentbot_command_rows() {
	(cd "$AGENTBOT_HOME" && python3 -m src.cli --root "$AGENTBOT_HOME" help --format menu)
}

_agentbot_command_lib_menu_for_surface() {
	local surface="$1" name behavior row_surface summary
	MENU_SIMPLE_LABELS=()
	MENU_SIMPLE_KEYS=()
	MENU_SIMPLE_DESCS=()
	while IFS=$'\t' read -r name behavior row_surface summary; do
		[[ "$row_surface" == "$surface" ]] || continue
		MENU_SIMPLE_LABELS+=("${name} [${behavior}] — ${summary}")
		MENU_SIMPLE_KEYS+=("$name")
		MENU_SIMPLE_DESCS+=("agentbot help ${name}")
	done < <(_agentbot_command_rows)
}

_agentbot_command_lib_public_menu() {
	MENU_SIMPLE_TITLE='Command Lib'
	MENU_SIMPLE_BREADCRUMB='Agentbot › Command Lib'
	_agentbot_command_lib_menu_for_surface public
	MENU_SIMPLE_LABELS+=('Bootstrap commands')
	MENU_SIMPLE_KEYS+=('__bootstrap__')
	MENU_SIMPLE_DESCS+=('Commands exposed by install.sh for setup and repair.')
}

_agentbot_command_lib_backend_menu() {
	MENU_SIMPLE_TITLE='Bootstrap commands'
	MENU_SIMPLE_BREADCRUMB='Agentbot › Command Lib › Bootstrap commands'
	_agentbot_command_lib_menu_for_surface bootstrap
}

_agentbot_command_lib_detail() {
	local command="$1"
	(cd "$AGENTBOT_HOME" && AGENTBOT_MENU_COLS="$(tui_cols)" AGENTBOT_TUI=1 \
		python3 -m src.cli --root "$AGENTBOT_HOME" help "$command" --format tui)
}

agentbot_menu_command_lib() {
	local choice
	while true; do
		_agentbot_command_lib_public_menu
		agentbot_menu_run || return 0
		choice="${MENU_SIMPLE_RESULT:-}"
		if [[ "$choice" == __bootstrap__ ]]; then
			while true; do
				_agentbot_command_lib_backend_menu
				agentbot_menu_run || break
				choice="${MENU_SIMPLE_RESULT:-}"
				tui_clear
				_agentbot_command_lib_detail "$choice"
				tui_wait_back
			done
			continue
		fi
		tui_clear
		_agentbot_command_lib_detail "$choice"
		tui_wait_back
	done
}
