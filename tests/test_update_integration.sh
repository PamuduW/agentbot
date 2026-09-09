#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"

test_harness_report_init

test_repo_gate_short_circuits_unsafe_states() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	check_skills_deps() { :; }
	run_cli() { printf 'cli:%s\n' "$*" >>"$TEST_ROOT/calls"; }
	: >"$TEST_ROOT/calls"
	run_update_backend_for() {
		local gate_outcome="$1" gate_reason="$2"
		agentbot_repo_update_run() {
			printf -v "$3" '%s' "$gate_outcome"
			printf -v "$4" '%s' "$gate_reason"
			case "$gate_outcome" in
			stopped) return 1 ;;
			repository_changed) return 2 ;;
			esac
			return 0
		}
		run_update_backend_as update --dry-run >/dev/null 2>&1
	}
	set +e
	run_update_backend_for stopped dirty
	dirty_rc=$?
	run_update_backend_for repository_changed pulled
	pulled_rc=$?
	run_update_backend_for current current
	current_rc=$?
	set -e
	[[ "$dirty_rc" -ne 0 && "$pulled_rc" -eq 2 && "$current_rc" -eq 0 ]] || return 1
	[[ "$(<"$TEST_ROOT/calls")" == $'cli:status\ncli:update --dry-run' ]]
)

test_direct_update_shows_status_before_reconciliation() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	check_skills_deps() { :; }
	agentbot_repo_update_run() {
		printf -v "$3" '%s' current
		printf -v "$4" '%s' current
	}
	run_cli() { printf 'cli:%s\n' "$*" >>"$TEST_ROOT/direct-update.calls"; }
	: >"$TEST_ROOT/direct-update.calls"
	run_update_backend_as update --dry-run >/dev/null 2>&1 || return 1
	[[ "$(<"$TEST_ROOT/direct-update.calls")" == $'cli:status\ncli:update --dry-run' ]]
)

test_dirty_state_reports_changes_remote_history_and_blocks_backend() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local calls="$TEST_ROOT/dirty-update.calls"
	: >"$calls"
	run_cli() { printf 'cli:%s\n' "$*" >>"$calls"; }
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	agentbot_repo_update_run() {
		REPO_UPDATE_STATE=behind
		REPO_UPDATE_AHEAD=0
		REPO_UPDATE_BEHIND=3
		REPO_UPDATE_DIRTY=1
		REPO_UPDATE_UPSTREAM=origin/main
		REPO_UPDATE_CHANGES=$' M scripts/example.sh\n?? .cursor/rules/agentbot-policy.mdc'
		printf -v "$3" '%s' stopped
		printf -v "$4" '%s' dirty
		return 1
	}
	local output rc
	set +e
	output="$(run_update_backend_as update --dry-run 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -ne 0 ]] || return 1
	[[ "$output" == *'Repository update'* ]] || return 1
	[[ "$output" == *'2 local change(s)'* ]] || return 1
	[[ "$output" == *'origin/main'* && "$output" == *'3 commit(s) behind'* ]] || return 1
	[[ "$output" == *'blocked'* && "$output" == *'Local changes:'* ]] || return 1
	[[ "$output" == *' M scripts/example.sh'* ]] || return 1
	[[ "$output" == *'?? .cursor/rules/agentbot-policy.mdc'* ]] || return 1
	[[ "$output" == *'Repository pull and downstream updates stopped.'* ]] || return 1
	[[ ! -s "$calls" ]]
)

test_dirty_current_reports_verified_current_and_stops() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	agentbot_repo_update_run() {
		REPO_UPDATE_STATE=current
		REPO_UPDATE_DIRTY=1
		REPO_UPDATE_UPSTREAM=origin/main
		REPO_UPDATE_CHANGES='?? local-file'
		printf -v "$3" '%s' stopped
		printf -v "$4" '%s' dirty
		return 1
	}
	local output rc
	set +e
	output="$(run_update_backend_as update --dry-run 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -ne 0 && "$output" == *'origin/main'* && "$output" == *'current'* ]] || return 1
	[[ "$output" == *'?? local-file'* ]]
)

test_dirty_fetch_failure_reports_paths_and_unknown_freshness() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	agentbot_repo_update_run() {
		REPO_UPDATE_STATE=stopped
		REPO_UPDATE_DIRTY=1
		REPO_UPDATE_UPSTREAM=origin/main
		REPO_UPDATE_CHANGES='?? local-file'
		printf -v "$3" '%s' stopped
		printf -v "$4" '%s' fetch-failed
		return 1
	}
	local output rc
	set +e
	output="$(run_update_backend_as update --dry-run 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -ne 0 && "$output" == *'?? local-file'* ]] || return 1
	[[ "$output" == *'origin/main'* && "$output" == *'freshness unknown'* ]] || return 1
	[[ "$output" == *'Repository pull and downstream updates stopped.'* ]]
)

