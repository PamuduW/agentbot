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
	local tool="$1" hint="$2"
	command -v "$tool" >/dev/null 2>&1 && return 0
	if [[ "${AGENTBOT_ALLOW_SKIP:-0}" == 1 ]]; then
		printf '\n==> %s (skipped: %s is not installed)\n' "$hint" "$tool"
		return 1
	fi
	printf '\n==> %s: %s is not installed.\n' "$hint" "$tool" >&2
	printf '    Install it (%s) or rerun with AGENTBOT_ALLOW_SKIP=1.\n' \
		"python3 -m pip install -r requirements-dev.txt" >&2
	exit 1
}

mapfile -t SHELL_SUITES < <(
	find "$ROOT/tests" -type f -name 'test_*.sh' \
		! -path "$ROOT/tests/lib/*" -print | sort
)

if [[ "${AGENTBOT_TEST_RUNNER_SOURCE_ONLY:-0}" == 1 ]]; then
	return 0
fi

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
if require_tool coverage "Coverage"; then
	coverage erase
	run_check "Python unit tests with coverage" coverage run -m unittest discover -s tests
	run_check "Coverage" coverage report
else
	run_check "Python unit tests" python3 -m unittest discover -s tests
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
run_check "Shared contract" bash "$ROOT/tests/check_shared_contract.sh"
run_check "Whitespace errors" git diff --check

printf '\nAll Agentbot checks passed in %ss.\n' "$((SECONDS - total_started))"
