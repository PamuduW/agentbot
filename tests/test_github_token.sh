#!/usr/bin/env bash
# shellcheck disable=SC1091  # Harness path is dynamically rooted beside this test.
# shellcheck disable=SC2030,SC2031  # Token environment changes are intentionally isolated in test subshells.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tests/lib/harness.sh
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"

TOKEN_LIB="$ROOT/scripts/lib/github_token.sh"
if [[ -f "$TOKEN_LIB" ]]; then
	# shellcheck source=scripts/lib/github_token.sh
	source "$TOKEN_LIB"
fi

test_harness_report_init
seq=0
# `expect` is this suite's spelling of the shared `check`.
expect() { check "$@"; }

token() {
	seq=$((seq + 1))
	printf 'ghp_agentbot_%s_%024d' "${1:-test}" "$seq"
}
stateless_token() {
	printf 'ghs_12345_%0250d.%0200d.%055d-x' 0 0 0
}
active_file() { printf '%s\n' "$XDG_CONFIG_HOME/agentbot/github.env"; }
reset_state() {
	rm -rf "$XDG_CONFIG_HOME/agentbot"
	unset GITHUB_TOKEN
}
write_token_file() {
	local file="$1" content="$2" mode="${3:-600}"
	mkdir -p "$(dirname "$file")"
	chmod 700 "$(dirname "$file")"
	printf '%b' "$content" >"$file"
	chmod "$mode" "$file"
}
require_contract() {
	local fn
	for fn in github_token_file github_token_is_valid github_token_read github_token_export_if_valid github_token_write github_token_remove github_token_fingerprint; do
		declare -F "$fn" >/dev/null || return 1
	done
}

test_interface_and_independence() (
	require_contract || return 1
	[[ "$(github_token_file)" == "$(active_file)" ]] || return 1
	! grep -Fq '/dotfiles/' "$TOKEN_LIB" || return 1
	# The pre-rename agent_bootstrap token path is retired, not migrated.
	! grep -Fq 'agent_bootstrap' "$TOKEN_LIB"
)

test_precedence_and_absent_optional() (
	require_contract || return 1
	reset_state
	local saved env value='x' err="$TEST_ROOT/precedence.err"
	saved="$(token saved)"
	env="$(token env)"
	write_token_file "$(active_file)" "GITHUB_TOKEN=${saved}\n"
	GITHUB_TOKEN="$env"
	export GITHUB_TOKEN
	github_token_export_if_valid 2>"$err" || return 1
	[[ "$GITHUB_TOKEN" == "$env" && ! -s "$err" ]] || return 1
	reset_state
	github_token_read value 2>"$err" || return 1
	[[ -z "$value" && ! -s "$err" ]]
)

test_strict_parser_and_warning_fallback() (
	require_contract || return 1
	reset_state
	local good value='' err="$TEST_ROOT/parser.err" marker="$TEST_ROOT/executed" content
	good="$(token good)"
	write_token_file "$(active_file)" "GITHUB_TOKEN=${good}\n"
	github_token_read value >"$TEST_ROOT/parser.out" 2>"$err" || return 1
	[[ "$value" == "$good" && ! -s "$TEST_ROOT/parser.out" && ! -s "$err" ]] || return 1
	local cases=(
		"GITHUB_TOKEN=${good}\nSECOND=x\n" "GH_TOKEN=${good}\n"
		"GITHUB_TOKEN=\$(touch ${marker})\n" " GITHUB_TOKEN=${good}\n"
		"GITHUB_TOKEN=${good} \n" "GITHUB_TOKEN=${good}\tbad\n" 'GITHUB_TOKEN=short\n'
		"GITHUB_TOKEN=${good}"
	)
	for content in "${cases[@]}"; do
		write_token_file "$(active_file)" "$content"
		value=x
		github_token_read value 2>"$err" || return 1
		[[ -z "$value" && "$(wc -l <"$err")" -eq 1 ]] || return 1
	done
	[[ ! -e "$marker" ]] || return 1
	write_token_file "$(active_file)" "GITHUB_TOKEN=${good}\n" 644
	unset GITHUB_TOKEN
	github_token_export_if_valid 2>"$err" || return 1
	[[ -z "${GITHUB_TOKEN:-}" && "$(wc -l <"$err")" -eq 1 ]]
)

