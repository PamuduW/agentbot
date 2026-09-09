#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_CB_* globals are consumed by the checkbox runner.

# Roadmap 4.1: Agentbot had no component selection, so bootstrap triggered
# `agentbot install` outright and the operator got skills, Graphify and Boost as
# one all-or-nothing decision. This is the same checkbox the Dotfiles component
# selector uses, over the three parts of an install that are genuinely optional.
#
# Managed outputs and diagnostics are deliberately absent: they are the baseline
# every install leaves behind, and an install that skipped them would report on
# a state it had not established.

AGENTBOT_COMPONENT_KEYS=(skills graphify boost)
AGENTBOT_COMPONENT_LABELS=(
	'Skills'
	'Graphify integration'
	'Boost integration'
)
AGENTBOT_COMPONENT_DESCS=(
	'Install and update the skills named in skills.sources.yaml.'
	'Refresh the Graphify CLI and Agent Skills integration.'
	'Configure Boost for Claude, Codex, and Cursor.'
)

_agentbot_components_prepare() {
	local i
	declare -g -a MENU_CB_LABELS=() MENU_CB_STATUS=() MENU_CB_CHECKED=()
	for i in "${!AGENTBOT_COMPONENT_KEYS[@]}"; do
		MENU_CB_LABELS[i]="${AGENTBOT_COMPONENT_LABELS[$i]}"
		MENU_CB_STATUS[i]=''
		# Everything on by default: the previous behaviour was to install all
		# three, so an operator who just presses Enter gets what they had.
		MENU_CB_CHECKED[i]=1
	done
	MENU_CB_TITLE='Install Agentbot'
	MENU_CB_BREADCRUMB='Agentbot › Install Agentbot'
	MENU_CB_HINT='Up/Down navigate   Space toggle   a all   n none   Enter confirm   q back'
	MENU_CB_STATUS_MESSAGE=''
}

# The selection as the CLI's --components expects it.
agentbot_components_selection() {
	local i selected=''
	for i in "${!AGENTBOT_COMPONENT_KEYS[@]}"; do
		[[ "${MENU_CB_CHECKED[$i]:-0}" -eq 1 ]] || continue
		selected+="${selected:+,}${AGENTBOT_COMPONENT_KEYS[$i]}"
	done
	printf '%s\n' "$selected"
}

# The repository gate, before anything is selected.
#
# It used to run inside the install that follows the selector, so the operator
# chose components and was then asked to pull -- and pulling moves the checkout,
# restarts the run, and throws the selection away. Dotfiles asks first for the
# same reason (run_install_action). Through the same seam agentbot_menu_install
# uses, so the TUI output path and the exit contract are the ones this menu
# already understands.
_agentbot_menu_components_repo_gate() {
	local rc=0
	if [[ -n "${AGENTBOT_TUI:-}" ]]; then
		AGENTBOT_INSTALL_GATE_ONLY=1 tui_run_to_output agentbot_run_backend install || rc=$?
	else
		AGENTBOT_INSTALL_GATE_ONLY=1 agentbot_run_backend install || rc=$?
	fi
	return "$rc"
}

agentbot_menu_components() {
	local selection rc=0

	# 2 is "the checkout moved": the caller restarts into the new code, and the
	# selector must not open on the old one. 3 is a declined pull under the TUI,
	# which the gate has already explained -- back to the menu without a second
	# message. Anything else stopped, and says why.
	_agentbot_menu_components_repo_gate || rc=$?
	case "$rc" in
	0) ;;
	2) return 2 ;;
	3) return 0 ;;
	*) return "$rc" ;;
	esac

	_agentbot_components_prepare
	agentbot_checkbox_run || return 0

	selection="$(agentbot_components_selection)"
	if [[ -z "$selection" ]]; then
		printf '%sNothing selected; install cancelled.%s\n' "${C_DIM:-}" "${C_RESET:-}"
		return 0
	fi

	# Through agentbot_menu_install, not around it: that function owns the TUI
	# output seam and the exit-3 repository-change contract, and calling the
	# backend directly from here silently dropped both.
	agentbot_menu_install --components "$selection" || rc=$?
	return "$rc"
}