test_dirty_change_list_is_bounded_with_copyable_command() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local i output status_lines='' printed
	for i in $(seq 1 22); do status_lines+="?? path-${i}"$'\n'; done
	REPO_UPDATE_CHANGES="${status_lines%$'\n'}"
	output="$(print_repo_update_changes)"
	printed="$(grep -c '^  ?? path-' <<<"$output")"
	[[ "$printed" -eq 20 ]] || return 1
	[[ "$output" == *'... 2 more local change(s)'* ]] || return 1
	[[ "$output" == *'git -C '* && "$output" == *' status --short --untracked-files=all'* ]]
)

test_interactive_repo_decision_uses_tty_prompt_contract() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local prompted=''
	run_repo_update_prompt() {
		prompted="$1"
		[[ "${TEST_UPDATE_ANSWER:-no}" == yes ]]
	}
	AGENTBOT_UPDATE_INTERACTIVE=1 TEST_UPDATE_ANSWER=yes
	export AGENTBOT_UPDATE_INTERACTIVE TEST_UPDATE_ANSWER
	run_update_decision pull-behind || return 1
	[[ "$prompted" == pull-behind ]]
)

test_repo_prompt_renders_after_table_on_the_tty_stream() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local tty_input="$TEST_ROOT/update-prompt.input"
	local tty_output="$TEST_ROOT/update-prompt.output"
	local captured_stdout="$TEST_ROOT/update-prompt.stdout"
	local table_line prompt_line
	printf 'y\n' >"$tty_input"
	: >"$tty_output"
	: >"$captured_stdout"
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	REPO_UPDATE_STATE=behind
	REPO_UPDATE_BEHIND=6
	REPO_UPDATE_DIRTY=0
	REPO_UPDATE_UPSTREAM=origin/main
	AGENTBOT_UPDATE_TTY_INPUT="$tty_input"
	AGENTBOT_UPDATE_TTY_OUTPUT="$tty_output"

	run_repo_update_prompt pull-behind >"$captured_stdout" || return 1

	table_line="$(grep -n 'Repository update' "$tty_output" | cut -d: -f1)"
	prompt_line="$(grep -n 'Pull 6 commit(s) with --ff-only' "$tty_output" | cut -d: -f1)"
	[[ -n "$table_line" && -n "$prompt_line" && "$table_line" -lt "$prompt_line" ]] || return 1
	[[ ! -s "$captured_stdout" ]]
)

test_repo_prompt_supports_recover_and_replace() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local tty_input="$TEST_ROOT/replace-prompt.input"
	local tty_output="$TEST_ROOT/replace-prompt.output"
	printf 'y\n' >"$tty_input"
	: >"$tty_output"
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	REPO_UPDATE_STATE=diverged
	REPO_UPDATE_AHEAD=2
	REPO_UPDATE_BEHIND=3
	REPO_UPDATE_DIRTY=1
	REPO_UPDATE_CHANGES=' M scripts/example.sh'
	REPO_UPDATE_UPSTREAM=origin/main
	AGENTBOT_UPDATE_TTY_INPUT="$tty_input"
	AGENTBOT_UPDATE_TTY_OUTPUT="$tty_output"

	run_repo_update_prompt replace-local || return 1
	grep -Fq 'Back up local work and replace it with origin/main? [y/N]:' "$tty_output"
)

test_current_interactive_update_plan_uses_descriptor_backed_tty() (
	# Break caught: the Python update-plan confirmation reopens /dev/tty instead
	# of consuming the current TUI descriptor seam after the repository gate.
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local input="$TEST_ROOT/current-plan.input" output="$TEST_ROOT/current-plan.output" errors="$TEST_ROOT/current-plan.errors" rc=0
	printf 'n\n' >"$input"
	: >"$output"
	agentbot_repo_update_run() {
		printf -v "$3" '%s' current
		printf -v "$4" '%s' current
	}
	run_cli() {
		[[ "$1" == update ]] || return 1
		python3 -c '
from src.cli import confirm_update_plan
if not confirm_update_plan():
    print("Update cancelled.")
'
	}
	exec {AGENTBOT_UPDATE_TTY_IN_FD}<"$input"
	exec {AGENTBOT_UPDATE_TTY_OUT_FD}>>"$output"
	export AGENTBOT_UPDATE_TTY_IN_FD AGENTBOT_UPDATE_TTY_OUT_FD
	set +e
	run_update_backend_as update --interactive >"$TEST_ROOT/current-plan.stdout" 2>"$errors"
	rc=$?
	set -e
	exec {AGENTBOT_UPDATE_TTY_IN_FD}<&-
	exec {AGENTBOT_UPDATE_TTY_OUT_FD}>&-
	[[ "$rc" -eq 0 && ! -s "$errors" ]] || return 1
	[[ "$(<"$output")" == *'Apply this Agentbot update plan? [y/N] '* ]]
	[[ "$(<"$TEST_ROOT/current-plan.stdout")" == *'Update cancelled.'* ]]
)

