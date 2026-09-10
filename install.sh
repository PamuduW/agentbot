#!/usr/bin/env bash
set -euo pipefail

AGENTBOT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AGENTBOT_HOME
REPO_ROOT="$AGENTBOT_HOME"
# shellcheck source=scripts/lib/github_token.sh
source "${REPO_ROOT}/scripts/lib/github_token.sh"

AGENTBOT_OUT_RESET=''
AGENTBOT_OUT_CYAN=''
AGENTBOT_OUT_YELLOW=''
AGENTBOT_OUT_RED=''
if [[ -z "${NO_COLOR:-}" && (-t 1 || -t 0 || -n "${AGENTBOT_TUI:-}" || -n "${FORCE_COLOR:-}") ]]; then
	AGENTBOT_OUT_RESET=$'\033[0m'
	AGENTBOT_OUT_CYAN=$'\033[36m'
	AGENTBOT_OUT_YELLOW=$'\033[33m'
	AGENTBOT_OUT_RED=$'\033[31m'
fi

# shellcheck source=scripts/lib/repo_update.sh
source "${REPO_ROOT}/scripts/lib/repo_update.sh"
# shellcheck source=scripts/lib/shared/tui/tty.sh
source "${REPO_ROOT}/scripts/lib/shared/tui/tty.sh"

github_token_child() (
	github_token_export_if_valid
	"$@"
)

die() {
	printf '  %s[err]%s %s\n' "$AGENTBOT_OUT_RED" "$AGENTBOT_OUT_RESET" "$*" >&2
	exit 1
}

info() {
	printf '  %s[info]%s %s\n' "$AGENTBOT_OUT_CYAN" "$AGENTBOT_OUT_RESET" "$*"
}

warn() {
	printf '  %s[warn]%s %s\n' "$AGENTBOT_OUT_YELLOW" "$AGENTBOT_OUT_RESET" "$*" >&2
}

bootstrap_quiet() {
	[[ -n "${AGENTBOT_TUI:-}${AGENTBOT_QUIET:-}" ]]
}

log_info() {
	bootstrap_quiet && return 0
	info "$@"
}

check_python_deps() {
	if ! command -v python3 >/dev/null 2>&1; then
		die "python3 is required"
	fi
	if ! python3 -c "import yaml" >/dev/null 2>&1; then
		die "PyYAML is required (run: python3 -m pip install -r requirements.txt)"
	fi
	if ! python3 -c "import src.cli" >/dev/null 2>&1; then
		die "Agentbot Python CLI is unavailable (restore the checkout, then retry)"
	fi
}

check_skills_deps() {
	check_python_deps
	if ! command -v node >/dev/null 2>&1; then
		die "node is required (install Node.js for npx skills)"
	fi
	if ! command -v npx >/dev/null 2>&1; then
		die "npx is required (install Node.js/npm for npx skills)"
	fi
}

run_cli() {
	python3 -m src.cli --root "$REPO_ROOT" "$@"
}

run_global_render() {
	run_cli global "$@"
}

run_doctor() {
	run_cli doctor "$@"
}

run_status() {
	run_cli status "$@"
}

run_skills() {
	local subcmd="${1:-}"
	shift

	case "$subcmd" in
	install | update | upgrade) github_token_child run_cli skills "$subcmd" "$@" ;;
	*) run_cli skills "$subcmd" "$@" ;;
	esac
}

