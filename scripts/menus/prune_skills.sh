#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_CB globals are consumed by the checkbox runner.

_agentbot_load_prune_candidates() {
	local listing name reason detail rc=0
	listing="$(mktemp "${TMPDIR:-/tmp}/agentbot-prune-skills.XXXXXX")" || return 1
	agentbot_run_backend skills prune --candidates0 >"$listing" || rc=$?
	if ((rc != 0)); then
		rm -f -- "$listing"
		return "$rc"
	fi
	AGENTBOT_PRUNE_SKILLS=()
	AGENTBOT_PRUNE_REASONS=()
	AGENTBOT_PRUNE_DETAILS=()
	while IFS= read -r -d '' name &&
		IFS= read -r -d '' reason &&
		IFS= read -r -d '' detail; do
		AGENTBOT_PRUNE_SKILLS+=("$name")
		AGENTBOT_PRUNE_REASONS+=("$reason")
		AGENTBOT_PRUNE_DETAILS+=("$detail")
	done <"$listing"
	rm -f -- "$listing"
}

agentbot_menu_prune_skills() {
	local index selected_label rc=0
	local -a selected=()
	declare -g -a AGENTBOT_PRUNE_SKILLS=() AGENTBOT_PRUNE_REASONS=() AGENTBOT_PRUNE_DETAILS=()

	_agentbot_load_prune_candidates || return $?
	if ((${#AGENTBOT_PRUNE_SKILLS[@]} == 0)); then
		printf 'No prunable skills found.\n'
		return 0
	fi

	declare -g -a MENU_CB_LABELS=() MENU_CB_STATUS=() MENU_CB_CHECKED=() MENU_CB_DESCS=()
	for index in "${!AGENTBOT_PRUNE_SKILLS[@]}"; do
		MENU_CB_LABELS[index]="${AGENTBOT_PRUNE_SKILLS[index]}"
		MENU_CB_STATUS[index]="${AGENTBOT_PRUNE_REASONS[index]}"
		MENU_CB_DESCS[index]="${AGENTBOT_PRUNE_DETAILS[index]}"
		MENU_CB_CHECKED[index]=0
	done
	MENU_CB_TITLE='Prune Skills'
	MENU_CB_BREADCRUMB='Agentbot › Prune Skills'
	MENU_CB_HINT='Up/Down navigate   Space toggle   a all   n none   Enter confirm   q back'
	MENU_CB_COMPACT=false
	unset MENU_CB_TOGGLE_FN MENU_CB_ALL_FN MENU_CB_NONE_FN MENU_CB_DESC_FN
	agentbot_checkbox_run || return 0

	for index in "${!AGENTBOT_PRUNE_SKILLS[@]}"; do
		if [[ "${MENU_CB_CHECKED[index]}" -eq 1 ]]; then
			selected+=("${AGENTBOT_PRUNE_SKILLS[index]}")
		fi
	done
	if ((${#selected[@]} == 0)); then
		printf 'No skills selected for pruning.\n'
		return 0
	fi

	selected_label="${selected[0]}"
	for ((index = 1; index < ${#selected[@]}; index++)); do
		selected_label+=", ${selected[index]}"
	done
	tui_confirm \
		"Permanently prune ${#selected[@]} skills (${selected_label})?" || return 0
	tui_run_to_output agentbot_run_backend skills prune "${selected[@]}" --yes || rc=$?
	((rc == 0)) || return "$rc"
	tui_run_to_output agentbot_run_backend skills install
}
