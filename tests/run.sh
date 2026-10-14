#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prefer the repository virtualenv so a local run uses the same Ruff and
# coverage that CI installs. Without this the guards below silently skip both
# and still report success.
if [[ -d "$ROOT/.venv/bin" ]]; then
	PATH="$ROOT/.venv/bin:$PATH"
	export PATH
fi

# A missing check is a failure, not a skip. Set AGENTBOT_ALLOW_SKIP=1 to
# downgrade it on a machine that deliberately lacks the tool.
require_tool() {
	local tool="$1" hint="$2" install="${3:-python3 -m pip install -r requirements-dev.txt}"
	command -v "$tool" >/dev/null 2>&1 && return 0
	if [[ "${AGENTBOT_ALLOW_SKIP:-0}" == 1 ]]; then
		printf '\n==> %s (skipped: %s is not installed)\n' "$hint" "$tool"
		return 1
	fi
	printf '\n==> %s: %s is not installed.\n' "$hint" "$tool" >&2
	printf '    Install it (%s) or rerun with AGENTBOT_ALLOW_SKIP=1.\n' "$install" >&2
	exit 1
}

mapfile -t SHELL_SUITES < <(
	find "$ROOT/tests" -type f -name 'test_*.sh' \
		! -path "$ROOT/tests/lib/*" -print | sort
)

if [[ "${AGENTBOT_TEST_RUNNER_SOURCE_ONLY:-0}" == 1 ]]; then
	return 0
fi

# No test sees the real home. One that forgot to isolate itself would
# otherwise read or write this machine's ~/.config/agentbot, its Git config,
# or the live memory vault named by AGENTBOT_MEMORY_DIR.
TEST_HOME="$(mktemp -d)"
trap 'rm -rf "$TEST_HOME"' EXIT
export HOME="$TEST_HOME/home" XDG_CONFIG_HOME="$TEST_HOME/config"
export XDG_CACHE_HOME="$TEST_HOME/cache" XDG_DATA_HOME="$TEST_HOME/data"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
unset AGENTBOT_MEMORY_DIR AGENTBOT_MEMORY_ROOT GIT_CONFIG_GLOBAL

run_check() {
	local label="$1" started=$SECONDS elapsed
	shift
	printf '\n==> %s\n' "$label"
	"$@"
	elapsed=$((SECONDS - started))
	printf '<== %s (%ss)\n' "$label" "$elapsed"
}

mapfile -t shell_files < <(
	find "$ROOT" \
		-path "$ROOT/.git" -prune -o \
		-type f -name '*.sh' -print | sort
)
production_shell_files=()
for shell_file in "${shell_files[@]}"; do
	[[ "$shell_file" == "$ROOT/tests/"* ]] || production_shell_files+=("$shell_file")
done

cd "$ROOT"
total_started=$SECONDS
if require_tool ruff "Ruff"; then
	run_check "Ruff" ruff check src tests
fi
if require_tool mypy "Types"; then
	run_check "Types" mypy
fi
# The Python suite in parallel shards, balanced by file size. unittest has no
# parallel mode of its own; run serially under coverage it was three quarters
# of the gate. -b keeps a passing test's output out of the log; a failing
# shard's full output is printed. AGENTBOT_SLOW_TESTS=1 adds the slow lane.
python_unit_tests() {
	local -a runner=("$@") shards=() pids=() modules=()
	local jobs shard_dir module slot index=0 status=0
	jobs="${AGENTBOT_TEST_JOBS:-$(nproc 2>/dev/null || echo 2)}"
	((jobs > 4)) && jobs=4
	mapfile -t modules < <(
		find tests -name 'test_*.py' -printf '%s %p\n' | sort -rn | awk '{print $2}' |
			sed -e 's|\.py$||' -e 's|/|.|g'
	)
	shard_dir="$(mktemp -d)"
	for ((index = 0; index < jobs; index++)); do shards[index]=''; done
	index=0
	# Snake order (0..n-1, then n-1..0) keeps the largest files apart.
	for module in "${modules[@]}"; do
		slot=$((index % (2 * jobs)))
		((slot >= jobs)) && slot=$((2 * jobs - 1 - slot))
		shards[slot]+="$module "
		index=$((index + 1))
	done
	for ((index = 0; index < jobs; index++)); do
		# shellcheck disable=SC2086 # The shard is a word list of module names.
		"${runner[@]}" -m unittest -b ${shards[index]} >"$shard_dir/$index.log" 2>&1 &
		pids[index]=$!
	done
	for ((index = 0; index < jobs; index++)); do
		if ! wait "${pids[index]}"; then
			status=1
			printf '\n--- shard %s failed ---\n' "$index"
			cat "$shard_dir/$index.log"
		fi
	done
	grep -h -E '^(Ran |OK|FAILED)' "$shard_dir"/*.log | sed 's/^/  /'
	rm -rf "$shard_dir"
	return "$status"
}

if require_tool coverage "Coverage"; then
	coverage erase
	run_check "Python unit tests with coverage" python_unit_tests coverage run -p
	coverage combine -q
	run_check "Coverage" coverage report
else
	run_check "Python unit tests" python_unit_tests python3
fi
for suite in "${SHELL_SUITES[@]}"; do
	run_check "$(basename "$suite")" bash "$suite"
done
run_check "Bash syntax" bash -n "${shell_files[@]}"
if require_tool shellcheck "ShellCheck"; then
	run_check "ShellCheck" shellcheck "${production_shell_files[@]}"
fi
if require_tool shfmt "Formatting"; then
	run_check "Formatting" shfmt -d "${shell_files[@]}"
fi
if require_tool zizmor "Workflow security" "Dotfiles' zizmor component"; then
	run_check "Workflow security" zizmor --offline --quiet .github/workflows
fi
run_check "Shared contract" bash "$ROOT/tests/check_shared_contract.sh"
run_check "Whitespace errors" git diff --check

printf '\nAll Agentbot checks passed in %ss.\n' "$((SECONDS - total_started))"