test_repo_update_table_honors_tui_color_mode() (
	NO_COLOR=''
	AGENTBOT_TUI=1
	export NO_COLOR AGENTBOT_TUI
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local output
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	output="$(print_repo_update_table)"
	[[ "$output" == *$'\033[1m\033[33mRepository update\033[0m'* ]] || return 1
	[[ "$output" != *$'\033[38;5;208mRepository update\033[0m'* ]] || return 1
	[[ "$output" == *$'\033[33mcheck\033[0m'* ]]
)

test_ahead_repo_table_describes_recoverable_replacement() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local output
	git() {
		case "$*" in
		*'rev-parse --abbrev-ref HEAD'*) printf 'main\n' ;;
		*'rev-parse --short HEAD'*) printf 'abc123\n' ;;
		*) return 1 ;;
		esac
	}
	REPO_UPDATE_STATE=ahead
	REPO_UPDATE_AHEAD=2
	REPO_UPDATE_BEHIND=0
	REPO_UPDATE_DIRTY=0
	REPO_UPDATE_UPSTREAM=origin/main
	tui_cols() { printf '120\n'; }
	NO_COLOR=1 output="$(print_repo_update_table)"
	[[ "$output" == *'replace after backup'* && "$output" != *'continue'* ]]
)

test_full_runs_install_then_update_with_one_restart_budget() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local events="$TEST_ROOT/full.events"

	# both stages clean
	: >"$events"
	run_install() { printf 'install\n' >>"$events"; }
	check_skills_deps() { :; }
	run_update_backend_as() { printf 'update:%s\n' "$*" >>"$events"; }
	run_full >/dev/null || return 1
	[[ "$(<"$events")" == $'install\nupdate:update --yes' ]] || return 1

	# a repository change during install is retried exactly once
	: >"$events"
	local install_calls=0
	run_install() {
		install_calls=$((install_calls + 1))
		printf 'install\n' >>"$events"
		((install_calls == 1)) && return 2
		return 0
	}
	run_full >/dev/null || return 1
	[[ "$(<"$events")" == $'install\ninstall\nupdate:update --yes' ]] || return 1

	# a second repository change exhausts the budget and stops
	: >"$events"
	run_install() {
		printf 'install\n' >>"$events"
		return 2
	}
	local rc=0
	run_full >/dev/null 2>&1 || rc=$?
	[[ "$rc" -eq 1 && "$(wc -l <"$events")" -eq 2 ]] || return 1

	# a genuine failure propagates unchanged, without running update
	: >"$events"
	run_install() { return 23; }
	rc=0
	run_full >/dev/null 2>&1 || rc=$?
	[[ "$rc" -eq 23 && ! -s "$events" ]]
)

check 'full runs install then update with a one-restart budget' test_full_runs_install_then_update_with_one_restart_budget
check 'repo gate short-circuits stopped and changed-repository states' test_repo_gate_short_circuits_unsafe_states
check 'dirty update reports changes and remote history before blocking backend work' test_dirty_state_reports_changes_remote_history_and_blocks_backend
check 'dirty current repository reports verified current and stops' test_dirty_current_reports_verified_current_and_stops
check 'dirty fetch failure reports paths and unknown remote freshness' test_dirty_fetch_failure_reports_paths_and_unknown_freshness
check 'dirty change report caps paths and prints a copyable full-status command' test_dirty_change_list_is_bounded_with_copyable_command
check 'interactive update decisions use the TTY prompt seam' test_interactive_repo_decision_uses_tty_prompt_contract
check 'repository pull prompt renders below its table on the TTY stream' test_repo_prompt_renders_after_table_on_the_tty_stream
check 'repository prompt supports recover and replace decisions' test_repo_prompt_supports_recover_and_replace
check 'current interactive update plan uses the descriptor-backed TTY' test_current_interactive_update_plan_uses_descriptor_backed_tty
check 'repository update table honors the Agentbot TUI color mode' test_repo_update_table_honors_tui_color_mode
check 'ahead repository table describes recoverable replacement' test_ahead_repo_table_describes_recoverable_replacement
check 'direct update shows the status table before reconciliation' test_direct_update_shows_status_before_reconciliation
test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d update-integration test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
