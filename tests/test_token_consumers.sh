#!/usr/bin/env bash
# shellcheck disable=SC1091  # Owned entrypoints are intentionally sourced in isolated subshell tests.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"

test_harness_report_init
seq=0
# `expect` is this suite's spelling of the shared `check`.
expect() { check "$@"; }

token() {
	seq=$((seq + 1))
	printf '%s_%s_%024d' "${1:-consumer}" "$(date +%s%N)" "$seq"
}

stateless_token() {
	printf 'ghs_12345_%0250d.%0200d.%055d-x' 0 0 0
}

active_file() { printf '%s\n' "$XDG_CONFIG_HOME/agentbot/github.env"; }

write_token_file() {
	local file="$1" value="$2" mode="${3:-600}"
	mkdir -p "$(dirname "$file")"
	chmod 700 "$(dirname "$file")"
	printf 'GITHUB_TOKEN=%s\n' "$value" >"$file"
	chmod "$mode" "$file"
}

reset_state() {
	rm -rf "$XDG_CONFIG_HOME/agentbot" "$XDG_CONFIG_HOME/agent_bootstrap"
	unset GITHUB_TOKEN AGENTBOT_QUIET AGENTBOT_TUI
	unset TEST_CHILD_PID_FILE TEST_CHILD_RELEASE_FILE TEST_PYTHON_EXIT
	: >"$TEST_COMMAND_LOG"
	: >"$TEST_URL_LOG"
	: >"$TEST_SIBLING_LOG"
	: >"$TEST_RELAUNCH_LOG"
}

install_child_fake() {
	cat >"$TEST_FAKE_BIN/python3" <<'FAKE'
#!/usr/bin/env bash
set -u
if [[ "${1:-}" == '-c' ]]; then exit 0; fi
valid=no
source_kind=none
if [[ "${GITHUB_TOKEN:-}" == ghs_* ]]; then
  [[ "${GITHUB_TOKEN:-}" =~ ^ghs_[A-Za-z0-9._-]{36,}$ ]] && valid=yes
elif [[ "${GITHUB_TOKEN:-}" =~ ^[A-Za-z0-9_]{20,}$ ]]; then
  valid=yes
fi
case "${GITHUB_TOKEN:-}" in
  envpreferred_*) source_kind=environment ;;
  saved_*) source_kind=saved ;;
esac
source "${TEST_HARNESS_LIB:?}"
_harness_append_sanitized "$TEST_COMMAND_LOG" python3 "$@" "valid=$valid" "source=$source_kind"
if [[ -n "${TEST_CHILD_PID_FILE:-}" ]]; then
  printf '%s\n' "$$" >"$TEST_CHILD_PID_FILE"
  while [[ ! -e "${TEST_CHILD_RELEASE_FILE:?}" ]]; do sleep 0.01; done
fi
exit "${TEST_PYTHON_EXIT:-0}"
FAKE
	chmod 700 "$TEST_FAKE_BIN/python3"
}

run_install_script() {
	bash "$ROOT/install.sh" "$@"
}

test_sources_local_helper_only() {
	grep -Fq 'source "${REPO_ROOT}/scripts/lib/github_token.sh"' "$ROOT/install.sh" || return 1
	! grep -Eq 'source .*dotfiles|source .*/agent_bootstrap/github\.env' "$ROOT/install.sh"
}

test_mutating_skills_children_are_authenticated() (
	local subcmd
	for subcmd in install update upgrade; do
		reset_state
		write_token_file "$(active_file)" "saved_$(token saved)"
		run_install_script skills "$subcmd" >/dev/null || return 1
		grep -q "^python3"$'\t-m\tsrc\.cli\t--root\t[^\t]*\tskills\t'"${subcmd}"$'\tvalid=yes\tsource=saved$' "$TEST_COMMAND_LOG" || return 1
		[[ -z "${GITHUB_TOKEN:-}" ]] || return 1
	done
)

test_readonly_skills_children_are_unwrapped() (
	local subcmd err
	for subcmd in list doctor; do
		reset_state
		err="$TEST_ROOT/${subcmd}.err"
		write_token_file "$(active_file)" "saved_$(token saved)"
		run_install_script skills "$subcmd" >/dev/null 2>"$err" || return 1
		grep -q "^python3"$'\t-m\tsrc\.cli\t--root\t[^\t]*\tskills\t'"${subcmd}"$'\tvalid=no\tsource=none$' "$TEST_COMMAND_LOG" || return 1
		[[ ! -s "$err" ]] || return 1
	done
)

test_repo_update_child_is_authenticated() (
	reset_state
	write_token_file "$(active_file)" "saved_$(token saved)"
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	agentbot_repo_update_run() {
		printf -v "$3" '%s' current
		printf -v "$4" '%s' current
	}
	run_update_backend_as update --dry-run >/dev/null || return 1
	grep -q $'^python3\t-m\tsrc\.cli\t--root\t[^\t]*\tstatus\tvalid=no\tsource=none$' "$TEST_COMMAND_LOG" || return 1
	grep -q $'^python3\t-m\tsrc\.cli\t--root\t[^\t]*\tupdate\t--dry-run\tvalid=yes\tsource=saved$' "$TEST_COMMAND_LOG"
)

