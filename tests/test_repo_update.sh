#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"

test_harness_report_init
# `expect` is this suite's spelling of the shared `check`.
expect() { check "$@"; }

install_git_scenario_fake() {
	cat >"$TEST_FAKE_BIN/git" <<'FAKE'
#!/usr/bin/env bash
set -u
source "${TEST_HARNESS_LIB:?}"
_harness_append_sanitized "$TEST_COMMAND_LOG" git "$@"
args=("$@")
if [[ "${args[0]:-}" == -C ]]; then args=("${args[@]:2}"); fi
cmd="${args[*]}" scenario="${REPO_SCENARIO:-current}"
case "$cmd" in
  'rev-parse --is-inside-work-tree') [[ "$scenario" == invalid-repository ]] && { printf 'false\n'; exit 0; }; printf 'true\n' ;;
  'rev-parse --is-bare-repository') printf 'false\n' ;;
  'remote get-url origin')
    case "$scenario" in
      invalid-origin) printf 'https://github.com/other/repo.git\n' ;;
      token-origin) printf 'https://credential@github.com/PamuduW/agentbot.git\n' ;;
      alias-origin|alias-wrong-path)
        alias_owner='Other'
        [[ "$scenario" == alias-origin ]] && alias_owner='PamuduW'
        printf 'git@github-personal:%s/agentbot.git\n' "$alias_owner"
        ;;
      absent-origin) exit 2 ;;
      *) printf 'https://github.com/PamuduW/agentbot.git\n' ;;
    esac ;;
  'config --global --get-regexp ^url\..*\.insteadof$')
    [[ "$scenario" == alias-origin || "$scenario" == alias-wrong-path ]] &&
      printf '%s\n' 'url.git@github-personal:.insteadof git@github.com:'
    ;;
  'rev-parse --abbrev-ref --symbolic-full-name @{upstream}') [[ "$scenario" == no-upstream ]] && exit 1; printf 'origin/main\n' ;;
  'fetch --prune') [[ "$scenario" == fetch-failed || "$scenario" == dirty-fetch-failed ]] && exit 29; : ;;
  'status --short --untracked-files=all')
    [[ "$scenario" == status-failed ]] && exit 43
    case "$scenario" in
      dirty|dirty-*|recovery-stash-failed|recovery-incomplete|recovery-reset-failed)
        if [[ ! -f "$TEST_ROOT/recovery-stashed" || "$scenario" == recovery-incomplete ]]; then
          printf '%s\n' \
            ' M scripts/example.sh' \
            '?? .cursor/rules/agentbot-policy.mdc'
        fi
        ;;
    esac
    ;;
  'symbolic-ref --quiet --short HEAD') [[ "$scenario" == detached ]] && exit 1; printf 'main\n' ;;
  'rev-list --left-right --count HEAD...@{upstream}')
    case "$scenario" in
      ahead|dirty-ahead|recovery-branch-failed) printf '2\t0\n' ;;
      behind|pull-failed|dirty-behind) printf '0\t3\n' ;;
      diverged|dirty-diverged) printf '2\t3\n' ;;
      invalid-counts) printf 'not-counts\n' ;; *) printf '0\t0\n' ;;
    esac ;;
  'pull --ff-only') [[ "$scenario" == pull-failed ]] && exit 31; : ;;
  'stash push --include-untracked -m '*)
    [[ "$scenario" == recovery-stash-failed ]] && exit 41
    : >"$TEST_ROOT/recovery-stashed"; printf 'Saved working directory\n' ;;
  'rev-parse --verify refs/stash') printf 'agentbot-stash-object\n' ;;
  'show-ref --verify --quiet refs/heads/recovery/agentbot-'*) exit 1 ;;
  'branch recovery/agentbot-'*' HEAD') [[ "$scenario" == recovery-branch-failed ]] && exit 42; : ;;
  'reset --hard @{upstream}') [[ "$scenario" == recovery-reset-failed ]] && exit 43; : ;;
  *) printf 'unconfigured fake Git operation: %s\n' "$cmd" >&2; exit 97 ;;
esac
FAKE
	chmod 700 "$TEST_FAKE_BIN/git"
}

