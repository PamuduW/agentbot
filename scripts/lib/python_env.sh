# shellcheck shell=bash
# Agentbot's Python interpreter resolution and dependency provisioning.
#
# Agentbot needs third-party Python packages at runtime -- PyYAML for the
# policy renderers, and tomlkit, mcp and httpx for the MCP control plane.
# Every call site used to run a bare `python3`, which on a Debian-family
# machine is the system interpreter marked EXTERNALLY-MANAGED: `pip install`
# into it is refused by PEP 668, and apt carries neither `mcp` nor a tomlkit
# new enough for requirements.txt. The result was that a checkout could
# fast-forward onto code importing a module the machine had no supported way
# to obtain, and the failure surfaced as a traceback out of whichever command
# imported it first.
#
# So Agentbot owns an interpreter instead of borrowing one. `.venv` in the
# repository root is created and filled from requirements.txt by the install
# path, and every Python call site resolves through here.
#
# Resolution order, most explicit first:
#
#   1. $AGENTBOT_PYTHON        an operator's or a test's deliberate override
#   2. $AGENTBOT_HOME/.venv    the interpreter Agentbot provisions for itself
#   3. python3                 the fallback, so a checkout that has never run
#                              install still answers instead of failing to
#                              locate an interpreter at all
#
# The fallback is why the dependency checks in install.sh remain: resolution
# succeeding does not prove the resolved interpreter has the packages.
#
# Resolution publishes its answer in AGENTBOT_PYTHON, which means a child
# process cannot tell an operator's choice from a parent's fallback by reading
# that variable -- and the difference decides whether provisioning is skipped.
# `bin/agentbot` resolves before delegating to install.sh, so trusting the
# inherited value made every install through the launcher skip provisioning and
# then fail the dependency check it had just declined to satisfy.
#
# So the operator's value is captured once, at source time, before any
# resolution can overwrite it. Assignment is `=` and not `:=` on purpose: a
# parent that had no override exports an empty string, and that empty string is
# the answer -- re-deriving it from AGENTBOT_PYTHON is exactly the mistake.
: "${AGENTBOT_PYTHON_EXPLICIT=${AGENTBOT_PYTHON:-}}"
export AGENTBOT_PYTHON_EXPLICIT

agentbot_python_venv_dir() {
	printf '%s/.venv\n' "${AGENTBOT_HOME:?AGENTBOT_HOME is not set}"
}

agentbot_python_venv_bin() {
	printf '%s/bin/python\n' "$(agentbot_python_venv_dir)"
}

# Resolve the interpreter and publish it as AGENTBOT_PYTHON. Idempotent, and
# exported so a child process -- the menu, a skills install, a py_service
# coprocess -- inherits the same answer rather than re-deriving it.
agentbot_python_resolve() {
	local venv_bin
	if [[ -n "${AGENTBOT_PYTHON_EXPLICIT}" && -x "${AGENTBOT_PYTHON_EXPLICIT}" ]]; then
		AGENTBOT_PYTHON="$AGENTBOT_PYTHON_EXPLICIT"
		export AGENTBOT_PYTHON
		return 0
	fi
	# Deliberately recomputed rather than reusing an inherited AGENTBOT_PYTHON:
	# a parent that resolved before .venv existed exported the fallback, and
	# after provisioning the venv is the right answer.
	venv_bin="$(agentbot_python_venv_bin)"
	if [[ -x "$venv_bin" ]]; then
		AGENTBOT_PYTHON="$venv_bin"
	elif command -v python3 >/dev/null 2>&1; then
		AGENTBOT_PYTHON="$(command -v python3)"
	else
		return 1
	fi
	export AGENTBOT_PYTHON
	return 0
}

# The requirements fingerprint the current .venv was filled from. Kept inside
# .venv so deleting the environment also discards the claim that it is current,
# and so the stamp never lands in the working tree.
agentbot_python_stamp_file() {
	printf '%s/.agentbot-requirements\n' "$(agentbot_python_venv_dir)"
}

agentbot_python_requirements_digest() {
	local requirements="${AGENTBOT_HOME}/requirements.txt"
	[[ -f "$requirements" ]] || return 1
	sha256sum -- "$requirements" | cut -d' ' -f1
}

# Create .venv when it is absent and install requirements.txt into it when the
# file has changed since the last fill. Both halves are conditional: a normal
# install on an up-to-date checkout does no network work and costs one
# sha256sum. Only the install path calls this -- read-only commands must not
# create an environment as a side effect of being asked a question.
agentbot_python_ensure() {
	local venv_dir venv_bin stamp digest
	# An explicit AGENTBOT_PYTHON is an operator saying they own the
	# interpreter. Provisioning one anyway would build an environment nothing
	# then uses, so the override opts out of the whole step.
	if [[ -n "${AGENTBOT_PYTHON_EXPLICIT}" && -x "${AGENTBOT_PYTHON_EXPLICIT}" ]]; then
		agentbot_python_resolve
		return 0
	fi
	venv_dir="$(agentbot_python_venv_dir)"
	venv_bin="$(agentbot_python_venv_bin)"
	stamp="$(agentbot_python_stamp_file)"

	if ! digest="$(agentbot_python_requirements_digest)"; then
		printf 'requirements.txt is missing from %s\n' "$AGENTBOT_HOME" >&2
		return 1
	fi

	if [[ ! -x "$venv_bin" ]]; then
		command -v python3 >/dev/null 2>&1 || {
			printf 'python3 is required to create %s\n' "$venv_dir" >&2
			return 1
		}
		# A half-built environment is worse than none: it resolves as the
		# interpreter and then fails on import. Clear it before rebuilding.
		[[ -e "$venv_dir" ]] && rm -rf -- "$venv_dir"
		python3 -m venv "$venv_dir" || {
			printf 'failed to create %s (is python3-venv installed?)\n' "$venv_dir" >&2
			return 1
		}
		rm -f -- "$stamp"
	fi

	if [[ -f "$stamp" && "$(cat -- "$stamp")" == "$digest" ]]; then
		agentbot_python_resolve
		return 0
	fi

	"$venv_bin" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
	"$venv_bin" -m pip install --quiet -r "${AGENTBOT_HOME}/requirements.txt" || {
		printf 'failed to install %s/requirements.txt into %s\n' "$AGENTBOT_HOME" "$venv_dir" >&2
		return 1
	}
	printf '%s\n' "$digest" >"$stamp"
	agentbot_python_resolve
}
