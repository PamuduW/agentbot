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

# The keys must match Lifecycle.SELECTABLE_COMPONENTS; tests/test_menus.py
# pins the two lists against each other.
AGENTBOT_COMPONENT_KEYS=(skills graphify boost vscode cursor cli-config)
AGENTBOT_COMPONENT_LABELS=(
	'Skills'
	'Graphify integration'
	'Boost integration'
	'VS Code'
	'Cursor statusline'
	'CLI config'
)
AGENTBOT_COMPONENT_DESCS=(
	'Install and update the skills named in skills.sources.yaml.'
	'Refresh the Graphify CLI and Agent Skills integration.'
	'Configure Boost for Claude, Codex, and Cursor.'
	'Install the selected extensions and merge owned settings for each host.'
	'Install the managed Cursor CLI statusline.'
	'Merge the declared Claude, Codex, and Cursor configuration keys.'
)

_agentbot_components_prepare() {
	local i
	# MENU_CB_DESCS is declared here too, and that is not tidiness: Prune Skills
	# fills it and this menu did not clear it, so opening Install straight after
	# Prune showed prune descriptions under the component rows -- four of them
	# against three labels. The callback hooks go the same way, for the same
	# reason prune_skills.sh unsets them.
	declare -g -a MENU_CB_LABELS=() MENU_CB_STATUS=() MENU_CB_CHECKED=() MENU_CB_DESCS=()
	unset MENU_CB_TOGGLE_FN MENU_CB_ALL_FN MENU_CB_NONE_FN MENU_CB_DESC_FN
	for i in "${!AGENTBOT_COMPONENT_KEYS[@]}"; do
		MENU_CB_LABELS[i]="${AGENTBOT_COMPONENT_LABELS[$i]}"
		MENU_CB_STATUS[i]=''
		# Written since this menu was added and never shown: the descriptions
		# existed in AGENTBOT_COMPONENT_DESCS and were never handed to the
		# renderer, so the footer under the selection was blank.
		MENU_CB_DESCS[i]="${AGENTBOT_COMPONENT_DESCS[$i]}"
		# Everything on by default: the previous behaviour was to install all
		# three, so an operator who just presses Enter gets what they had.
		MENU_CB_CHECKED[i]=1
	done
	MENU_CB_TITLE='Install Agentbot'
	MENU_CB_BREADCRUMB='Agentbot › Install Agentbot'
	MENU_CB_HINT='Up/Down navigate   Space toggle   a all   n none   Enter confirm   q back'
	# Compact, as the sibling product's component selector is. The wide layout
	# reserves sixteen columns for a status none of these rows has, and draws
	# the `·` separators around the gap: `>  1. [x] ·                  · Skills`.
	MENU_CB_COMPACT=true
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

# The plan screen, drawn by the backend because it holds the state each row
# reports. Its exit status is the operator's answer: 0 confirm, 10 edit,
# anything else back.
_agentbot_install_plan() {
	local selection="$1" rc=0
	if [[ -n "${AGENTBOT_TUI:-}" ]]; then
		tui_refresh_tty_seam
		tui_run_to_output env \
			AGENTBOT_UPDATE_TTY_INPUT="$DOTFILES_TTY_INPUT" \
			AGENTBOT_UPDATE_TTY_IN_FD="$DOTFILES_TTY_IN_FD" \
			agentbot_run_backend install --plan-only --components "$selection" || rc=$?
	else
		agentbot_run_backend install --plan-only --components "$selection" || rc=$?
	fi
	return "$rc"
}

agentbot_menu_components() {
	local selection rc=0 plan_rc=0

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

	# Selector, then the plan, then the run -- the sibling product's shape. The
	# plan's `edit` comes back here, so the two screens are a loop rather than
	# a one-way street: an operator who sees the plan and wants a different
	# selection does not have to leave the menu and start again.
	while true; do
		_agentbot_components_prepare
		agentbot_checkbox_run || return 0

		selection="$(agentbot_components_selection)"
		if [[ -z "$selection" ]]; then
			printf '  %sNothing selected; install cancelled.%s\n' "${C_DIM:-}" "${C_RESET:-}"
			return 0
		fi

		tui_clear
		_agentbot_install_plan "$selection" || plan_rc=$?
		case "${plan_rc:-0}" in
		0) break ;;
		10)
			plan_rc=0
			continue
			;;
		*) return 0 ;;
		esac
	done

	# Through agentbot_menu_install, not around it: that function owns the TUI
	# output seam and the exit-3 repository-change contract, and calling the
	# backend directly from here silently dropped both.
	agentbot_menu_install --components "$selection" || rc=$?
	return "$rc"
}