decision() {
	printf '%s\n' "$1" >>"$TEST_ROOT/decisions.log"
	[[ "${DECISION_APPROVE:-no}" == yes ]]
}

reset_case() {
	: >"$TEST_COMMAND_LOG"
	: >"$TEST_URL_LOG"
	: >"$TEST_SIBLING_LOG"
	: >"$TEST_RELAUNCH_LOG"
	: >"$TEST_ROOT/decisions.log"
	rm -f "$TEST_ROOT/recovery-stashed"
	REPO_SCENARIO="$1" DECISION_APPROVE="${2:-no}"
	export REPO_SCENARIO DECISION_APPROVE
	OUTCOME=unset REASON=unset
}

run_case() {
	reset_case "$1" "${2:-no}"
	repo_update_run "$TEST_ROOT/repo" decision OUTCOME REASON >/dev/null 2>&1
	RUN_RC=$?
	return 0
}
pull_count() { grep -c $'git\t-C\t.*\tpull\t--ff-only$' "$TEST_COMMAND_LOG" 2>/dev/null || true; }
decision_count() { wc -l <"$TEST_ROOT/decisions.log"; }

test_current() {
	run_case current
	[[ "$OUTCOME/$REASON" == current/current && "$(decision_count)" -eq 0 && "$(pull_count)" -eq 0 ]]
}
test_dirty() {
	run_case dirty
	[[ "$OUTCOME/$REASON" == stopped/replace-declined && "$(pull_count)" -eq 0 ]]
}
test_dirty_approved_replacement() {
	reset_case dirty yes
	local output_file="$TEST_ROOT/dirty-replacement.output"
	repo_update_run "$TEST_ROOT/repo" decision OUTCOME REASON >"$output_file" 2>&1
	RUN_RC=$?
	[[ "$OUTCOME/$REASON" == repository_changed/replaced && "$RUN_RC" -eq 2 ]] || return 1
	grep -Fq 'Recovery stash: agentbot-stash-object' "$output_file" || return 1
	local stash_line clean_line reset_line
	stash_line="$(grep -n $'git\t-C\t.*\tstash\tpush\t--include-untracked\t-m\t' "$TEST_COMMAND_LOG" | cut -d: -f1)"
	clean_line="$(grep -n $'git\t-C\t.*\tstatus\t--short\t--untracked-files=all$' "$TEST_COMMAND_LOG" | tail -n 1 | cut -d: -f1)"
	reset_line="$(grep -n $'git\t-C\t.*\treset\t--hard\t@{upstream}$' "$TEST_COMMAND_LOG" | cut -d: -f1)"
	[[ -n "$stash_line" && -n "$clean_line" && -n "$reset_line" ]] && ((stash_line < clean_line && clean_line < reset_line))
}
test_recovery_failures_stop_before_unsafe_followup() {
	local scenario expected
	while read -r scenario expected; do
		run_case "$scenario" yes
		[[ "$OUTCOME/$REASON" == "stopped/$expected" && "$RUN_RC" -eq 1 ]] || return 1
		case "$scenario" in
		recovery-branch-failed | recovery-stash-failed | recovery-incomplete)
			! grep -q $'\treset\t--hard\t@{upstream}$' "$TEST_COMMAND_LOG" || return 1
			;;
		esac
	done <<'TABLE'
