#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HARNESS_LIB="${ROOT}/tests/lib/harness.sh"

# shellcheck source=tests/lib/harness.sh
source "$HARNESS_LIB"

test_harness_report_init

# These assertions report their own pass/fail with a diagnostic, unlike the
# shared status-returning assert_eq. Named apart so neither shadows the other.
assert_eq_reported() {
	local expected="$1"
	local actual="$2"
	local label="$3"
	if [[ "$actual" == "$expected" ]]; then
		pass "$label"
	else
		fail "${label} (expected ${expected@Q}, got ${actual@Q})"
	fi
}

assert_file_lacks() {
	local needle="$1"
	local path="$2"
	local label="$3"
	if [[ ! -e "$path" ]] || ! grep -Fq -- "$needle" "$path"; then
		pass "$label"
	else
		fail "${label} (${needle@Q} found in ${path})"
	fi
}

assert_text_lacks() {
	local needle="$1"
	local value="$2"
	local label="$3"
	if [[ "$value" != *"$needle"* ]]; then
		pass "$label"
	else
		fail "${label} (captured text contains canary)"
	fi
}

test_runner_lists_each_shell_suite_once() (
	local runner="$ROOT/tests/run.sh"
	[[ -f "$runner" ]] || return 1

	AGENTBOT_TEST_RUNNER_SOURCE_ONLY=1 source "$runner"

	local suite path
	local -A actual=() expected=()
	for suite in "${SHELL_SUITES[@]}"; do
		actual["$suite"]=$((${actual["$suite"]:-0} + 1))
	done
	while IFS= read -r path; do
		expected["$path"]=1
	done < <(
		find "$ROOT/tests" -type f -name 'test_*.sh' \
			! -path "$ROOT/tests/lib/*" -print | sort
	)

	[[ "${#actual[@]}" -eq "${#expected[@]}" ]] || return 1
	for path in "${!expected[@]}"; do
		[[ "${actual["$path"]:-0}" -eq 1 ]] || return 1
	done
)

test_ci_installs_full_gate_system_tools() (
	local install_step tool
	install_step="$(grep 'apt-get install' "$ROOT/.github/workflows/test.yml")"
	for tool in shellcheck shfmt ripgrep; do
		[[ " $install_step " == *" $tool "* ]] || return 1
	done
)

