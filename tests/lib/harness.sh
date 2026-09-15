#!/usr/bin/env bash

TEST_HARNESS_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
export TEST_HARNESS_LIB

# Reporting and assertions are shared verbatim with the sibling repository.
if [[ -z "${DOTFILES_SHARED_ROOT:-}" ]]; then
	# shellcheck source=scripts/lib/shared_resolve.sh
	source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts/lib" && pwd)/shared_resolve.sh"
	dotfiles_shared_require "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || return 1
fi
# shellcheck source=/dev/null
source "$DOTFILES_SHARED_ROOT/tests/lib/shared/assert.sh"

_harness_sanitize() {
	local value="${1-}"
	local secret="${TEST_CANARY_SECRET:-}"

	if [[ -n "$secret" ]]; then
		value="${value//"$secret"/[REDACTED]}"
	fi
	case "$value" in
	[Aa]uthorization:*) value="Authorization: [REDACTED]" ;;
	GITHUB_TOKEN=*) value="GITHUB_TOKEN=[REDACTED]" ;;
	esac
	printf '%s' "$value"
}

_harness_append_sanitized() {
	local destination="$1"
	shift
	local separator=""
	local field

	for field in "$@"; do
		printf '%s%s' "$separator" "$(_harness_sanitize "$field")" >>"$destination"
		separator=$'\t'
	done
	printf '\n' >>"$destination"
}

_harness_emit_sanitized() {
	local value="${1-}"
	local destination="$2"
	[[ -z "$value" ]] && return 0
	printf '%s\n' "$(_harness_sanitize "$value")" >"$destination"
}

harness_fake_dispatch() {
	local command_name="$1"
	shift
	local prefix
	local stdout_name stderr_name exit_name
	local stdout_value stderr_value exit_value
	local argument

	prefix="${command_name^^}"
	stdout_name="FAKE_${prefix}_STDOUT"
	stderr_name="FAKE_${prefix}_STDERR"
	exit_name="FAKE_${prefix}_EXIT"
	stdout_value="${!stdout_name-}"
	stderr_value="${!stderr_name-}"
	exit_value="${!exit_name-0}"

	_harness_append_sanitized "$TEST_COMMAND_LOG" "$command_name" "$@"
	if [[ "$command_name" == "curl" ]]; then
		for argument in "$@"; do
			case "$argument" in
			http://* | https://*) _harness_append_sanitized "$TEST_URL_LOG" "$argument" ;;
			esac
		done
	fi

	_harness_emit_sanitized "$stdout_value" /dev/stdout
	_harness_emit_sanitized "$stderr_value" /dev/stderr
	return "$exit_value"
}

_harness_write_fake_command() {
	local command_name="$1"
	local target="${TEST_FAKE_BIN}/${command_name}"

	printf '%s\n' \
		'#!/usr/bin/env bash' \
		'set -u' \
		'source "${TEST_HARNESS_LIB:?}"' \
		"harness_fake_dispatch ${command_name} \"\$@\"" \
		>"$target"
	chmod 0700 "$target"
}

_harness_protected_files_fingerprint() {
	local path

	while IFS= read -r path; do
		[[ -z "$path" ]] && continue
		if [[ -f "$path" ]]; then
			printf '%s\t' "$path"
			cksum <"$path"
		elif [[ -L "$path" ]]; then
			printf '%s\tsymlink\t%s\n' "$path" "$(readlink "$path")"
		else
			printf '%s\tmissing\n' "$path"
		fi
	done <<<"$TEST_PROTECTED_PATHS"
}

_harness_repository_fingerprint() {
	{
		"$TEST_REAL_GIT" -C "$TEST_REPOSITORY_ROOT" rev-parse HEAD
		"$TEST_REAL_GIT" -C "$TEST_REPOSITORY_ROOT" status --porcelain=v1 --untracked-files=all
		"$TEST_REAL_GIT" -C "$TEST_REPOSITORY_ROOT" diff --no-ext-diff --binary
	} | cksum
}

_harness_exit_teardown() {
	local body_rc=$?
	local cleanup_rc=0
	local final_rc

	# Avoid recursive EXIT handling when the wrapper explicitly terminates with
	# the selected status.
	trap - EXIT
	test_harness_cleanup || cleanup_rc=$?

	if [[ "$body_rc" -ne 0 ]]; then
		final_rc="$body_rc"
	else
		final_rc="$cleanup_rc"
	fi
	exit "$final_rc"
}

