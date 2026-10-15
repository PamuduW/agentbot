#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/lib/harness.sh
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"
AGENTBOT_MENU_SOURCE_ONLY=1 source "$ROOT/scripts/menu.sh"
AGENTBOT_HOME="$ROOT"
export AGENTBOT_HOME
# shellcheck source=scripts/lib/python_env.sh
source "$ROOT/scripts/lib/python_env.sh"
agentbot_python_resolve
test_harness_report_init

test_atlassian_menu_saves_without_showing_secrets() (
	export XDG_CONFIG_HOME="$TEST_ROOT/atlassian-save"
	local input="$TEST_ROOT/atlassian.input" output="$TEST_ROOT/atlassian.output"
	printf '%s\n' s reader@example.test 11111111-2222-3333-4444-555555555555 \
		jira-test-read-token confluence-test-read-token y q >"$input"
	AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" \
		AGENTBOT_TOKEN_TTY_COLS=80 agentbot_atlassian_token_menu || return 1
	[[ -f "$XDG_CONFIG_HOME/agentbot/atlassian.json" ]] || return 1
	! rg -q 'jira-test-read-token|confluence-test-read-token' "$output" || return 1
	[[ "$(stat -c %a "$XDG_CONFIG_HOME/agentbot/atlassian.json")" == 600 ]] || return 1
	grep -q 'Atlassian settings saved' "$output"
)

test_atlassian_menu_cancel_does_not_save() (
	export XDG_CONFIG_HOME="$TEST_ROOT/atlassian-cancel"
	local input="$TEST_ROOT/atlassian-cancel.input" output="$TEST_ROOT/atlassian-cancel.output"
	printf '%s\n' s reader@example.test 11111111-2222-3333-4444-555555555555 \
		jira-test-read-token confluence-test-read-token n q >"$input"
	AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" \
		AGENTBOT_TOKEN_TTY_COLS=80 agentbot_atlassian_token_menu || return 1
	[[ ! -e "$XDG_CONFIG_HOME/agentbot/atlassian.json" ]]
)

test_atlassian_menu_removes_saved_credentials() (
	export XDG_CONFIG_HOME="$TEST_ROOT/atlassian-remove"
	local input="$TEST_ROOT/atlassian-remove.input" output="$TEST_ROOT/atlassian-remove.output"
	printf '%s\n' s reader@example.test 11111111-2222-3333-4444-555555555555 \
		jira-test-read-token confluence-test-read-token y d y q >"$input"
	AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" \
		AGENTBOT_TOKEN_TTY_COLS=80 agentbot_atlassian_token_menu || return 1
	[[ ! -e "$XDG_CONFIG_HOME/agentbot/atlassian.json" ]] || return 1
	grep -q 'Saved Atlassian settings removed' "$output"
)

test_atlassian_menu_protects_secrets_when_shell_tracing_is_enabled() (
	export XDG_CONFIG_HOME="$TEST_ROOT/atlassian-trace"
	local input="$TEST_ROOT/atlassian-trace.input" output="$TEST_ROOT/atlassian-trace.output"
	local trace="$TEST_ROOT/atlassian-trace.log" rc=0 restored=0
	printf '%s\n' s reader@example.test 11111111-2222-3333-4444-555555555555 \
		jira-test-read-token confluence-test-read-token y q >"$input"
	{
		set -x
		AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" \
			AGENTBOT_TOKEN_TTY_COLS=80 agentbot_atlassian_token_menu || rc=$?
		[[ "$-" != *x* ]] || restored=1
		set +x
	} 2>"$trace"
	[[ "$rc" == 0 && "$restored" == 1 ]] || return 1
	! rg -q 'jira-test-read-token|confluence-test-read-token' "$trace" "$output"
)

check 'Atlassian menu protects secret input from shell tracing' test_atlassian_menu_protects_secrets_when_shell_tracing_is_enabled
check 'Atlassian menu saves private credentials without displaying tokens' test_atlassian_menu_saves_without_showing_secrets
check 'Atlassian menu cancellation writes nothing' test_atlassian_menu_cancel_does_not_save
check 'Atlassian menu removes credentials after confirmation' test_atlassian_menu_removes_saved_credentials
test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d Atlassian-menu test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
