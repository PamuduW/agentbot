# shellcheck shell=bash
# shellcheck disable=SC2034  # REPO_UPDATE_* globals are this module's published output.
# Agentbot binding for the shared repository-update state machine.
#
# The state machine itself lives in scripts/lib/shared/repo_update.sh and is
# shared verbatim with the sibling repository. This file supplies the Agentbot
# identity and keeps the calling convention Agentbot's callers already use:
#
#   agentbot_repo_update_run <repo> <decision_fn> <outcome_var> <reason_var> [repository]
#
# plus the REPO_UPDATE_* globals its callers read, and Agentbot's own
# result vocabulary (invalid-repository, invalid-origin, replaced, pulled).
#
# The wrappers carry the agentbot_ prefix, and that is the point of the prefix:
# they used to be called repo_update_run and the rest, which are names the
# shared file also defines with different signatures. Sourcing the shared
# machine and then redefining four of its functions meant that in Agentbot the
# shared versions never ran, while `sync-shared.sh --check` proved the shared
# file byte-identical in both repositories and so read as "both behave the
# same". Nothing said otherwise at any call site. Now the name does.

if [[ "${_AGENTBOT_REPO_UPDATE_LOADED:-0}" == 1 ]]; then
	return 0
fi
_AGENTBOT_REPO_UPDATE_LOADED=1

if ! declare -F tui_cols >/dev/null 2>&1; then
	# shellcheck disable=SC1091
	source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/tui.sh"
fi

REPO_UPDATE_RECOVERY_PREFIX=agentbot
# Agentbot's decision prompt renders its own table before asking, so the shared
# machine must not print a second one.
REPO_UPDATE_REPORT_FN=_agentbot_repo_update_no_report
# Read by the shared renderer, which repo_update_print_report calls below. The
# shared default is the Dotfiles breadcrumb, and it must be set before the
# shared file is sourced: it claims the name with `: "${VAR:=...}"`.
REPO_UPDATE_REPORT_BREADCRUMB='Agentbot › Update › Repository'
export REPO_UPDATE_RECOVERY_PREFIX

_agentbot_repo_update_no_report() { :; }

# shellcheck source=scripts/lib/shared/repo_update.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/shared/repo_update.sh"

REPO_UPDATE_STATE=stopped
REPO_UPDATE_AHEAD=0
REPO_UPDATE_BEHIND=0
REPO_UPDATE_DIRTY=0
REPO_UPDATE_CHANGES=''
REPO_UPDATE_UPSTREAM=''
REPO_UPDATE_RECOVERY_BRANCH=''
REPO_UPDATE_RECOVERY_STASH=''
REPO_UPDATE_RECOVERY_REASON=''

declare -A _AGENTBOT_REPO_RESULT=()

# The shared machine and Agentbot name some stop reasons differently. Agentbot
# collapses "there is no origin" and "origin is not ours" into one
# invalid-origin, and calls a non-worktree invalid-repository.
_agentbot_repo_update_reason() {
	case "$1" in
	invalid) printf 'invalid-repository\n' ;;
	no-origin | wrong-origin | non-origin-upstream) printf 'invalid-origin\n' ;;
	*) printf '%s\n' "$1" ;;
	esac
}

# Mirror the shared result into the globals the bootstrap entrypoint reads.
_agentbot_repo_update_export() {
	REPO_UPDATE_STATE="${_AGENTBOT_REPO_RESULT[state]:-stopped}"
	REPO_UPDATE_AHEAD="${_AGENTBOT_REPO_RESULT[ahead]:-0}"
	REPO_UPDATE_BEHIND="${_AGENTBOT_REPO_RESULT[behind]:-0}"
	REPO_UPDATE_DIRTY="${_AGENTBOT_REPO_RESULT[dirty]:-0}"
	REPO_UPDATE_CHANGES="${_AGENTBOT_REPO_RESULT[changes]:-}"
	REPO_UPDATE_UPSTREAM="${_AGENTBOT_REPO_RESULT[upstream]:-}"
	REPO_UPDATE_RECOVERY_BRANCH="${_AGENTBOT_REPO_RESULT[recovery_branch]:-}"
	REPO_UPDATE_RECOVERY_STASH="${_AGENTBOT_REPO_RESULT[recovery_stash]:-}"
}

