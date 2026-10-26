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
# Agentbot prepares permanent releases from the approved dependency lock and
# switches the repository-root `.venv` symlink only after validation. Every
# Python call site resolves through here.
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
	local directory
	directory="$(agentbot_python_venv_dir)"
	if [[ -L "$directory" ]]; then directory="$(realpath -- "$directory")" || return 1; fi
	printf '%s/bin/python\n' "$directory"
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

# Provision only a qualified, hash-pinned dependency closure. The stamp binds
# both the source requirements and the frozen dependency lock. Read-only
# commands use resolution without provisioning.
agentbot_python_catalog_lock() {
	if [[ -z "${DOTFILES_SHARED_LIB:-}" || -z "${DOTFILES_SHARED_ROOT:-}" ]]; then
		printf 'Resolve dotfiles-shared before provisioning approved Python dependencies.\n' >&2
		return 1
	fi
	# shellcheck source=/dev/null
	source "$DOTFILES_SHARED_LIB/software_catalog.sh"
	if [[ -n "${AGENTBOT_QUALIFICATION_CONTEXT:-}" ]]; then
		software_catalog_qualification_begin "$AGENTBOT_QUALIFICATION_CONTEXT" || return 1
	fi
	software_catalog_begin "$DOTFILES_SHARED_ROOT" || return 1
	local architecture target lock
	case "$(uname -m)" in
	x86_64 | amd64) architecture=amd64 ;;
	aarch64 | arm64) architecture=arm64 ;;
	*) return 1 ;;
	esac
	target="linux-$architecture"
	[[ "$(uname -r)" == *[Mm]icrosoft* ]] && target="wsl2-$architecture"
	lock="$(software_catalog_get python.pip lock "$target")" || return 1
	AGENTBOT_REQUIREMENTS_LOCK="$SOFTWARE_CATALOG_SNAPSHOT/$lock"
	[[ -f "$AGENTBOT_REQUIREMENTS_LOCK" && ! -L "$AGENTBOT_REQUIREMENTS_LOCK" ]] || return 1
}

_agentbot_python_stamp_valid() {
	local path="$1"
	local -a lines=()
	[[ -f "$path" && ! -L "$path" && -O "$path" && "$(stat -c %s -- "$path")" -le 130 ]] || return 1
	mapfile -t lines <"$path"
	[[ "${#lines[@]}" == 1 && "${lines[0]}" =~ ^[0-9a-f]{64}(:[0-9a-f]{64})?$ ]]
}

_agentbot_python_owned() {
	local directory="$1" releases="$2" actual
	[[ -d "$directory" && -O "$directory" ]] || return 1
	if [[ -L "$directory" ]]; then
		actual="$(realpath -- "$directory")" || return 1
		[[ "$actual" == "$releases/"* ]] || return 1
	fi
	_agentbot_python_stamp_valid "$directory/.agentbot-requirements"
}