test_harness_setup() {
	local repository_root="${1:?repository root is required}"
	local temp_parent="${TMPDIR:-/tmp}"
	local real_curl real_npx

	_HARNESS_OLD_HOME="${HOME-}"
	_HARNESS_OLD_XDG_CONFIG_HOME="${XDG_CONFIG_HOME-}"
	_HARNESS_OLD_TMPDIR="${TMPDIR-}"
	_HARNESS_OLD_PATH="$PATH"
	# A nested harness inherits the outer fake PATH. Preserve the original Git
	# executable so repository-state checks never go through a test double.
	TEST_REAL_GIT="${TEST_REAL_GIT:-$(command -v git)}"
	real_curl="$(command -v curl)"
	real_npx="$(command -v npx)"
	TEST_REPOSITORY_ROOT="$(cd "$repository_root" && pwd)"
	TEST_REPOSITORY_STATUS_BEFORE="$($TEST_REAL_GIT -C "$TEST_REPOSITORY_ROOT" status --porcelain=v1 --untracked-files=all)"
	TEST_PROTECTED_PATHS="${TEST_REAL_GIT}"$'\n'"${real_curl}"$'\n'"${real_npx}"
	TEST_PROTECTED_FILES_BEFORE="$(_harness_protected_files_fingerprint)"
	TEST_REPOSITORY_FINGERPRINT_BEFORE="$(_harness_repository_fingerprint)"

	TEST_ROOT="$(mktemp -d "${temp_parent%/}/agentbot-harness.XXXXXX")"
	: >"${TEST_ROOT}/.agentbot-test-root"
	_HARNESS_ACTIVE=0
	_HARNESS_CLEANED=0
	trap _harness_exit_teardown EXIT
	if [[ "${HARNESS_FAIL_INIT_AFTER_MKTEMP:-0}" == "1" ]]; then
		test_harness_cleanup
		return 97
	fi

	TEST_HOME="${TEST_ROOT}/home"
	TEST_XDG_CONFIG_HOME="${TEST_ROOT}/xdg-config"
	TEST_FAKE_BIN="${TEST_ROOT}/fake-bin"
	TEST_LOG_DIR="${TEST_ROOT}/logs"
	TEST_COMMAND_LOG="${TEST_LOG_DIR}/commands.log"
	TEST_URL_LOG="${TEST_LOG_DIR}/urls.log"
	TEST_SIBLING_LOG="${TEST_LOG_DIR}/sibling.log"
	TEST_RELAUNCH_LOG="${TEST_LOG_DIR}/relaunch.log"

	mkdir -p \
		"$TEST_HOME" \
		"$TEST_XDG_CONFIG_HOME" \
		"$TEST_FAKE_BIN" \
		"$TEST_LOG_DIR"
	: >"$TEST_COMMAND_LOG"
	: >"$TEST_URL_LOG"
	: >"$TEST_SIBLING_LOG"
	: >"$TEST_RELAUNCH_LOG"

	HOME="$TEST_HOME"
	XDG_CONFIG_HOME="$TEST_XDG_CONFIG_HOME"
	TMPDIR="${TEST_ROOT}/tmp"
	mkdir -p "$TMPDIR"
	PATH="${TEST_FAKE_BIN}:${_HARNESS_OLD_PATH}"

	export TEST_ROOT TEST_HOME TEST_XDG_CONFIG_HOME TEST_FAKE_BIN TEST_LOG_DIR
	export TEST_COMMAND_LOG TEST_URL_LOG TEST_SIBLING_LOG TEST_RELAUNCH_LOG
	export TEST_REAL_GIT TEST_REPOSITORY_ROOT
	export HOME XDG_CONFIG_HOME TMPDIR PATH
	export FAKE_GIT_STDOUT="" FAKE_GIT_STDERR="" FAKE_GIT_EXIT=0
	export FAKE_CURL_STDOUT="" FAKE_CURL_STDERR="" FAKE_CURL_EXIT=0
	export FAKE_NPX_STDOUT="" FAKE_NPX_STDERR="" FAKE_NPX_EXIT=0
	export HARNESS_RELAUNCH_EXIT=0

	_harness_write_fake_command git
	_harness_write_fake_command curl
	_harness_write_fake_command npx
	_HARNESS_ACTIVE=1
}