test_stateless_installation_token_round_trip() (
	require_contract || return 1
	reset_state
	local value token
	token="$(stateless_token)"
	[[ ${#token} -ge 500 ]] || return 1
	github_token_is_valid "$token" || return 1
	github_token_write "$token" || return 1
	github_token_read value || return 1
	[[ "$value" == "$token" ]] || return 1
	! github_token_is_valid 'ghs_short' || return 1
	! github_token_is_valid "${token}/unsafe" || return 1
	! github_token_is_valid "ordinary.${token#ghs_}" || return 1
)

test_private_atomic_storage_and_removal() (
	require_contract || return 1
	reset_state
	local one two outside="$TEST_ROOT/outside" err="$TEST_ROOT/write.err"
	one="$(token one)"
	two="$(token two)"
	github_token_write "$one" 2>"$err" || return 1
	[[ "$(stat -c %a "$(dirname "$(active_file)")")" == 700 ]] || return 1
	[[ "$(stat -c %a "$(active_file)")" == 600 ]] || return 1
	github_token_write "$two" || return 1
	[[ "$(<"$(active_file)")" == "GITHUB_TOKEN=$two" ]] || return 1
	[[ -z "$(find "$(dirname "$(active_file)")" -name '.github.env.*' -print -quit)" ]] || return 1
	github_token_remove || return 1
	[[ ! -e "$(active_file)" ]] || return 1
	printf 'safe\n' >"$outside"
	ln -s "$outside" "$(active_file)"
	! github_token_write "$one" 2>"$err" || return 1
	[[ "$(<"$outside")" == safe ]]
)

test_fingerprint_is_safe() (
	require_contract || return 1
	local value fp
	value="$(token fingerprint)"
	fp="$(github_token_fingerprint "$value")"
	[[ -n "$fp" && "$fp" != "$value" && "$fp" == *"${value: -4}"* ]]
)

test_canary_absent_from_output_and_harness_logs() (
	require_contract || return 1
	reset_state
	local canary output="$TEST_ROOT/canary.out" git_log="$TEST_ROOT/git.log"
	local process_sample diff_sample report_path
	canary="$(token canary)"
	export TEST_CANARY_SECRET="$canary"
	write_token_file "$(active_file)" "GITHUB_TOKEN=${canary}\n"
	unset GITHUB_TOKEN
	github_token_export_if_valid >"$output" 2>&1 || return 1
	: >"$git_log"
	process_sample="$(ps -o args= -p "$$" 2>/dev/null || true)"
	diff_sample="$($TEST_REAL_GIT -C "$ROOT" diff --no-ext-diff 2>/dev/null || true)"
	report_path="${ROOT%/agentbot}/temp/sdd/task-07-agentbot-token-report.md"
	[[ "$process_sample" != *"$canary"* ]] || return 1
	[[ "$diff_sample" != *"$canary"* ]] || return 1
	if [[ -f "$report_path" ]] && grep -Fq -- "$canary" "$report_path"; then return 1; fi
	! grep -FRq -- "$canary" "$output" "$TEST_COMMAND_LOG" "$TEST_URL_LOG" "$TEST_SIBLING_LOG" "$TEST_RELAUNCH_LOG" "$git_log"
)

expect 'independent Agentbot token interface targets shared active file' test_interface_and_independence
expect 'environment precedence and absent optional fallback are strict' test_precedence_and_absent_optional
expect 'strict parser rejects unsafe content and falls back anonymously' test_strict_parser_and_warning_fallback
expect 'stateless GitHub App installation tokens round-trip through strict storage' test_stateless_installation_token_round_trip
expect 'private atomic storage, replacement, symlink rejection, and removal work' test_private_atomic_storage_and_removal
expect 'fingerprint never returns the full token' test_fingerprint_is_safe
expect 'canary is absent from output and harness logs' test_canary_absent_from_output_and_harness_logs

test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d token test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