# A test that does not run is worse than no test: it reports "ok" for work
# nobody checked. Two ways that happens here, one of which already had.
#
# In Python, a second `class ReconciliationReportTests` shadowed the first, so
# three tests written for a fix never ran and reverting the fix still passed.
# unittest sees one class; the file contains two.
test_no_python_test_is_shadowed() (
	python3 - "$ROOT" <<'PY'
import ast
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
problems = []
for path in sorted((root / "tests").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top = Counter(
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    )
    problems += [
        f"{path.name}: {name} defined {count}x" for name, count in top.items() if count > 1
    ]
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = Counter(
            member.name
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        problems += [
            f"{path.name}: {node.name}.{name} defined {count}x"
            for name, count in methods.items()
            if count > 1
        ]
for problem in problems:
    print(problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
)

# And in Bash, a `test_*` function that no line ever calls. These suites list
# their cases by hand at the bottom of the file, so adding a function and
# forgetting the line is one keystroke away.
test_every_shell_test_is_invoked() (
	local suite joined name orphans=''
	while IFS= read -r suite; do
		# Registration lines wrap, so continuations are joined before matching.
		joined="$(sed -e :a -e '/\\$/N; s/\\\n//; ta' "$suite")"
		while IFS= read -r name; do
			[[ -n "$name" ]] || continue
			grep -qE "(check|expect|expect_success|run_case|run_test|if)[[:space:]].*\b${name}\b" \
				<<<"$joined" || orphans+=" $(basename "$suite"):${name}"
		done < <(grep -oE '^test_[a-zA-Z0-9_]+\(\)' "$suite" | tr -d '()')
	done < <(find "$ROOT/tests" -type f -name 'test_*.sh' ! -path "$ROOT/tests/lib/*" | sort)
	[[ -z "$orphans" ]] || {
		printf '   shell tests defined but never invoked:%s\n' "$orphans" >&2
		return 1
	}
)

test_harness_setup "$ROOT"

if test_no_python_test_is_shadowed; then
	pass "no Python test is shadowed by a duplicate definition"
else
	fail "no Python test is shadowed by a duplicate definition"
fi

if test_every_shell_test_is_invoked; then
	pass "every shell test function is invoked by its suite"
else
	fail "every shell test function is invoked by its suite"
fi

if test_runner_lists_each_shell_suite_once; then
	pass "full runner lists every shell suite exactly once"
else
	fail "full runner lists every shell suite exactly once"
fi

if test_ci_installs_full_gate_system_tools; then
	pass "CI installs every system tool required by the full gate"
else
	fail "CI installs every system tool required by the full gate"
fi

[[ "$HOME" == "$TEST_ROOT"/* ]] && pass "HOME is isolated" || fail "HOME is isolated"
[[ "$XDG_CONFIG_HOME" == "$TEST_ROOT"/* ]] && pass "XDG_CONFIG_HOME is isolated" || fail "XDG_CONFIG_HOME is isolated"
[[ "$TEST_COMMAND_LOG" == "$TEST_ROOT"/* ]] && pass "command log is isolated" || fail "command log is isolated"
[[ "$TEST_URL_LOG" == "$TEST_ROOT"/* ]] && pass "URL log is isolated" || fail "URL log is isolated"

FAKE_GIT_STDOUT="fake-main"
git_output="$(git status --short --branch)"
assert_eq_reported "fake-main" "$git_output" "fake git output is configurable"
grep -Fq $'git\tstatus\t--short\t--branch' "$TEST_COMMAND_LOG" &&
	pass "fake git intercepts and logs argv" ||
	fail "fake git intercepts and logs argv"

FAKE_NPX_STDOUT="fake-npx"
npx_output="$(npx skills check)"
assert_eq_reported "fake-npx" "$npx_output" "fake npx output is configurable"

FAKE_CURL_EXIT=23
set +e
curl_error="$(curl -fsS https://example.invalid 2>&1)"
curl_rc=$?
set -e
assert_eq_reported "23" "$curl_rc" "fake curl propagates a configured nonzero status"
assert_text_lacks "Could not resolve" "$curl_error" "fake curl never reaches the network"

export TEST_CANARY_SECRET="agentbot-canary-secret-4871"
FAKE_CURL_EXIT=7
FAKE_CURL_STDOUT="response:${TEST_CANARY_SECRET}"
FAKE_CURL_STDERR="failure:${TEST_CANARY_SECRET}"
set +e
secret_capture="$(curl -H "Authorization: Bearer test-placeholder" \
	"https://example.invalid/public" 2>&1)"
secret_rc=$?
set -e
_harness_append_sanitized "$TEST_COMMAND_LOG" synthetic-argv "$TEST_CANARY_SECRET"
_harness_append_sanitized "$TEST_URL_LOG" "https://example.invalid/?token=${TEST_CANARY_SECRET}"
assert_eq_reported "7" "$secret_rc" "fake curl retains status while redacting secrets"
assert_text_lacks "$TEST_CANARY_SECRET" "$secret_capture" "canary is absent from captured stdout and stderr"
assert_file_lacks "$TEST_CANARY_SECRET" "$TEST_COMMAND_LOG" "canary is absent from argv logs"
assert_file_lacks "$TEST_CANARY_SECRET" "$TEST_URL_LOG" "canary is absent from URL logs"

export HARNESS_RELAUNCH_EXIT=19
set +e
harness_relaunch "$TEST_ROOT/bin/agentbot" update --dry-run
relaunch_rc=$?
set -e
assert_eq_reported "19" "$relaunch_rc" "injected relaunch propagates status"
assert_relaunch_call "$TEST_ROOT/bin/agentbot" update --dry-run &&
	pass "injected relaunch records argv" ||
	fail "injected relaunch records argv"

[[ ! -e "$TEST_FAKE_BIN/exec" ]] &&
	pass "harness never places a fake exec on PATH" ||
	fail "harness never places a fake exec on PATH"

harness_assert_path_allowed "$HOME/state/file" &&
	pass "write guard allows isolated-home paths" ||
	fail "write guard allows isolated-home paths"
if harness_assert_path_allowed "$ROOT/install.sh" >/dev/null 2>&1; then
	fail "write guard rejects repository paths"
else
	pass "write guard rejects repository paths"
fi
if harness_assert_path_allowed "/tmp/outside-agentbot-harness" >/dev/null 2>&1; then
	fail "write guard rejects paths outside the temp root"
else
	pass "write guard rejects paths outside the temp root"
fi
if harness_assert_path_allowed "$(command -v sh)" >/dev/null 2>&1; then
	fail "write guard rejects absolute external binary candidates"
else
	pass "write guard rejects absolute external binary candidates"
fi

declare -F _harness_exit_teardown >/dev/null &&
	pass "harness provides an explicit EXIT teardown wrapper" ||
	fail "harness provides an explicit EXIT teardown wrapper"

set +e
trap_failure_root="$(bash -c '
  set -euo pipefail
  source "$1"
  test_harness_setup "$2"
  export HARNESS_FORCE_VERIFY_FAILURE=1
  printf "%s" "$TEST_ROOT"
  exit 0
' _ "$HARNESS_LIB" "$ROOT" 2>/dev/null)"
trap_failure_rc=$?
set -e
[[ "$trap_failure_rc" -ne 0 ]] &&
	pass "EXIT teardown turns verification failure into process failure" ||
	fail "EXIT teardown turns verification failure into process failure"
[[ -n "$trap_failure_root" && ! -e "$trap_failure_root" ]] &&
	pass "EXIT teardown removes the root after verification failure" ||
	fail "EXIT teardown removes the root after verification failure"

set +e
earlier_failure_root="$(bash -c '
  set -euo pipefail
  source "$1"
  test_harness_setup "$2"
  export HARNESS_FORCE_VERIFY_FAILURE=1
  printf "%s" "$TEST_ROOT"
  exit 41
' _ "$HARNESS_LIB" "$ROOT" 2>/dev/null)"
earlier_failure_rc=$?
set -e
assert_eq_reported "41" "$earlier_failure_rc" "EXIT teardown preserves an earlier nonzero status"
[[ -n "$earlier_failure_root" && ! -e "$earlier_failure_root" ]] &&
	pass "EXIT teardown cleans after an earlier nonzero status" ||
	fail "EXIT teardown cleans after an earlier nonzero status"

cleanup_failure_result="$(bash -c '
  set -euo pipefail
  source "$1"
  original_home=$HOME
  test_harness_setup "$2"
  root=$TEST_ROOT
  export HARNESS_FORCE_VERIFY_FAILURE=1
  set +e
  test_harness_cleanup >/dev/null 2>&1
  cleanup_rc=$?
  set -e
  restored=false
  removed=false
  [[ "$HOME" == "$original_home" ]] && restored=true
  [[ ! -e "$root" ]] && removed=true
  unset HARNESS_FORCE_VERIFY_FAILURE
  test_harness_cleanup >/dev/null 2>&1 || true
  printf "%s|%s|%s\n" "$cleanup_rc" "$restored" "$removed"
' _ "$HARNESS_LIB" "$ROOT")"
IFS='|' read -r cleanup_failure_rc cleanup_restored cleanup_removed <<<"$cleanup_failure_result"
[[ "$cleanup_failure_rc" -ne 0 ]] &&
	pass "cleanup preserves a nonzero verification result" ||
	fail "cleanup preserves a nonzero verification result"
assert_eq_reported "true" "$cleanup_restored" "cleanup restores environment after verification failure"
assert_eq_reported "true" "$cleanup_removed" "cleanup removes its temp root after verification failure"

init_failure_result="$(bash -c '
  set -euo pipefail
  source "$1"
  export HARNESS_FAIL_INIT_AFTER_MKTEMP=1
  set +e
  test_harness_setup "$2" >/dev/null 2>&1
  setup_rc=$?
  set -e
  root=${TEST_ROOT:-}
  removed=false
  [[ -n "$root" && ! -e "$root" ]] && removed=true
  if [[ -n "$root" && -e "$root" ]]; then
    unset HARNESS_FAIL_INIT_AFTER_MKTEMP
    test_harness_cleanup >/dev/null 2>&1 || true
  fi
  printf "%s|%s\n" "$setup_rc" "$removed"
' _ "$HARNESS_LIB" "$ROOT")"
IFS='|' read -r init_failure_rc init_failure_removed <<<"$init_failure_result"
[[ "$init_failure_rc" -ne 0 ]] &&
	pass "injected initialization failure returns nonzero" ||
	fail "injected initialization failure returns nonzero"
assert_eq_reported "true" "$init_failure_removed" "initialization failure removes the temp root"

cleanup_root="$(bash -c '
  set -euo pipefail
  source "$1"
  test_harness_setup "$2"
  root=$TEST_ROOT
  test_harness_cleanup
  printf "%s" "$root"
' _ "$HARNESS_LIB" "$ROOT")"
[[ ! -e "$cleanup_root" ]] &&
	pass "cleanup removes the unique temp root" ||
	fail "cleanup removes the unique temp root"

test_harness_verify_safety &&
	pass "safety verification detects no repository mutation" ||
	fail "safety verification detects no repository mutation"

printf '\nRan %d harness test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
[[ "$failed" -eq 0 ]]