repo_update_change_count() {
	if [[ -z "${REPO_UPDATE_CHANGES:-}" ]]; then
		printf '0\n'
		return
	fi
	awk 'END { print NR }' <<<"$REPO_UPDATE_CHANGES"
}

# Standalone classifier kept for callers and tests that classify without
# running the whole machine.
repo_update_classify_history() {
	local repo="$1" state_name="$2" reason_name="$3"
	local counts state reason

	REPO_UPDATE_AHEAD=0
	REPO_UPDATE_BEHIND=0

	if ! counts="$(git -C "$repo" rev-list --left-right --count 'HEAD...@{upstream}' 2>/dev/null)"; then
		state=stopped reason=invalid-counts
	elif [[ "$counts" =~ ^([0-9]+)[[:space:]]+([0-9]+)$ ]]; then
		REPO_UPDATE_AHEAD="${BASH_REMATCH[1]}"
		REPO_UPDATE_BEHIND="${BASH_REMATCH[2]}"
		if ((REPO_UPDATE_AHEAD > 0 && REPO_UPDATE_BEHIND > 0)); then
			state=diverged
		elif ((REPO_UPDATE_AHEAD > 0)); then
			state=ahead
		elif ((REPO_UPDATE_BEHIND > 0)); then
			state=behind
		else
			state=current
		fi
		reason="$state"
	else
		state=stopped reason=invalid-counts
	fi

	printf -v "$state_name" '%s' "$state"
	printf -v "$reason_name" '%s' "$reason"
	REPO_UPDATE_STATE="$state"
}

# The result table, rendered by the shared table the sibling product uses.
#
# This carried its own four-column layout -- widths proportional to the terminal
# -- and its own colour mapping for the available and action columns, against
# the shared fixed 18/28/22/16 and status_color_available/status_color_action.
# Two implementations of one table, so the same repository state was drawn two
# ways depending on which product printed it. The proportional layout had
# exactly one caller: this function.
#
# The signature stays as Agentbot's callers use it -- a repository path, with
# the state read from the REPO_UPDATE_* globals -- so the globals are folded
# back into the result array the shared renderer takes.
repo_update_print_report() {
	local repo="$1"
	local -A report=()
	colors_complete_palette
	report[dir]="$repo"
	report[label]='agentbot repo'
	report[state]="${REPO_UPDATE_STATE:-stopped}"
	report[ahead]="${REPO_UPDATE_AHEAD:-0}"
	report[behind]="${REPO_UPDATE_BEHIND:-0}"
	report[dirty]="${REPO_UPDATE_DIRTY:-0}"
	report[changes]="${REPO_UPDATE_CHANGES:-}"
	report[upstream]="${REPO_UPDATE_UPSTREAM:-upstream}"
	_repo_update_print_result_default report
}

# Agentbot's callers hold the reason as a string, where the shared machine
# holds a result array. Defined after the shared source so this spelling wins.
agentbot_repo_update_is_declined() {
	case "${1:-unknown}" in
	behind-declined | ahead-declined | replace-declined) return 0 ;;
	*) return 1 ;;
	esac
}

_repo_update_color_output_enabled() {
	[[ -z "${NO_COLOR:-}" && (-t 1 || -t 0 || -n "${AGENTBOT_TUI:-}" || -n "${FORCE_COLOR:-}") ]]
}

repo_update_print_declined() {
	local action="${1:-pull-behind}" red="${C_RED:-}" reset="${C_RESET:-}"
	if [[ -z "$red" ]] && _repo_update_color_output_enabled; then
		red=$'\033[31m'
		reset=$'\033[0m'
	fi
	case "$action" in
	pull-behind) printf '\n\n  %sPull declined; update stopped.%s\n' "$red" "$reset" ;;
	*) printf '\n\n  %sUpdate stopped; no downstream work was run.%s\n' "$red" "$reset" ;;
	esac
}