harness_relaunch() {
	_harness_append_sanitized \
		"$TEST_RELAUNCH_LOG" \
		relaunch \
		"$@"
	return "${HARNESS_RELAUNCH_EXIT:-0}"
}

assert_relaunch_call() {
	local expected_file="${TEST_ROOT}/expected-relaunch.log"
	local actual
	local expected

	: >"$expected_file"
	_harness_append_sanitized "$expected_file" relaunch "$@"
	actual="$(tail -n 1 "$TEST_RELAUNCH_LOG")"
	expected="$(tail -n 1 "$expected_file")"
	[[ "$actual" == "$expected" ]]
}

harness_assert_path_allowed() {
	local candidate="${1:?path is required}"
	local normalized

	normalized="$(realpath -m -- "$candidate")" || return 1
	case "$normalized" in
	"$TEST_ROOT" | "$TEST_ROOT"/*) return 0 ;;
	*)
		printf 'refusing path outside test root: %s\n' "$normalized" >&2
		return 1
		;;
	esac
}

test_harness_verify_safety() {
	local current_status
	local current_fingerprint
	local protected_fingerprint
	local path

	if [[ "${HARNESS_FORCE_VERIFY_FAILURE:-0}" == "1" ]]; then
		printf 'injected harness verification failure\n' >&2
		return 97
	fi

	for path in "$HOME" "$XDG_CONFIG_HOME" "$TMPDIR" "$TEST_COMMAND_LOG" "$TEST_URL_LOG"; do
		harness_assert_path_allowed "$path" || return 1
	done
	[[ "${PATH%%:*}" == "$TEST_FAKE_BIN" ]] || return 1
	[[ ! -e "$TEST_FAKE_BIN/exec" ]] || return 1

	current_status="$($TEST_REAL_GIT -C "$TEST_REPOSITORY_ROOT" status --porcelain=v1 --untracked-files=all)"
	[[ "$current_status" == "$TEST_REPOSITORY_STATUS_BEFORE" ]] || {
		printf 'repository changed during isolated harness test\n' >&2
		return 1
	}
	current_fingerprint="$(_harness_repository_fingerprint)"
	[[ "$current_fingerprint" == "$TEST_REPOSITORY_FINGERPRINT_BEFORE" ]] || {
		printf 'repository fingerprint changed during isolated harness test\n' >&2
		return 1
	}
	protected_fingerprint="$(_harness_protected_files_fingerprint)"
	[[ "$protected_fingerprint" == "$TEST_PROTECTED_FILES_BEFORE" ]] || {
		printf 'protected executable fingerprint changed during isolated harness test\n' >&2
		return 1
	}

	if [[ -n "${TEST_CANARY_SECRET:-}" ]]; then
		for path in "$TEST_COMMAND_LOG" "$TEST_URL_LOG" "$TEST_SIBLING_LOG" "$TEST_RELAUNCH_LOG"; do
			if grep -Fq -- "$TEST_CANARY_SECRET" "$path"; then
				printf 'canary leaked into %s\n' "$path" >&2
				return 1
			fi
		done
	fi
}

test_harness_cleanup() {
	local cleanup_root="${TEST_ROOT:-}"
	local cleanup_rc=0

	if [[ "${_HARNESS_CLEANED:-0}" == "1" ]]; then
		return 0
	fi
	if [[ "${_HARNESS_ACTIVE:-0}" == "1" ]]; then
		test_harness_verify_safety || cleanup_rc=$?
	fi
	if [[ -n "$cleanup_root" && -f "${cleanup_root}/.agentbot-test-root" ]]; then
		rm -rf -- "$cleanup_root" || cleanup_rc=$?
	elif [[ -n "$cleanup_root" ]]; then
		printf 'refusing to clean unmarked test root: %s\n' "$cleanup_root" >&2
		cleanup_rc=1
	fi

	HOME="${_HARNESS_OLD_HOME-}"
	XDG_CONFIG_HOME="${_HARNESS_OLD_XDG_CONFIG_HOME-}"
	TMPDIR="${_HARNESS_OLD_TMPDIR-}"
	PATH="${_HARNESS_OLD_PATH-}"
	export HOME XDG_CONFIG_HOME TMPDIR PATH
	_HARNESS_ACTIVE=0
	_HARNESS_CLEANED=1
	return "$cleanup_rc"
}
