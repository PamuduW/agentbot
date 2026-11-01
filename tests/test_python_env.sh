#!/usr/bin/env bash
# shellcheck shell=bash
set -uo pipefail

# Agentbot resolves and provisions its own Python interpreter.
#
# The behaviour this covers was added because a checkout could fast-forward
# onto code importing tomlkit while every call site ran a bare `python3` --
# the system interpreter, which PEP 668 forbids installing into. The failure
# was a traceback out of whichever command imported the module first, and it
# took a whole install down from the diagnostics step.
#
# So the cases below are the ones that regression matters for: resolution
# order, an override that opts out of provisioning, a fill that happens once,
# and a read-only command that reports rather than builds.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/shared_resolve.sh
source "$ROOT/scripts/lib/shared_resolve.sh"
dotfiles_shared_require "$ROOT" || exit 1
passed=0
failed=0

check() {
	local label="$1"
	shift
	if "$@"; then
		printf 'ok - %s\n' "$label"
		passed=$((passed + 1))
	else
		printf 'not ok - %s\n' "$label"
		failed=$((failed + 1))
	fi
}

TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

# A checkout stand-in: the library only reads AGENTBOT_HOME, requirements.txt
# and .venv, so a real export is unnecessary and would cost a pip run per case.
make_home() {
	local home="$TEST_ROOT/$1"
	mkdir -p "$home"
	printf 'PyYAML>=6.0.1,<7\n' >"$home/requirements.txt"
	printf '%s\n' "$home"
}

make_fake_interpreter() {
	local path="$1"
	mkdir -p -- "$(dirname -- "$path")"
	printf '#!/usr/bin/env bash\nexit 0\n' >"$path"
	chmod 700 -- "$path"
	printf '%s\n' "$path"
}

# Each case runs in a subshell: the library exports AGENTBOT_PYTHON, and a
# resolution leaking into the next case would make the order of these tests
# decide their outcome.
resolve_in() {
	local home="$1" override="${2:-}"
	(
		AGENTBOT_HOME="$home"
		export AGENTBOT_HOME
		if [[ -n "$override" ]]; then
			AGENTBOT_PYTHON="$override"
			export AGENTBOT_PYTHON
		else unset AGENTBOT_PYTHON; fi
		# shellcheck source=/dev/null
		source "$ROOT/scripts/lib/python_env.sh"
		agentbot_python_resolve || exit 1
		printf '%s\n' "$AGENTBOT_PYTHON"
	)
}

test_falls_back_to_path_python3() {
	local home resolved
	home="$(make_home fallback)"
	resolved="$(resolve_in "$home")" || return 1
	[[ "$resolved" == "$(command -v python3)" ]]
}

test_prefers_the_checkout_venv() {
	local home resolved
	home="$(make_home venv)"
	make_fake_interpreter "$home/.venv/bin/python" >/dev/null
	resolved="$(resolve_in "$home")" || return 1
	[[ "$resolved" == "$home/.venv/bin/python" ]]
}

test_override_outranks_the_venv() {
	local home override resolved
	home="$(make_home override)"
	make_fake_interpreter "$home/.venv/bin/python" >/dev/null
	override="$(make_fake_interpreter "$TEST_ROOT/mine/python")"
	resolved="$(resolve_in "$home" "$override")" || return 1
	[[ "$resolved" == "$override" ]]
}

# An unset-but-present variable must not win: the override is a promise that
# the interpreter exists, and honouring a dead path would resolve to something
# that cannot run.
test_unusable_override_is_ignored() {
	local home resolved
	home="$(make_home deadoverride)"
	resolved="$(resolve_in "$home" "$TEST_ROOT/absent/python")" || return 1
	[[ "$resolved" == "$(command -v python3)" ]]
}

test_override_skips_provisioning() {
	local home override
	home="$(make_home nobuild)"
	override="$(make_fake_interpreter "$TEST_ROOT/nobuild-bin/python")"
	(
		AGENTBOT_HOME="$home" AGENTBOT_PYTHON="$override"
		export AGENTBOT_HOME AGENTBOT_PYTHON
		# shellcheck source=/dev/null
		source "$ROOT/scripts/lib/python_env.sh"
		agentbot_python_ensure
	) || return 1
	[[ ! -e "$home/.venv" ]]
}

# The fill is stamped with the requirements digest, so an unchanged file must
# not reinstall and a changed one must. Asserted against the stamp rather than
# a pip run: the point is the decision, and a network install per case would
# make this suite unrunnable offline.
test_stamp_tracks_requirements() {
	local home first second
	home="$(make_home stamp)"
	first="$(
		AGENTBOT_HOME="$home"
		export AGENTBOT_HOME
		# shellcheck source=/dev/null
		source "$ROOT/scripts/lib/python_env.sh"
		agentbot_python_requirements_digest
	)" || return 1
	printf 'PyYAML>=6.0.1,<7\ntomlkit>=0.13,<1\n' >"$home/requirements.txt"
	second="$(
		AGENTBOT_HOME="$home"
		export AGENTBOT_HOME
		# shellcheck source=/dev/null
		source "$ROOT/scripts/lib/python_env.sh"
		agentbot_python_requirements_digest
	)" || return 1
	[[ -n "$first" && "$first" != "$second" ]]
}