agentbot_repo_update_print_changed() {
	local green="${C_GREEN:-}" reset="${C_RESET:-}"
	if [[ -z "$green" ]] && _repo_update_color_output_enabled; then
		green=$'\033[32m'
		reset=$'\033[0m'
	fi
	printf '  %sRepository fast-forward succeeded%s\n\n' "$green" "$reset"
	printf '  Run setup again when ready.\n'
}

agentbot_repo_update_print_recovery() {
	[[ -n "${REPO_UPDATE_RECOVERY_BRANCH:-}${REPO_UPDATE_RECOVERY_STASH:-}" ]] || return 0
	printf '  Recovery data preserved:\n'
	[[ -n "${REPO_UPDATE_RECOVERY_BRANCH:-}" ]] &&
		printf '    Recovery branch: %s\n' "$REPO_UPDATE_RECOVERY_BRANCH"
	[[ -n "${REPO_UPDATE_RECOVERY_STASH:-}" ]] &&
		printf '    Recovery stash: %s\n' "$REPO_UPDATE_RECOVERY_STASH"
	return 0
}

agentbot_repo_update_run() {
	# Contract: 0 continue, 1 stopped, 2 the checkout changed and all
	# higher-level work must stop so the user can rerun from the new state.
	local repo="$1" decision_fn="$2" outcome_name="$3" reason_name="$4"
	local repository="${5:-agentbot}"
	local slug="PamuduW/${repository}"
	local outcome reason rc=0

	_AGENTBOT_REPO_RESULT=()
	repo_update_preflight "$repo" 'agentbot repo' _AGENTBOT_REPO_RESULT "$slug"
	_agentbot_repo_update_export

	if [[ "${_AGENTBOT_REPO_RESULT[safe]}" != 1 &&
		"${_AGENTBOT_REPO_RESULT[reason]}" != dirty &&
		"${_AGENTBOT_REPO_RESULT[state]}" != diverged ]]; then
		outcome=stopped
		reason="$(_agentbot_repo_update_reason "${_AGENTBOT_REPO_RESULT[reason]}")"
		printf -v "$outcome_name" '%s' "$outcome"
		printf -v "$reason_name" '%s' "$reason"
		return 1
	fi

	if ! repo_update_request_approval _AGENTBOT_REPO_RESULT "$decision_fn"; then
		_agentbot_repo_update_export
		printf -v "$outcome_name" '%s' stopped
		printf -v "$reason_name" '%s' \
			"$(_agentbot_repo_update_reason "${_AGENTBOT_REPO_RESULT[reason]}")"
		return 1
	fi

	local replaced=0
	[[ "${_AGENTBOT_REPO_RESULT[apply_action]:-}" == replace ]] && replaced=1

	if ! repo_update_apply _AGENTBOT_REPO_RESULT; then
		_agentbot_repo_update_export
		REPO_UPDATE_RECOVERY_REASON="${_AGENTBOT_REPO_RESULT[reason]}"
		agentbot_repo_update_print_recovery
		printf -v "$outcome_name" '%s' stopped
		printf -v "$reason_name" '%s' \
			"$(_agentbot_repo_update_reason "${_AGENTBOT_REPO_RESULT[reason]}")"
		return 1
	fi

	_agentbot_repo_update_export
	agentbot_repo_update_print_recovery

	case "${_AGENTBOT_REPO_RESULT[outcome]}" in
	repository_changed)
		outcome=repository_changed
		((replaced)) && reason=replaced || reason=pulled
		rc=2
		;;
	current)
		outcome=current reason=current rc=0
		;;
	*)
		outcome="${_AGENTBOT_REPO_RESULT[outcome]}"
		reason="$(_agentbot_repo_update_reason "${_AGENTBOT_REPO_RESULT[reason]}")"
		rc=0
		;;
	esac

	printf -v "$outcome_name" '%s' "$outcome"
	printf -v "$reason_name" '%s' "$reason"
	return "$rc"
}