test_agentbot_install_child_is_authenticated() (
	reset_state
	write_token_file "$(active_file)" "saved_$(token saved)"
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	check_skills_deps() { :; }
	run_agentbot_install_backend >/dev/null || return 1
	grep -q $'^python3\t-m\tsrc\.cli\t--root\t[^\t]*\tinstall\tvalid=yes\tsource=saved$' "$TEST_COMMAND_LOG"
)

test_environment_precedence() (
	reset_state
	write_token_file "$(active_file)" "saved_$(token saved)"
	GITHUB_TOKEN="envpreferred_$(token parent)"
	export GITHUB_TOKEN
	run_install_script skills update >/dev/null || return 1
	grep -q $'^python3\t-m\tsrc\.cli\t--root\t[^\t]*\tskills\tupdate\tvalid=yes\tsource=environment$' "$TEST_COMMAND_LOG" || return 1
	[[ "$GITHUB_TOKEN" == envpreferred_* ]]
)

test_stateless_token_reaches_only_the_authenticated_child() (
	reset_state
	local value
	value="$(stateless_token)"
	write_token_file "$(active_file)" "$value"
	run_install_script skills update >/dev/null || return 1
	grep -q $'^python3\t-m\tsrc\.cli\t--root\t[^\t]*\tskills\tupdate\tvalid=yes\tsource=none$' "$TEST_COMMAND_LOG" || return 1
	[[ -z "${GITHUB_TOKEN:-}" ]] || return 1
	! grep -FRq -- "$value" "$TEST_COMMAND_LOG" "$TEST_URL_LOG" "$TEST_SIBLING_LOG" "$TEST_RELAUNCH_LOG"
)

test_child_status_propagates() (
	reset_state
	TEST_PYTHON_EXIT=37
	export TEST_PYTHON_EXIT
	set +e
	run_install_script skills install >/dev/null 2>&1
	local rc=$?
	set -e
	[[ "$rc" -eq 37 ]]
)

test_parent_never_gains_saved_token() (
	reset_state
	write_token_file "$(active_file)" "saved_$(token saved)"
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	github_token_child bash -c '[[ -n "${GITHUB_TOKEN:-}" ]]' || return 1
	[[ -z "${GITHUB_TOKEN:-}" ]]
)

test_canary_and_proc_safety() (
	reset_state
	local canary pid='' job cmdline output="$TEST_ROOT/canary.out"
	canary="saved_$(token canary)"
	write_token_file "$(active_file)" "$canary"
	TEST_CHILD_PID_FILE="$TEST_ROOT/python.pid"
	TEST_CHILD_RELEASE_FILE="$TEST_ROOT/python.release"
	export TEST_CHILD_PID_FILE TEST_CHILD_RELEASE_FILE
	run_install_script skills update >"$output" 2>&1 &
	job=$!
	for _ in {1..200}; do
		[[ -s "$TEST_CHILD_PID_FILE" ]] && {
			pid="$(<"$TEST_CHILD_PID_FILE")"
			break
		}
		sleep 0.01
	done
	[[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || {
		touch "$TEST_CHILD_RELEASE_FILE"
		wait "$job"
		return 1
	}
	cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
	touch "$TEST_CHILD_RELEASE_FILE"
	wait "$job" || return 1
	[[ "$cmdline" != *"$canary"* && "$cmdline" != *'GITHUB_TOKEN='* ]] || return 1
	! grep -FRq -- "$canary" "$output" "$TEST_COMMAND_LOG" "$TEST_URL_LOG" "$TEST_SIBLING_LOG" "$TEST_RELAUNCH_LOG" || return 1
	! "$TEST_REAL_GIT" -C "$ROOT" diff --no-ext-diff | grep -Fq -- "$canary"
)

test_no_token_bearing_arguments() {
	! grep -En -- '(-H|--header)[[:space:]].*Authorization|https?://[^/[:space:]]*@|GITHUB_TOKEN=.*run_cli' "$ROOT/install.sh"
}

test_sole_migration_owner() {
	local matches
	matches="$(grep -RIl --exclude=github_token.sh 'agent_bootstrap/github.env' "$ROOT/install.sh" "$ROOT/bin" "$ROOT/scripts" 2>/dev/null || true)"
	[[ -z "$matches" ]] || return 1
	! grep -Eq 'github_token_(migrate_legacy|read|write)' "$ROOT/install.sh"
}

install_child_fake
expect 'install.sh sources only the local token helper' test_sources_local_helper_only
expect 'mutating skills commands authenticate only their Python child' test_mutating_skills_children_are_authenticated
expect 'read-only skills commands neither authenticate nor warn' test_readonly_skills_children_are_unwrapped
expect 'repository update authenticates only the Python update child' test_repo_update_child_is_authenticated
expect 'Agentbot install authenticates only its Python child' test_agentbot_install_child_is_authenticated
expect 'valid environment token takes precedence over saved state' test_environment_precedence
expect 'stateless installation token reaches only the authenticated child' test_stateless_token_reaches_only_the_authenticated_child
expect 'child nonzero status propagates unchanged' test_child_status_propagates
expect 'saved token never remains in the calling shell' test_parent_never_gains_saved_token
expect 'canary is absent from output logs diff and sampled process cmdline' test_canary_and_proc_safety
expect 'owned entrypoint contains no token-bearing argument construction' test_no_token_bearing_arguments
expect 'token helper remains the sole migration and legacy-path owner' test_sole_migration_owner

test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d consumer test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