# The whole reason the guard exists: asking a question must not build anything.
test_readonly_command_reports_and_builds_nothing() {
	local home output
	home="$TEST_ROOT/readonly"
	mkdir -p "$home"
	cp -r -- "$ROOT/install.sh" "$ROOT/scripts" "$ROOT/src" "$ROOT/requirements.txt" "$home/"
	# An interpreter that fails the tomlkit probe, which is the machine state
	# the guard exists for.
	mkdir -p "$TEST_ROOT/nodeps"
	cat >"$TEST_ROOT/nodeps/python" <<'FAKE'
#!/usr/bin/env bash
set -u
[[ "${1:-}" == '-c' && "${2:-}" == *tomlkit* ]] && exit 1
exit 0
FAKE
	chmod 700 "$TEST_ROOT/nodeps/python"
	output="$(
		cd "$home" || exit 1
		AGENTBOT_PYTHON="$TEST_ROOT/nodeps/python"
		export AGENTBOT_PYTHON
		# The fixture is a partial copy with no sibling shared checkout, and
		# this test is about provisioning, not resolution. Name the real one.
		DOTFILES_SHARED_DIR="$DOTFILES_SHARED_ROOT"
		export DOTFILES_SHARED_DIR
		bash install.sh status 2>&1
	)"
	[[ ! -e "$home/.venv" ]] || return 1
	grep -q 'tomlkit is required' <<<"$output" || return 1
	grep -q 'install' <<<"$output"
}

# The regression this file exists for after the first attempt shipped broken:
# bin/agentbot resolves and exports AGENTBOT_PYTHON before delegating to
# install.sh. If the child reads that as an operator override it skips
# provisioning, then fails the dependency check it just declined to satisfy --
# which is exactly what a real full-update did.
test_inherited_fallback_is_not_an_override() {
	local home
	home="$(make_home inherited)"
	(
		AGENTBOT_HOME="$home"
		export AGENTBOT_HOME
		unset AGENTBOT_PYTHON AGENTBOT_PYTHON_EXPLICIT
		# shellcheck source=/dev/null
		source "$ROOT/scripts/lib/python_env.sh"
		agentbot_python_resolve || exit 1
		# The parent now exports a resolved fallback, as the launcher does.
		# A child must still treat itself as having no override.
		bash -c '
			# shellcheck source=/dev/null
			source "'"$ROOT"'/scripts/lib/python_env.sh"
			[[ -z "$AGENTBOT_PYTHON_EXPLICIT" ]]
		'
	)
}

# And the other half: once provisioning has created .venv, resolution must
# return it rather than the fallback an earlier resolve already exported.
test_resolution_prefers_a_new_venv_over_an_exported_fallback() {
	local home resolved
	home="$(make_home latervenv)"
	resolved="$(
		AGENTBOT_HOME="$home"
		export AGENTBOT_HOME
		unset AGENTBOT_PYTHON AGENTBOT_PYTHON_EXPLICIT
		# shellcheck source=/dev/null
		source "$ROOT/scripts/lib/python_env.sh"
		agentbot_python_resolve || exit 1
		mkdir -p "$home/.venv/bin"
		printf '#!/usr/bin/env bash\nexit 0\n' >"$home/.venv/bin/python"
		chmod 700 "$home/.venv/bin/python"
		agentbot_python_resolve || exit 1
		printf '%s\n' "$AGENTBOT_PYTHON"
	)" || return 1
	[[ "$resolved" == "$home/.venv/bin/python" ]]
}

check 'an inherited fallback is not treated as an override' test_inherited_fallback_is_not_an_override
check 'resolution prefers a newly created venv over an exported fallback' test_resolution_prefers_a_new_venv_over_an_exported_fallback
check 'resolution falls back to python3 on PATH' test_falls_back_to_path_python3
check 'resolution prefers the checkout virtual environment' test_prefers_the_checkout_venv
check 'an explicit interpreter outranks the virtual environment' test_override_outranks_the_venv
check 'an unusable override is ignored rather than honoured' test_unusable_override_is_ignored
check 'an explicit interpreter provisions nothing' test_override_skips_provisioning
check 'the fill stamp tracks the requirements digest' test_stamp_tracks_requirements
check 'a read-only command reports instead of provisioning' test_readonly_command_reports_and_builds_nothing

printf '\nRan %d python-env test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