recovery-branch-failed recovery-branch-failed
recovery-stash-failed stash-failed
recovery-incomplete recovery-incomplete
recovery-reset-failed reset-failed
TABLE
}
test_detached() {
	run_case detached
	[[ "$OUTCOME/$REASON" == stopped/detached && "$(pull_count)" -eq 0 ]]
}
test_no_upstream() {
	run_case no-upstream
	[[ "$OUTCOME/$REASON" == stopped/no-upstream ]] && ! grep -q $'\tfetch\t--prune$' "$TEST_COMMAND_LOG"
}
test_ahead_approved() {
	run_case ahead yes
	[[ "$OUTCOME/$REASON" == repository_changed/replaced && "$RUN_RC" -eq 2 && "$(pull_count)" -eq 0 ]] || return 1
	[[ "$REPO_UPDATE_RECOVERY_BRANCH" == recovery/agentbot-* ]] && grep -Fqx replace-local "$TEST_ROOT/decisions.log"
}
test_ahead_declined() {
	run_case ahead no
	[[ "$OUTCOME/$REASON" == stopped/replace-declined && "$(pull_count)" -eq 0 ]]
}
test_behind_declined() {
	run_case behind no
	[[ "$OUTCOME/$REASON" == stopped/behind-declined && "$(pull_count)" -eq 0 ]] && grep -Fqx pull-behind "$TEST_ROOT/decisions.log"
}
test_behind_pulled() {
	run_case behind yes
	[[ "$OUTCOME/$REASON" == repository_changed/pulled && "$RUN_RC" -eq 2 && "$(pull_count)" -eq 1 ]]
}
test_pull_failed() {
	run_case pull-failed yes
	[[ "$OUTCOME/$REASON" == stopped/pull-failed && "$(pull_count)" -eq 1 ]]
}
test_diverged() {
	run_case diverged yes
	[[ "$OUTCOME/$REASON" == repository_changed/replaced && "$RUN_RC" -eq 2 && "$(decision_count)" -eq 1 && "$(pull_count)" -eq 0 ]]
}
test_fetch_failed() {
	run_case fetch-failed yes
	[[ "$OUTCOME/$REASON" == stopped/fetch-failed ]] && grep -q $'\tstatus\t--short\t--untracked-files=all$' "$TEST_COMMAND_LOG"
}
test_invalid_counts() {
	run_case invalid-counts yes
	[[ "$OUTCOME/$REASON" == stopped/invalid-counts && "$(pull_count)" -eq 0 ]]
}

test_invalid_repo_and_origin() {
	local scenario
	for scenario in invalid-repository invalid-origin token-origin absent-origin; do
		run_case "$scenario" yes
		case "$scenario" in invalid-repository) [[ "$REASON" == invalid-repository ]] ;; *) [[ "$REASON" == invalid-origin ]] ;; esac || return 1
		! grep -q $'\tfetch\t--prune$' "$TEST_COMMAND_LOG" || return 1
	done
}

test_configured_alias_origin_is_allowed() {
	run_case alias-origin
	[[ "$OUTCOME/$REASON" == current/current ]] && ! grep -q $'\tpull\t--ff-only$' "$TEST_COMMAND_LOG"
}

test_configured_alias_wrong_path_is_rejected() {
	run_case alias-wrong-path
	[[ "$OUTCOME/$REASON" == stopped/invalid-origin ]] &&
		! grep -q $'\tfetch\t--prune$' "$TEST_COMMAND_LOG"
}

test_classify_history_output_parameter_table() {
	local scenario expected_state expected_reason actual_state actual_reason rc
	while read -r scenario expected_state expected_reason; do
		reset_case "$scenario"
		actual_state=unset actual_reason=unset
		repo_update_classify_history "$TEST_ROOT/repo" actual_state actual_reason
		rc=$?
		[[ "$rc" -eq 0 && "$actual_state" == "$expected_state" && "$actual_reason" == "$expected_reason" ]] || return 1
	done <<'TABLE'
current current current
ahead ahead ahead
behind behind behind
diverged diverged diverged
invalid-counts stopped invalid-counts
TABLE
}

test_status_failure_stops_before_fetch() {
	run_case status-failed yes
	[[ "$OUTCOME/$REASON" == stopped/status-failed ]] || return 1
	! grep -q $'\tfetch\t--prune$' "$TEST_COMMAND_LOG" || return 1
	[[ "$(decision_count)" -eq 0 && "$(pull_count)" -eq 0 ]]
}

