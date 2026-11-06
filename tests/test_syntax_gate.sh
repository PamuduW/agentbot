#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTBOT_TEST_RUNNER_SOURCE_ONLY=1 source "$ROOT/tests/run.sh"
fixture="$(mktemp -d)"
trap 'rm -rf -- "$fixture"' EXIT
printf 'echo valid\n' >"$fixture/first.sh"
printf 'if then\n' >"$fixture/broken second.sh"
if bash_syntax "$fixture/first.sh" "$fixture/broken second.sh" >"$fixture/output" 2>&1; then
	printf 'not ok - syntax gate accepted an invalid second script\n' >&2
	exit 1
fi
grep -Fq 'broken second.sh' "$fixture/output"
printf 'echo valid\n' >"$fixture/broken second.sh"
bash_syntax "$fixture/first.sh" "$fixture/broken second.sh"
printf 'ok - syntax gate checks every script and handles spaces\n'
