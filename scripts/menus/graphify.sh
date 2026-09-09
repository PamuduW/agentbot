#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2016,SC2034  # literal Codex syntax and MENU_SIMPLE globals are intentional

# The reference itself is src/ui/graphify_lib.py; the menus are derived from it
# and the detail page reads one row back. What stays here is the drawing, which
# uses the shared TUI, and the routing between the two.

agentbot_graphify_validate_rows() {
	local help_text="$1" top
	while IFS= read -r top; do
		[[ -n "$top" ]] || continue
		grep -Eq "^[[:space:]]{2}${top}([[:space:]]|$)" <<<"$help_text" || return 1
	done < <(cd "$AGENTBOT_HOME" && python3 -m src.ui.graphify_lib --commands)
}

_agentbot_graphify_render_detail() {
	local command="$1" section="$2" row label description cols
	cols="$(tui_cols)"
	row="$(cd "$AGENTBOT_HOME" && python3 -m src.ui.graphify_lib \
		--section "$section" --command "$command")" || return 0
	label="${row%%$'\x1f'*}"
	description="${row#*$'\x1f'}"
	tui_header "$label" "Agentbot › Graphify Lib › ${section^} › ${label}" "$cols"
	tui_section 'Command' "$cols"
	printf '  %s%s%s\n\n' "$C_CYAN" "$(tui_fit "$command" "$((cols - 2))")" "$C_RESET"
	tui_section 'What it does' "$cols"
	printf '  %s\n' "$(tui_fit "$description" "$((cols - 2))")"
}

_agentbot_graphify_render_boundary() {
	local cols
	cols="$(tui_cols)"
	tui_header 'Agentbot lifecycle boundary' 'Agentbot › Graphify Lib › Lifecycle boundary' "$cols"
	tui_section 'Agentbot owns' "$cols"
	printf '  %s%s%s\n' "$C_CYAN" 'graphify install --platform agents' "$C_RESET"
	printf '  %s\n' "$(tui_fit 'Install and Update run this only when the optional Graphify CLI is already installed.' "$((cols - 2))")"
	printf '\n'
	tui_section 'Manual only' "$cols"
	printf '  %s\n' "$(tui_fit 'Dotfiles owns CLI installation. Project graphs and platform-specific installers remain explicit.' "$((cols - 2))")"
}

agentbot_menu_graphify_lib() {
	local section command
	while true; do
		agentbot_menu_run graphify_lib || return 0
		section="${MENU_SIMPLE_RESULT:-}"
		if [[ "$section" == boundary ]]; then
			tui_clear
			_agentbot_graphify_render_boundary
			tui_wait_back
			continue
		fi
		while true; do
			agentbot_menu_run "graphify_${section}" || break
			command="${MENU_SIMPLE_RESULT:-}"
			tui_clear
			_agentbot_graphify_render_detail "$command" "$section"
			tui_wait_back
		done
	done
}