cleanup_owned_old_agentboot_link() {
	local old_link="${HOME}/bin/agentboot" raw resolved
	[[ -e "$old_link" || -L "$old_link" ]] || return 0
	if [[ ! -L "$old_link" ]]; then
		warn "preserving non-symlink old path: ${old_link}"
		return 0
	fi
	raw="$(readlink -- "$old_link" 2>/dev/null || true)"
	if [[ -z "$raw" ]]; then
		warn "preserving unprovable old symlink: ${old_link}"
		return 0
	fi
	if [[ "$raw" == /* ]]; then
		resolved="$(realpath -m -- "$raw")"
	else resolved="$(realpath -m -- "$(dirname "$old_link")/$raw")"; fi
	if [[ "$resolved" == "$(realpath -m -- "$AGENTBOT_HOME/bin/agentboot")" ]]; then
		rm -- "$old_link"
		log_info "removed owned old link: ${old_link}"
	else
		warn "preserving foreign old symlink: ${old_link} -> ${raw}"
	fi
}

link_agentbot() {
	local source="${AGENTBOT_HOME}/bin/agentbot"
	local target="${HOME}/bin/agentbot" source_resolved target_resolved
	[[ -x "$source" ]] || die "Agentbot executable is missing: $source"
	source_resolved="$(readlink -f -- "$source" 2>/dev/null || true)"
	[[ -n "$source_resolved" && -x "$source_resolved" ]] || die "Agentbot executable is missing: $source"

	if [[ -e "$target" || -L "$target" ]]; then
		if [[ ! -L "$target" ]]; then
			die "refusing to replace existing launcher: ${target}. Move or remove it, then retry."
		fi
		target_resolved="$(readlink -f -- "$target" 2>/dev/null || true)"
		if [[ "$target_resolved" != "$source_resolved" ]]; then
			die "refusing to replace existing launcher: ${target}. Move or remove it, then retry."
		fi
	else
		if ! mkdir -p "${HOME}/bin"; then
			die "failed to create Agentbot launcher: ${target}. Inspect the existing path, then retry."
		fi
		if ! ln -sT -- "$source" "$target" >/dev/null 2>&1; then
			die "failed to create Agentbot launcher: ${target}. Inspect the existing path, then retry."
		fi
	fi

	target_resolved="$(readlink -f -- "$target" 2>/dev/null || true)"
	[[ "$target_resolved" == "$source_resolved" && -x "$target_resolved" ]] || die "failed to verify Agentbot launcher: ${target}"
	log_info "linked ${target} -> ${source}"
}

run_agentbot_install_backend() {
	check_skills_deps
	log_info "installing Agentbot from ${REPO_ROOT}"

	local rc=0
	github_token_child run_cli install "$@" || rc=$?

	return "$rc"
}

run_install() {
	run_install_repo_gate || return $?
	[[ "${AGENTBOT_REPOSITORY_UPDATE_DECLINED:-false}" == true ]] && return 0
	# The component selector runs in the menu, before this process starts, so
	# the menu asks for the gate on its own first: a pull moves the checkout and
	# restarts the run, and asking afterwards would throw the selection away.
	# Same contract, stopped one step early.
	[[ "${AGENTBOT_INSTALL_GATE_ONLY:-0}" == 1 ]] && return 0
	local rc=0
	# Forwarded so the component selector can narrow an install; no arguments
	# still means every component, exactly as before selection existed.
	run_agentbot_install_backend "$@" || rc=$?
	cleanup_owned_old_agentboot_link
	link_agentbot
	if ((rc == 0)); then
		log_info "Agentbot install complete"
	else
		warn "Agentbot install failed (exit ${rc})"
	fi
	return "$rc"
}

run_update_decision() {
	if [[ "${AGENTBOT_UPDATE_INTERACTIVE:-0}" == 1 ]]; then
		run_repo_update_prompt "$1"
		return $?
	fi
	case "${AGENTBOT_UPDATE_CONFIRM:-no}" in
	1 | true | yes | y) return 0 ;;
	*) return 1 ;;
	esac
}

print_repo_update_changes() {
	local max_lines=20 total shown=0 omitted quoted_repo line
	[[ -n "${REPO_UPDATE_CHANGES:-}" ]] || return 0
	total="$(repo_update_change_count)"
	printf '  Local changes:\n'
	while IFS= read -r line; do
		((shown >= max_lines)) && break
		printf '  %s\n' "$line"
		shown=$((shown + 1))
	done <<<"$REPO_UPDATE_CHANGES"
	omitted=$((total - shown))
	if ((omitted > 0)); then
		printf '  ... %d more local change(s)\n' "$omitted"
	fi
	printf -v quoted_repo '%q' "$REPO_ROOT"
	printf '  Full list: git -C %s status --short --untracked-files=all\n' "$quoted_repo"
}

print_repo_update_table() {
	repo_update_print_report "$REPO_ROOT"
}

run_repo_update_prompt() {
	local action="$1" prompt answer='' tty_input tty_output
	case "$action" in
	pull-behind) prompt="Pull ${REPO_UPDATE_BEHIND:-0} commit(s) with --ff-only?" ;;
	continue-ahead) prompt='The repository is ahead. Continue with the Agentbot update?' ;;
	replace-local) prompt="Back up local work and replace it with ${REPO_UPDATE_UPSTREAM:-upstream}?" ;;
	*) return 1 ;;
	esac
	if [[ -n "${AGENTBOT_UPDATE_TTY_INPUT:-}" ]]; then
		DOTFILES_TTY_INPUT="$AGENTBOT_UPDATE_TTY_INPUT"
	fi
	if [[ -n "${AGENTBOT_UPDATE_TTY_OUTPUT:-}" ]]; then
		DOTFILES_TTY_OUTPUT="$AGENTBOT_UPDATE_TTY_OUTPUT"
	fi
	if tty_use_output_fd; then
		print_repo_update_table >&"$DOTFILES_TTY_OUT_FD"
		printf '  %s [y/N]: ' "$prompt" >&"$DOTFILES_TTY_OUT_FD"
	else
		tty_output_available || return 1
		tty_output="$(tty_output_path)"
		print_repo_update_table >>"$tty_output"
		printf '  %s [y/N]: ' "$prompt" >>"$tty_output"
	fi
	if tty_use_input_fd; then
		IFS= read -r answer <&"$DOTFILES_TTY_IN_FD" || true
	else
		tty_input_available || return 1
		tty_input="$(tty_input_path)"
		IFS= read -r answer <"$tty_input" || true
	fi
	case "$answer" in
	y | Y | yes | YES) return 0 ;;
	*)
		if tty_use_output_fd; then
			repo_update_print_declined "$action" >&"$DOTFILES_TTY_OUT_FD"
		else
			repo_update_print_declined "$action" >>"$tty_output"
		fi
		AGENTBOT_REPOSITORY_UPDATE_DECLINE_REPORTED=true
		return 1
		;;
	esac
}

run_install_decision() {
	if [[ -n "${AGENTBOT_INSTALL_CONFIRM:-}" ]]; then
		[[ "$AGENTBOT_INSTALL_CONFIRM" == yes ]]
		return
	fi
	run_repo_update_prompt "$1"
}

run_install_repo_gate() {
	local update_outcome update_reason repo_rc=0
	AGENTBOT_REPOSITORY_UPDATE_DECLINED=false
	AGENTBOT_REPOSITORY_UPDATE_DECLINE_REPORTED=false
	agentbot_repo_update_run "$REPO_ROOT" run_install_decision update_outcome update_reason agentbot || repo_rc=$?
	case "$repo_rc" in
	0) return 0 ;;
	2)
		agentbot_repo_update_print_changed
		return 2
		;;
	*)
		if agentbot_repo_update_is_declined "$update_reason"; then
			if [[ "${AGENTBOT_REPOSITORY_UPDATE_DECLINE_REPORTED:-false}" != true ]]; then
				repo_update_print_declined pull-behind
			fi
			AGENTBOT_REPOSITORY_UPDATE_DECLINED=true
			[[ -n "${AGENTBOT_TUI:-}" ]] && return 3
			return 0
		fi
		if [[ "${REPO_UPDATE_DIRTY:-0}" == 1 ]]; then
			print_repo_update_table >&2
			print_repo_update_changes >&2
			warn 'Repository pull and Agentbot install stopped.'
		else
			warn "repository update stopped: $update_reason"
		fi
		return 1
		;;
	esac
}

run_update_backend_as() {
	local update_command="$1"
	shift
	# shellcheck disable=SC2034  # populated indirectly by agentbot_repo_update_run
	local confirm=no dry_run=false interactive=false arg update_outcome update_reason repo_rc=0
	AGENTBOT_REPOSITORY_UPDATE_DECLINED=false
	AGENTBOT_REPOSITORY_UPDATE_DECLINE_REPORTED=false
	for arg in "$@"; do
		case "$arg" in
		--yes) confirm=yes ;;
		--dry-run) dry_run=true ;;
		--interactive)
			interactive=true
			export AGENTBOT_UPDATE_INTERACTIVE=1
			;;
		*) die "unknown update option: $arg" ;;
		esac
	done
	[[ "$confirm" == yes ]] && export AGENTBOT_UPDATE_CONFIRM=yes

	agentbot_repo_update_run "$REPO_ROOT" run_update_decision update_outcome update_reason || repo_rc=$?
	case "$repo_rc" in
	2)
		agentbot_repo_update_print_changed
		return 2
		;;
	1)
		if agentbot_repo_update_is_declined "$update_reason"; then
			if [[ "${AGENTBOT_REPOSITORY_UPDATE_DECLINE_REPORTED:-false}" != true ]]; then
				repo_update_print_declined pull-behind
			fi
			AGENTBOT_REPOSITORY_UPDATE_DECLINED=true
			[[ -n "${AGENTBOT_TUI:-}" ]] && return 3
			return 0
		fi
		if [[ "${REPO_UPDATE_DIRTY:-0}" == 1 ]]; then
			print_repo_update_table >&2
			print_repo_update_changes >&2
			warn 'Repository pull and downstream updates stopped.'
			if [[ "$update_reason" == dirty ]]; then
				warn 'Resolve the local changes, then run agentbot update again.'
			else
				warn "Remote freshness could not be verified: $update_reason"
			fi
		else
			warn "repository update stopped: $update_reason"
		fi
		return 1
		;;
	esac
	if [[ "$dry_run" == true || ("$interactive" == false && "${AGENTBOT_UPDATE_SHOW_STATUS:-1}" == 1) ]]; then
		run_cli status
	fi
	github_token_child run_cli "$update_command" "$@"
}

# `full` = install then update, sharing one exit contract.
#
# Both stages already return 0 continue / 1 stop / 2 repository changed. The
# only extra rule is the restart budget: a repository change may legitimately
# happen once (the checkout moved forward and this process is running the old
# code), so the stage is retried from the new checkout exactly once.
run_full() {
	local stage rc restarts=0
	for stage in install update; do
		while true; do
			rc=0
			case "$stage" in
			install) run_install || rc=$? ;;
			update)
				check_skills_deps
				run_update_backend_as update --yes || rc=$?
				;;
			esac
			case "$rc" in
			0) break ;;
			2)
				if ((restarts >= 1)); then
					warn 'repository changed more than once; full run stopped'
					return 1
				fi
				restarts=$((restarts + 1))
				log_info 'restarting from the updated checkout'
				;;
			*) return "$rc" ;;
			esac
		done
	done
	log_info 'Agentbot full run complete'
}

usage() {
	cat <<EOF
Usage: ./install.sh <command> [args]

  Commands:
  install [--components L]   Install Agentbot and link its launcher;
                             L narrows it to skills, graphify, boost
  full                       Run install, then update, in one command
  update [--dry-run|--yes]   Run the repository-first update flow
  status [--json]            Show current Agentbot state
  doctor                     Validate the installation
  skills <command>           Install, update, list, validate, or prune skills
  global                     Refresh managed global outputs
  workspace|workspaces|resync Manage registered workspace outputs
  graphify status|setup      Inspect or repair generic Graphify Agent Skills
  boost status|setup|off     Inspect or manage Boost Claude/Codex/Cursor integration
  cli-config status|apply    Merge the declared agent CLI configuration keys
  cursor status|statusline   Inspect or install the managed Cursor statusline
  vscode status|seed|apply   Reconcile VS Code extensions and owned settings
  help                       Show this rescue summary

For the complete command and option reference, run: agentbot help
EOF
}

main() {
	# Record where the operator actually invoked us from before leaving it.
	# run_cli needs the checkout as its working directory, so without this the
	# CLI resolves every relative workspace path against the checkout instead.
	AGENTBOT_CALLER_PWD="$PWD"
	export AGENTBOT_CALLER_PWD
	cd "$REPO_ROOT"

	# This must happen in main: `set --` inside a helper only changes that
	# helper's positional parameters, which broke the advertised legacy flags.
	case "${1:-}" in
	--status) set -- status "${@:2}" ;;
	--global) set -- global "${@:2}" ;;
	--workspace | --all)
		die "workspace/all render is archived — see archive/docs/README.md"
		;;
	esac

	local cmd="${1:-}"

	case "$cmd" in
	"")
		"$AGENTBOT_HOME/bin/agentbot"
		;;
	install)
		run_install "${@:2}"
		;;
	full)
		check_skills_deps
		run_full
		;;
	update | upgrade)
		check_skills_deps
		run_update_backend_as "$cmd" "${@:2}"
		;;
	skills)
		if [[ $# -lt 2 ]]; then
			die "usage: ./install.sh skills <install|update|upgrade|list|doctor|prune|remove-manual>"
		fi
		case "${2}" in
		install | update | upgrade) check_skills_deps ;;
		*) check_python_deps ;;
		esac
		run_skills "${2}" "${@:3}"
		;;
	doctor | status | global)
		check_python_deps
		case "$cmd" in
		global) run_global_render "${@:2}" ;;
		doctor) run_doctor "${@:2}" ;;
		status) run_status "${@:2}" ;;
		esac
		;;
	workspace | workspaces | resync)
		check_python_deps
		run_cli "$cmd" "${@:2}"
		;;
	boot)
		check_python_deps
		run_cli boot "${@:2}"
		;;
	graphify | boost)
		check_python_deps
		run_cli "$cmd" "${@:2}"
		;;
	cli-config | cursor | vscode)
		check_python_deps
		run_cli "$cmd" "${@:2}"
		;;
	all | interactive | import-local | remove-managed | delete-local)
		die "${cmd} is archived — see archive/docs/README.md"
		;;
	-h | --help | help)
		usage
		;;
	*)
		die "unknown command: ${cmd} (run ./install.sh --help)"
		;;
	esac
}

if [[ "${AGENTBOT_SOURCE_ONLY:-0}" != 1 ]]; then
	main "$@"
fi
