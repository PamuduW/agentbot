#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT
cat >"$TEST_ROOT/python-template" <<'PYTHON'
#!/usr/bin/env bash
set -euo pipefail
prefix="${0%/bin/python}"
case "$*" in
'-m pip install '*)
    [[ "$*" == *'--require-hashes'* && "$*" == *'--only-binary=:all:'* ]] || exit 96
    printf 'install\n' >>"$TEST_ENV_LOG"
    printf 'candidate\n' >"$prefix/marker"
    [[ "${TEST_ENV_PIP_FAIL:-0}" != 1 ]] || exit 27
    ;;
'-m pip check') [[ "${TEST_ENV_CHECK_FAIL:-0}" != 1 ]] || exit 28 ;;
*) cat "$prefix/marker" ;;
esac
PYTHON
setup_case() {
	AGENTBOT_HOME="$TEST_ROOT/$1"
	export AGENTBOT_HOME
	mkdir -p "$AGENTBOT_HOME"
	printf 'fixture requirement\n' >"$AGENTBOT_HOME/requirements.txt"
	printf '# frozen fixture closure\n' >"$AGENTBOT_HOME/requirements.lock"
	TEST_ENV_LOG="$AGENTBOT_HOME/pip.log"
	export TEST_ENV_LOG
	: >"$TEST_ENV_LOG"
	unset AGENTBOT_PYTHON AGENTBOT_PYTHON_EXPLICIT
	source "$ROOT/scripts/lib/python_env.sh"
	# Mock only the catalog boundary and Python processes. Publication, paths,
	# old data, receipts, failures and lock contention use real filesystem work.
	agentbot_python_catalog_lock() {
		AGENTBOT_REQUIREMENTS_LOCK="$AGENTBOT_HOME/requirements.lock"
		SOFTWARE_CATALOG_DIGEST="$(printf '%064d' 0)"
	}
	python3() {
		[[ "$#" == 3 && "$1" == -m && "$2" == venv ]] || return 97
		mkdir -p "$3/bin"
		cp "$TEST_ROOT/python-template" "$3/bin/python"
		chmod +x "$3/bin/python"
		printf 'new\n' >"$3/marker"
	}
}
seed_owned() {
	mkdir -p "$AGENTBOT_HOME/.venv/bin"
	cp "$TEST_ROOT/python-template" "$AGENTBOT_HOME/.venv/bin/python"
	chmod +x "$AGENTBOT_HOME/.venv/bin/python"
	printf 'working-old\n' >"$AGENTBOT_HOME/.venv/marker"
	printf '%064d\n' 0 >"$(agentbot_python_stamp_file)"
}
for failure in pip check; do
	(
		setup_case "failed-$failure"
		seed_owned
		if [[ "$failure" == pip ]]; then export TEST_ENV_PIP_FAIL=1; else export TEST_ENV_CHECK_FAIL=1; fi
		rc=0
		agentbot_python_ensure >/dev/null 2>&1 || rc=$?
		expected=27
		[[ "$failure" == check ]] && expected=28
		[[ "$rc" == "$expected" && "$(wc -l <"$TEST_ENV_LOG")" == 1 && "$(cat "$AGENTBOT_HOME/.venv/marker")" == working-old ]] || {
			printf 'FAIL: %s failure changed the working environment\n' "$failure" >&2
			exit 1
		}
		[[ "$(cat "$(agentbot_python_stamp_file)")" == "$(printf '%064d' 0)" ]]
	)
done
(
	setup_case publication
	seed_owned
	mv() {
		if [[ "${*: -1}" == "$AGENTBOT_HOME/.venv" && -L "${*: -2:1}" ]]; then return 29; fi
		command mv "$@"
	}
	rc=0
	agentbot_python_ensure >/dev/null 2>&1 || rc=$?
	[[ "$rc" == 29 && "$(wc -l <"$TEST_ENV_LOG")" == 1 && "$(cat "$AGENTBOT_HOME/.venv/marker")" == working-old ]]
)
(
	setup_case migration
	seed_owned
	agentbot_python_ensure
	[[ -L "$AGENTBOT_HOME/.venv" && -f "$(agentbot_python_stamp_file)" ]]
	mapfile -t previous < <(find "$AGENTBOT_HOME/.venv-releases" -maxdepth 1 -type d -name 'previous-*')
	[[ "${#previous[@]}" == 1 && "$(cat "${previous[0]}/marker")" == working-old ]]
)
(
	setup_case escaping
	seed_owned
	mv "$AGENTBOT_HOME/.venv" "$AGENTBOT_HOME/external"
	ln -s "$AGENTBOT_HOME/external" "$AGENTBOT_HOME/.venv"
	rc=0
	agentbot_python_ensure >/dev/null 2>&1 || rc=$?
	[[ "$rc" == 2 && ! -s "$TEST_ENV_LOG" && "$(cat "$AGENTBOT_HOME/external/marker")" == working-old ]]
)
(
	setup_case fresh
	agentbot_python_ensure
	[[ -L "$AGENTBOT_HOME/.venv" && "$(cat "$AGENTBOT_HOME/.venv/marker")" == candidate ]]
	first="$(realpath "$AGENTBOT_HOME/.venv")"
	[[ "$AGENTBOT_PYTHON" == "$first/bin/python" ]]
	agentbot_python_ensure
	[[ "$(wc -l <"$TEST_ENV_LOG")" == 1 ]]
	printf '# changed frozen closure\n' >>"$AGENTBOT_HOME/requirements.lock"
	agentbot_python_ensure
	[[ "$(wc -l <"$TEST_ENV_LOG")" == 2 ]]
	[[ "$(realpath "$AGENTBOT_HOME/.venv")" != "$first" && -x "$first/bin/python" ]]
)
(
	setup_case external
	seed_owned
	rm "$(agentbot_python_stamp_file)"
	rc=0
	agentbot_python_ensure >/dev/null 2>&1 || rc=$?
	[[ "$rc" != 0 && "$(cat "$AGENTBOT_HOME/.venv/marker")" == working-old && ! -s "$TEST_ENV_LOG" ]]
)
(
	setup_case concurrent
	seed_owned
	mkdir -p "$AGENTBOT_HOME/.venv-releases"
	exec {held_lock}>"$AGENTBOT_HOME/.venv-releases/install.lock"
	flock -n "$held_lock"
	rc=0
	agentbot_python_ensure >/dev/null 2>&1 || rc=$?
	[[ "$rc" == 75 && ! -s "$TEST_ENV_LOG" ]]
	exec {held_lock}>&-
)
printf 'PASS: managed Python environment publication preserves old releases through install/check/publication failure\n'