test_dirty_matrix_fetches_classifies_and_stops() {
	local scenario expected_history
	while read -r scenario expected_history; do
		run_case "$scenario" no
		[[ "$OUTCOME/$REASON" == stopped/replace-declined ]] || return 1
		[[ "${REPO_UPDATE_DIRTY:-0}" -eq 1 ]] || return 1
		[[ "${REPO_UPDATE_CHANGES:-}" == *' M scripts/example.sh'* ]] || return 1
		[[ "${REPO_UPDATE_CHANGES:-}" == *'?? .cursor/rules/agentbot-policy.mdc'* ]] || return 1
		[[ "$REPO_UPDATE_STATE" == "$expected_history" ]] || return 1
		grep -q $'\tfetch\t--prune$' "$TEST_COMMAND_LOG" || return 1
		grep -q $'\trev-list\t--left-right\t--count\tHEAD...@{upstream}$' "$TEST_COMMAND_LOG" || return 1
		[[ "$(pull_count)" -eq 0 && "$(decision_count)" -eq 1 ]] || return 1
	done <<'TABLE'
dirty-current current
dirty-ahead ahead
dirty-behind behind
dirty-diverged diverged
TABLE
}

test_dirty_fetch_failure_preserves_changes_and_stops() {
	run_case dirty-fetch-failed yes
	[[ "$OUTCOME/$REASON" == stopped/fetch-failed ]] || return 1
	[[ "${REPO_UPDATE_DIRTY:-0}" -eq 1 ]] || return 1
	[[ -n "${REPO_UPDATE_CHANGES:-}" ]] || return 1
	[[ "$(pull_count)" -eq 0 && "$(decision_count)" -eq 0 ]]
}

test_git_ordering() {
	run_case behind yes
	local log="$TEST_COMMAND_LOG"
	awk -F '\t' '
    /rev-parse.*--is-inside-work-tree/ {work=NR}
    /remote.*get-url.*origin/ {origin=NR}
    /symbolic-ref.*--quiet.*--short.*HEAD/ {branch=NR}
    /rev-parse.*symbolic-full-name.*upstream/ && !upstream {upstream=NR}
    /status.*--short.*--untracked-files=all/ {status=NR}
    /fetch.*--prune/ {fetch=NR}
    /rev-list.*--left-right.*--count/ {counts=NR}
    /pull.*--ff-only/ {pull=NR}
    END { exit !(work<origin && origin<branch && branch<upstream && upstream<status && status<fetch && fetch<counts && counts<pull) }
  ' "$log"
}

test_pull_only_complete_table() {
	local scenario approve expected
	while read -r scenario approve expected; do
		run_case "$scenario" "$approve"
		[[ "$(pull_count)" -eq "$expected" ]] || return 1
	done <<'TABLE'
current no 0
dirty yes 0
dirty-ahead yes 0
dirty-behind yes 0
dirty-diverged yes 0
dirty-fetch-failed yes 0
detached yes 0
no-upstream yes 0
ahead yes 0
behind no 0
behind yes 1
diverged yes 0
fetch-failed yes 0
invalid-counts yes 0
TABLE
}

test_success_returns_at_pull_boundary() (
	run_case behind yes
	[[ "$OUTCOME/$REASON" == repository_changed/pulled && "$RUN_RC" -eq 2 ]] || return 1
	[[ "$(tail -n 1 "$TEST_COMMAND_LOG")" == *$'\tpull\t--ff-only' ]] || return 1
	[[ ! -s "$TEST_RELAUNCH_LOG" && ! -s "$TEST_SIBLING_LOG" && ! -s "$TEST_URL_LOG" ]]
)

test_exit_contract() {
	run_case current
	[[ "$RUN_RC" -eq 0 ]] || return 1
	run_case dirty no
	[[ "$RUN_RC" -eq 1 ]] || return 1
	run_case behind yes
	[[ "$RUN_RC" -eq 2 ]]
}

test_safety_and_scope() {
	[[ "$(command -v git)" == "$TEST_FAKE_BIN/git" && ! -e "$TEST_FAKE_BIN/exec" ]] || return 1
	[[ ! -s "$TEST_URL_LOG" && ! -s "$TEST_SIBLING_LOG" ]] || return 1
	! grep -Eq '(^|[^[:alpha:]])(apt|curl|npx|skills|reconcile|doctor|install|render)([^[:alpha:]]|$)|exec[[:space:]]' "$ROOT/scripts/lib/repo_update.sh"
}