_agentbot_python_install() (
	local digest="$1" lock_file="$2" directory releases lock_fd release='' backup='' temporary_link=''
	local committed=0 published=0 moved_previous=0 keep_release=0 rc=0 actual
	directory="$(agentbot_python_venv_dir)"
	releases="$AGENTBOT_HOME/.venv-releases"
	[[ ! -L "$releases" && ! -L "$releases/install.lock" ]] || return 2
	if [[ ! -d "$releases" ]]; then mkdir -m 700 -- "$releases" || return 1; fi
	[[ -O "$releases" ]] || return 2
	releases="$(cd -- "$releases" && pwd -P)" || return 1
	if [[ -e "$releases/install.lock" ]]; then
		[[ -f "$releases/install.lock" && -O "$releases/install.lock" ]] || return 2
	fi
	exec {lock_fd}>>"$releases/install.lock" || return 1
	flock -n "$lock_fd" || {
		printf 'Another Python environment update is running.\n' >&2
		return 75
	}
	if [[ -e "$directory" || -L "$directory" ]]; then
		if ! _agentbot_python_owned "$directory" "$releases"; then
			printf 'Preserving an unowned or unverified Python environment at %s.\n' "$directory" >&2
			return 2
		fi
		if [[ -x "$directory/bin/python" && "$(cat -- "$directory/.agentbot-requirements")" == "$digest" ]]; then return 0; fi
	fi
	release="$(mktemp -d "$releases/env-XXXXXXXX")" || return 1
	temporary_link="$releases/current-$BASHPID"
	backup="$releases/previous-$BASHPID"
	[[ ! -e "$temporary_link" && ! -L "$temporary_link" && ! -e "$backup" && ! -L "$backup" ]] || return 2
	# shellcheck disable=SC2317,SC2329  # Invoked by this transaction's EXIT trap.
	_agentbot_python_cleanup() {
		local status=$? recovery_failed=0
		if [[ "$committed" == 0 ]]; then
			if [[ "$published" == 1 ]]; then rm -f -- "$directory" || recovery_failed=1; fi
			if [[ "$published" == 1 || "$moved_previous" == 1 ]]; then
				if [[ -e "$backup" || -L "$backup" ]]; then mv -T -- "$backup" "$directory" || recovery_failed=1; fi
			elif [[ -L "$backup" ]]; then rm -f -- "$backup" || recovery_failed=1; fi
			if [[ "$recovery_failed" == 0 ]]; then rm -rf -- "$release"; else keep_release=1; fi
		fi
		rm -f -- "$temporary_link"
		if [[ "$keep_release" == 1 ]]; then
			printf 'Python environment rollback needs recovery; originals retained at %s.\n' "$backup" >&2
		fi
		return "$status"
	}
	trap '_agentbot_python_cleanup' EXIT
	python3 -m venv "$release" || return $?
	[[ -x "$release/bin/python" ]] || return 1
	"$release/bin/python" -m pip install --quiet --require-hashes --no-deps --only-binary=:all: -r "$lock_file" || rc=$?
	if ((rc != 0)); then
		printf 'Failed to prepare the approved Python closure; preserving the working environment.\n' >&2
		return "$rc"
	fi
	"$release/bin/python" -m pip check || return $?
	actual="$(sha256sum -- "$lock_file")" || return 1
	[[ "${digest#*:}" == "${actual%% *}" && "${digest%%:*}" == "$(agentbot_python_requirements_digest)" ]] || return 2
	printf '%s\n' "$digest" >"$release/.agentbot-requirements" || return 1
	ln -s -- "$release" "$temporary_link" || return 1
	if [[ -L "$directory" ]]; then
		cp -a -- "$directory" "$backup" || return 1
	elif [[ -e "$directory" ]]; then
		mv -T -- "$directory" "$backup" || return $?
		moved_previous=1
	fi
	mv -Tf -- "$temporary_link" "$directory" || return $?
	published=1
	_agentbot_python_owned "$directory" "$releases" || return 2
	committed=1
)

agentbot_python_ensure() {
	local digest lock_digest
	# An external environment cannot prove installation from the frozen wheel hashes.
	if [[ -n "${AGENTBOT_PYTHON_EXPLICIT}" ]]; then
		printf 'Unset AGENTBOT_PYTHON and AGENTBOT_PYTHON_EXPLICIT to provision the approved Python environment. External interpreters remain available for read-only commands.\n' >&2
		return 2
	fi
	agentbot_python_catalog_lock || return 1
	digest="$(agentbot_python_requirements_digest)" || return 1
	lock_digest="$(sha256sum -- "$AGENTBOT_REQUIREMENTS_LOCK")" || return 1
	digest="$digest:${lock_digest%% *}"
	_agentbot_python_install "$digest" "$AGENTBOT_REQUIREMENTS_LOCK" || return $?
	agentbot_python_resolve
}
