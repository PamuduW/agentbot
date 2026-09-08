#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2016,SC2034  # literal Codex syntax and MENU_SIMPLE globals are intentional

GRAPHIFY_ASSISTANT_ROWS=(
	'Build or refresh|/graphify .|Create or update the current project graph.'
	'Codex skill|$graphify .|Run the same workflow through the Codex skill form.'
	'Query|/graphify query "what connects auth to the database?"|Ask a graph-grounded architecture question.'
	'Path|/graphify path "UserService" "DatabasePool"|Find the shortest connection between two nodes.'
	'Explain|/graphify explain "RateLimiter"|Explain one node and its immediate relationships.'
	'Wiki|/graphify . --wiki|Generate the graph and its navigable wiki output.'
)

GRAPHIFY_SHELL_ROWS=(
	'Extract|graphify extract .|Run a headless full extraction.'
	'Update|graphify update .|Refresh a graph without semantic LLM extraction.'
	'Recluster|graphify cluster-only . --resolution 1.5|Recluster an existing graph and regenerate its report.'
	'Query|graphify query "what connects auth to the database?"|Query the local graph from the shell.'
	'Path|graphify path "UserService" "DatabasePool"|Find the shortest path from the shell.'
	'Explain|graphify explain "RateLimiter"|Explain a graph node from the shell.'
	'Export call flow|graphify export callflow-html|Export the Mermaid call-flow report.'
	'Hook status|graphify hook status|Inspect repository hook integration.'
	'Merge graphs|graphify merge-graphs a.json b.json --out merged.json|Merge two graph files without altering their sources.'
)

GRAPHIFY_PLATFORM_ROWS=(
	'Agent Skills|graphify install --platform agents|Copy the generic skill into the Agent Skills store.'
	'Claude|graphify install --platform claude|Copy the skill into Claude configuration.'
	'Codex|graphify install --platform codex|Copy the skill into Codex configuration.'
	'Cursor|graphify install --platform cursor|Copy the skill into Cursor configuration.'
)

agentbot_graphify_validate_rows() {
	local help_text="$1" row _label command _description top
	for row in "${GRAPHIFY_SHELL_ROWS[@]}" "${GRAPHIFY_PLATFORM_ROWS[@]}"; do
		IFS='|' read -r _label command _description <<<"$row"
		top="${command#graphify }"
		top="${top%% *}"
		grep -Eq "^[[:space:]]{2}${top}([[:space:]]|$)" <<<"$help_text" || return 1
	done
}

_agentbot_graphify_rows_for_section() {
	case "$1" in
	assistant) printf '%s\n' "${GRAPHIFY_ASSISTANT_ROWS[@]}" ;;
	shell) printf '%s\n' "${GRAPHIFY_SHELL_ROWS[@]}" ;;
	platform) printf '%s\n' "${GRAPHIFY_PLATFORM_ROWS[@]}" ;;
	*) return 2 ;;
	esac
}

_agentbot_graphify_section_menu() {
	MENU_SIMPLE_TITLE='Graphify Lib'
	MENU_SIMPLE_BREADCRUMB='Agentbot › Graphify Lib'
	MENU_SIMPLE_LABELS=(
		'Assistant commands'
		'Shell query and export commands'
		'Manual platform setup'
		'Agentbot lifecycle boundary'
	)
	MENU_SIMPLE_KEYS=(assistant shell platform boundary)
	MENU_SIMPLE_DESCS=(
		'Commands used inside supported coding-agent conversations.'
		'Read-only queries plus explicit local extraction and export commands.'
		'Manual platform copies; Agentbot does not run these platform-specific paths.'
		'What Agentbot Install and Update own, and what remains manual.'
	)
}

_agentbot_graphify_command_menu() {
	local section="$1" row label command description
	MENU_SIMPLE_TITLE='Graphify commands'
	MENU_SIMPLE_BREADCRUMB="Agentbot › Graphify Lib › ${section^}"
	MENU_SIMPLE_LABELS=()
	MENU_SIMPLE_KEYS=()
	MENU_SIMPLE_DESCS=()
	while IFS= read -r row; do
		IFS='|' read -r label command description <<<"$row"
		MENU_SIMPLE_LABELS+=("${label} — ${description}")
		MENU_SIMPLE_KEYS+=("$command")
		MENU_SIMPLE_DESCS+=("$command")
	done < <(_agentbot_graphify_rows_for_section "$section")
}

_agentbot_graphify_render_detail() {
	local command="$1" section="$2" row label candidate description cols
	cols="$(tui_cols)"
	description=''
	while IFS= read -r row; do
		IFS='|' read -r label candidate description <<<"$row"
		[[ "$candidate" == "$command" ]] && break
	done < <(_agentbot_graphify_rows_for_section "$section")
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
		_agentbot_graphify_section_menu
		agentbot_menu_run || return 0
		section="${MENU_SIMPLE_RESULT:-}"
		if [[ "$section" == boundary ]]; then
			tui_clear
			_agentbot_graphify_render_boundary
			tui_wait_back
			continue
		fi
		while true; do
			_agentbot_graphify_command_menu "$section"
			agentbot_menu_run || break
			command="${MENU_SIMPLE_RESULT:-}"
			tui_clear
			_agentbot_graphify_render_detail "$command" "$section"
			tui_wait_back
		done
	done
}