install_git_scenario_fake
mkdir -p "$TEST_ROOT/repo"
[[ -f "$ROOT/scripts/lib/repo_update.sh" ]] && source "$ROOT/scripts/lib/repo_update.sh"
declare -F repo_update_run >/dev/null || repo_update_run() {
	printf -v "$3" stopped
	printf -v "$4" missing
	return 1
}

test_binding_overrides_are_declared_not_accidental() {
	# scripts/lib/shared/repo_update.sh is byte-identical in both repositories
	# and the drift check enforces that. Identical text is not identical
	# behaviour: this binding sources the shared machine and then redefines four
	# of its functions, so in Agentbot the shared versions never run. The
	# signatures differ too --
	#
	#   shared:   repo_update_run <repo_dir> <repo_label> <confirm_fn> <result_name> [slug]
	#   binding:  repo_update_run <repo> <decision_fn> <outcome_var> <reason_var> [repository]
	#
	# -- so a reader who looks the name up in the shared file reads the wrong
	# contract, and a fix applied there silently reaches Dotfiles only.
	#
	# The overrides are deliberate. This test pins the set so a fifth one cannot
	# appear unnoticed, and so removing one is a decision rather than an
	# accident.
	local shared="$ROOT/scripts/lib/shared/repo_update.sh"
	local binding="$ROOT/scripts/lib/repo_update.sh"
	local expected='repo_update_is_declined repo_update_print_changed repo_update_print_recovery repo_update_run'
	local actual

	actual="$(comm -12 \
		<(grep -oE '^[a-z_]+\(\)' "$shared" | tr -d '()' | sort -u) \
		<(grep -oE '^[a-z_]+\(\)' "$binding" | tr -d '()' | sort -u) | tr '\n' ' ')"
	actual="${actual% }"

	if [[ "$actual" != "$expected" ]]; then
		printf 'shared functions the binding overrides changed\n  expected: %s\n  actual:   %s\n' \
			"$expected" "$actual" >&2
		return 1
	fi
}

expect 'current returns current without decision or pull' test_current
expect 'dirty stops without pull' test_dirty
expect 'approved dirty replacement stashes and verifies before reset' test_dirty_approved_replacement
expect 'recovery failures retain distinct reasons and block unsafe followup' test_recovery_failures_stop_before_unsafe_followup
expect 'detached stops without pull' test_detached
expect 'missing upstream stops before fetch' test_no_upstream
expect 'ahead approval creates a recovery branch and replaces local history' test_ahead_approved
expect 'ahead replacement decline stops without pull' test_ahead_declined
expect 'behind decline stops without pull' test_behind_declined
expect 'behind approval pulls ff-only once and reports a changed repository' test_behind_pulled
expect 'failed confirmed pull stops with pull-failed' test_pull_failed
expect 'diverged approval preserves a recovery branch and replaces local history' test_diverged
expect 'fetch failure stops before classification' test_fetch_failed
expect 'malformed counts stop with invalid-counts' test_invalid_counts
expect 'invalid repository and origin stop before fetch' test_invalid_repo_and_origin
expect 'configured SSH alias resolving to Agentbot is accepted' test_configured_alias_origin_is_allowed
expect 'configured SSH alias resolving to another path is rejected' test_configured_alias_wrong_path_is_rejected
expect 'history classifier writes exact state and reason output parameters' test_classify_history_output_parameter_table
expect 'failed status probe stops before fetch' test_status_failure_stops_before_fetch
expect 'dirty states fetch classify and stop without decisions or pull' test_dirty_matrix_fetches_classifies_and_stops
expect 'dirty fetch failure preserves local changes and stops' test_dirty_fetch_failure_preserves_changes_and_stops
expect 'Git sequence is validate origin branch upstream status fetch classify pull' test_git_ordering
expect 'only clean confirmed behind state pulls across full table' test_pull_only_complete_table
expect 'successful pull returns at adapter boundary without relaunch or extra commands' test_success_returns_at_pull_boundary
expect 'repository update uses one exit contract for continue, stop, and changed states' test_exit_contract
expect 'harness prevents exec network skills home and repository mutation' test_safety_and_scope
expect 'the binding overrides exactly the four shared functions it declares' test_binding_overrides_are_declared_not_accidental

test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d repo-update test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
