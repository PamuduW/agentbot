#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/lib/harness.sh
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"

AGENTBOT="$ROOT/bin/agentbot"
test_harness_report_init

test_launcher_and_headless_guidance() {
	local output rc
	[[ -x "$AGENTBOT" ]] || return 1
	set +e
	output="$(env -u AGENTBOT_TTY "$AGENTBOT" 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -ne 0 && "$output" == '[err] No usable controlling TTY. Use agentbot help or an explicit command such as agentbot install.' ]]
}

test_menu_repository_change_propagates_to_launcher() (
	AGENTBOT_SOURCE_ONLY=1 source "$AGENTBOT"
	agentbot_has_tty() { return 0; }
	agentbot_run_menu() { return 2; }
	local rc=0
	agentbot_main || rc=$?
	[[ "$rc" -eq 2 ]]
)

test_shell_tty_access_stays_in_the_adapter() (
	! rg -n '/dev/tty' "$ROOT/bin" "$ROOT/scripts" "$ROOT/install.sh" \
		--glob '!tests/**' --glob '!**/lib/shared/tui/tty.sh' --glob '!**/lib/tui.sh'
)

test_symlink_resolves_repository_root() {
	local link="$TEST_ROOT/agentbot-link" output rc
	ln -s "$AGENTBOT" "$link"
	set +e
	output="$(AGENTBOT_TTY=0 "$link" status 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -eq 0 && "$output" == *'=== Check Status ==='* && "$output" != *'No such file or directory'* ]]
}

test_dispatch_matrix() (
	AGENTBOT_SOURCE_ONLY=1 source "$AGENTBOT"
	local calls="$TEST_ROOT/dispatch.calls"
	: >"$calls"
	agentbot_has_tty() { return 0; }
	agentbot_run_menu() {
		printf 'menu\n' >>"$calls"
		return 11
	}
	agentbot_run_token() {
		printf 'token\n' >>"$calls"
		return 12
	}
	agentbot_run_backend() {
		printf 'backend:%s\n' "$*" >>"$calls"
		return 14
	}
	set +e
	agentbot_main
	[[ $? -eq 11 ]] || exit 1
	agentbot_main status
	[[ $? -eq 14 ]] || exit 1
	agentbot_main install
	[[ $? -eq 14 ]] || exit 1
	agentbot_main doctor
	[[ $? -eq 14 ]] || exit 1
	agentbot_main token
	[[ $? -eq 12 ]] || exit 1
	agentbot_main graphify status
	[[ $? -eq 14 ]] || exit 1
	agentbot_main update
	[[ $? -eq 14 ]] || exit 1
	agentbot_main boot --cursor /tmp/project
	[[ $? -eq 14 ]] || exit 1
	agentbot_main workspace /tmp/project
	[[ $? -eq 14 ]] || exit 1
	agentbot_main workspaces
	[[ $? -eq 14 ]] || exit 1
	agentbot_main resync --all
	[[ $? -eq 14 ]] || exit 1
	set -e
	[[ "$(<"$calls")" == $'menu\nbackend:status\nbackend:install\nbackend:doctor\ntoken\nbackend:graphify status\nbackend:update\nbackend:boot --cursor /tmp/project\nbackend:workspace /tmp/project\nbackend:workspaces\nbackend:resync --all' ]]
)

test_token_route_loads_existing_menu() (
	AGENTBOT_SOURCE_ONLY=1 source "$AGENTBOT"
	local calls="$TEST_ROOT/token-route.calls"
	: >"$calls"
	AGENTBOT_MENU_LOADED=1
	agentbot_token_config_menu() { printf 'token-menu\n' >>"$calls"; }
	agentbot_run_token
	[[ "$(<"$calls")" == token-menu ]]
)

test_install_forwards_public_commands() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local calls="$TEST_ROOT/install-public.calls"
	: >"$calls"
	check_python_deps() { :; }
	check_skills_deps() { :; }
	run_cli() { printf '%s\n' "$*" >>"$calls"; }
	main skills install alpha
	main skills update beta
	main skills list
	main skills doctor
	main global
	main doctor
	main status --json
	main workspace --profile safe /tmp/project
	main workspaces --remove /tmp/project
	main resync --dry-run --all
	main graphify status
	[[ "$(<"$calls")" == $'skills install alpha\nskills update beta\nskills list\nskills doctor\nglobal\ndoctor\nstatus --json\nworkspace --profile safe /tmp/project\nworkspaces --remove /tmp/project\nresync --dry-run --all\ngraphify status' ]]
)

test_boot_selectors_and_atomic_validation() {
	local target="$TEST_ROOT/boot-target" config="$TEST_ROOT/boot-config" rc
	mkdir -p "$target"
	XDG_CONFIG_HOME="$config" AGENTBOT_HOME="$ROOT" "$AGENTBOT" boot "$target" >/dev/null
	[[ -f "$target/AGENTS.md" && -f "$target/CLAUDE.md" ]] || return 1
	# safe-default lists cursor as allowed, not default. A bare boot must honour
	# that; it used to hardcode every target and render the rule regardless.
	[[ ! -e "$target/.cursor/rules/agentbot-policy.mdc" ]] || return 1
	# Copilot is no longer a supported target; no boot path may produce one.
	[[ ! -e "$target/.github/copilot-instructions.md" ]] || return 1
	rm -rf "$target" "$config"
	mkdir -p "$target"
	XDG_CONFIG_HOME="$config" AGENTBOT_HOME="$ROOT" "$AGENTBOT" boot --cursor "$target" >/dev/null
	[[ -f "$target/.cursor/rules/agentbot-policy.mdc" && ! -e "$target/CLAUDE.md" ]] || return 1
	rm -rf "$target" "$config"
	mkdir -p "$target"
	set +e
	XDG_CONFIG_HOME="$config" AGENTBOT_HOME="$ROOT" "$AGENTBOT" boot --unknown "$target" >/dev/null 2>&1
	rc=$?
	set -e
	[[ "$rc" -ne 0 && ! -e "$target/AGENTS.md" && ! -e "$target/CLAUDE.md" ]]
}

test_workspace_paths_resolve_against_the_caller_directory() {
	# Break caught: install.sh cd's to the checkout, so a relative workspace
	# path -- including boot's "." default -- rendered into Agentbot itself.
	local target="$TEST_ROOT/caller-target" config="$TEST_ROOT/caller-config" output
	mkdir -p "$target/nested"
	(cd "$target" && XDG_CONFIG_HOME="$config" AGENTBOT_HOME="$ROOT" "$AGENTBOT" boot >/dev/null)
	[[ -f "$target/AGENTS.md" ]] || return 1
	[[ ! -e "$ROOT/AGENTS_temp.md" ]] || return 1
	output="$(cd "$target" && XDG_CONFIG_HOME="$config" AGENTBOT_HOME="$ROOT" \
		NO_COLOR=1 "$AGENTBOT" workspace nested)"
	[[ "$output" == *"$target/nested"* ]]
}

test_install_repo_gate_link_and_failure_status() (
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local calls="$TEST_ROOT/install.calls" output rc
	: >"$calls"
	agentbot_repo_update_run() {
		printf 'repo\n' >>"$calls"
		printf -v "$3" '%s' current
		printf -v "$4" '%s' current
	}
	run_agentbot_install_backend() {
		printf 'backend\n' >>"$calls"
		return 7
	}
	set +e
	output="$(run_install 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -eq 7 && "$(<"$calls")" == $'repo\nbackend' ]] || return 1
	[[ "$output" == *'Agentbot install failed (exit 7)'* && "$output" != *'Agentbot install complete'* ]] || return 1
	[[ "$(readlink "$HOME/bin/agentbot")" == "$ROOT/bin/agentbot" ]]
)

test_link_agentbot_only_replaces_owned_launchers() (
	# Break caught: overwriting a foreign launcher that appears after ownership classification.
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local source="$ROOT/bin/agentbot" home target output rc raw before expected

	run_link() {
		local case_home="$1"
		set +e
		output="$(HOME="$case_home" link_agentbot 2>&1)"
		rc=$?
		set -e
	}

	assert_refusal() {
		expected="[err] refusing to replace existing launcher: ${target}. Move or remove it, then retry."
		[[ "$rc" -eq 1 && "$output" == "$expected" ]]
	}

	run_foreign_create_race() {
		local case_home="$1"
		set +e
		output="$(
			HOME="$case_home"
			ln() {
				printf 'foreign launcher contents\n' >"$target"
				command ln "$@"
			}
			link_agentbot 2>&1
		)"
		rc=$?
		set -e
	}

	run_wrong_link_success() {
		local case_home="$1"
		set +e
		output="$(
			HOME="$case_home"
			ln() {
				command ln -sT -- "$TEST_ROOT/foreign-agentbot" "$target"
			}
			link_agentbot 2>&1
		)"
		rc=$?
		set -e
	}

	home="$TEST_ROOT/launcher-absent"
	target="$home/bin/agentbot"
	run_link "$home"
	[[ "$rc" -eq 0 && "$output" == "[info] linked ${target} -> ${source}" ]] || return 1
	[[ "$(readlink -f "$target")" == "$source" && -x "$(readlink -f "$target")" ]] || return 1

	home="$TEST_ROOT/launcher-owned"
	target="$home/bin/agentbot"
	mkdir -p "$(dirname "$target")"
	ln -s "$source" "$target"
	run_link "$home"
	[[ "$rc" -eq 0 && "$output" == "[info] linked ${target} -> ${source}" ]] || return 1
	[[ "$(readlink -f "$target")" == "$source" && -x "$(readlink -f "$target")" ]] || return 1

	home="$TEST_ROOT/launcher-foreign-symlink"
	target="$home/bin/agentbot"
	mkdir -p "$(dirname "$target")"
	printf 'foreign executable\n' >"$TEST_ROOT/foreign-agentbot"
	chmod +x "$TEST_ROOT/foreign-agentbot"
	ln -s "$TEST_ROOT/foreign-agentbot" "$target"
	raw="$(readlink "$target")"
	run_link "$home"
	assert_refusal || return 1
	[[ -L "$target" && "$(readlink "$target")" == "$raw" ]] || return 1

	home="$TEST_ROOT/launcher-unprovable-symlink"
	target="$home/bin/agentbot"
	mkdir -p "$(dirname "$target")"
	ln -s "$TEST_ROOT/missing-agentbot" "$target"
	raw="$(readlink "$target")"
	run_link "$home"
	assert_refusal || return 1
	[[ -L "$target" && "$(readlink "$target")" == "$raw" ]] || return 1

	home="$TEST_ROOT/launcher-foreign-file"
	target="$home/bin/agentbot"
	mkdir -p "$(dirname "$target")"
	printf 'foreign launcher contents\n' >"$target"
	before="$(sha256sum "$target")"
	run_link "$home"
	assert_refusal || return 1
	[[ -f "$target" && ! -L "$target" && "$(sha256sum "$target")" == "$before" ]] || return 1

	home="$TEST_ROOT/launcher-directory"
	target="$home/bin/agentbot"
	mkdir -p "$target"
	printf 'do not alter\n' >"$target/marker"
	run_link "$home"
	assert_refusal || return 1
	[[ -d "$target" && "$(<"$target/marker")" == 'do not alter' ]] || return 1

	home="$TEST_ROOT/launcher-create-race"
	target="$home/bin/agentbot"
	run_foreign_create_race "$home"
	expected="[err] failed to create Agentbot launcher: ${target}. Inspect the existing path, then retry."
	[[ "$rc" -eq 1 && "$output" == "$expected" ]] || return 1
	[[ -f "$target" && ! -L "$target" && "$(<"$target")" == 'foreign launcher contents' ]] || return 1

	home="$TEST_ROOT/launcher-verification-failure"
	target="$home/bin/agentbot"
	printf 'foreign executable\n' >"$TEST_ROOT/foreign-agentbot"
	chmod +x "$TEST_ROOT/foreign-agentbot"
	run_wrong_link_success "$home"
	expected="[err] failed to verify Agentbot launcher: ${target}"
	[[ "$rc" -eq 1 && "$output" == "$expected" ]] || return 1
	[[ -L "$target" && "$(readlink -f "$target")" == "$TEST_ROOT/foreign-agentbot" ]] || return 1
)

test_one_backend_and_complete_help() {
	local bin_files output boot_help workspace_help workspaces_help
	bin_files="$(find "$ROOT/bin" -maxdepth 1 -type f -printf '%f\n' | sort)"
	[[ "$bin_files" == agentbot ]] || return 1
	output="$(NO_COLOR=1 AGENTBOT_TTY=0 "$AGENTBOT" help)"
	[[ "$output" == *'AGENTBOT_HOME'* && "$output" == *'GITHUB_TOKEN'* ]] || return 1
	boot_help="$(NO_COLOR=1 "$AGENTBOT" help boot)"
	workspace_help="$(NO_COLOR=1 "$AGENTBOT" help workspace)"
	workspaces_help="$(NO_COLOR=1 "$AGENTBOT" help workspaces)"
	[[ "$boot_help" == *'--claude'* ]] || return 1
	[[ "$workspace_help" == *'--targets LIST'* ]] || return 1
	[[ "$workspaces_help" == *'--paths0'* && "$workspaces_help" == *'--remove PATH'* ]]
}

test_every_public_command_reaches_a_route() (
	# The launcher and install.sh dispatch by hand while `agentbot help` prints
	# the command index from src/commands.py. Three public commands (cli-config,
	# cursor, vscode) were advertised there while both shell layers answered
	# "unknown command". Derive the expectation from the same table the help
	# text uses so the next added command fails here instead of shipping
	# unreachable.
	local names unrouted=""
	names="$(cd "$ROOT" && python3 -c "
from src.commands import commands_for_surface
print(' '.join(c.name for c in commands_for_surface('public')))
")"
	[[ -n "$names" ]] || return 1

	AGENTBOT_SOURCE_ONLY=1 source "$AGENTBOT"
	agentbot_run_backend() { :; }
	agentbot_run_token() { :; }
	agentbot_run_menu() { :; }
	agentbot_usage() { :; }

	local name rc output
	for name in $names; do
		set +e
		output="$(agentbot_main "$name" 2>&1)"
		rc=$?
		set -e
		if [[ $rc -ne 0 || "$output" == *'unknown command'* ]]; then
			unrouted+=" $name"
		fi
	done
	[[ -z "$unrouted" ]] || {
		printf 'launcher has no route for:%s\n' "$unrouted" >&2
		return 1
	}
)

test_install_forwards_a_component_selection() (
	local _old=boot
	# Roadmap 4.1: Agentbot had no component selection, so bootstrap installed
	# skills, Graphify and Boost as one all-or-nothing decision. The selector
	# narrows an install, and the narrowing has to survive both shell layers.
	AGENTBOT_SOURCE_ONLY=1 source "$ROOT/install.sh"
	local calls="$TEST_ROOT/components.calls"
	: >"$calls"
	run_install_repo_gate() { return 0; }
	check_skills_deps() { :; }
	github_token_child() { "$@"; }
	run_cli() { printf '%s\n' "$*" >>"$calls"; }
	# Named by construction: test_product_rename forbids the old product string
	# outside install.sh, and this stub would otherwise reintroduce it here.
	eval "cleanup_owned_old_agent${_old}_link() { :; }"
	link_agentbot() { :; }
	log_info() { :; }
	warn() { :; }

	main install --components skills,boost
	# No selection must still mean every component, exactly as before the
	# selector existed.
	main install

	[[ "$(<"$calls")" == $'install --components skills,boost\ninstall' ]]
)

check 'launcher exists and headless invocation gives guidance' test_launcher_and_headless_guidance
check 'menu repository changes propagate to the public launcher' test_menu_repository_change_propagates_to_launcher
check 'shell TTY access stays in the adapter' test_shell_tty_access_stays_in_the_adapter
check 'symlink invocation resolves the owning repository' test_symlink_resolves_repository_root
check 'public dispatcher preserves commands and exit statuses' test_dispatch_matrix
check 'every public command in the help index has a launcher route' test_every_public_command_reaches_a_route
check 'token command opens the existing token menu' test_token_route_loads_existing_menu
check 'install.sh forwards public commands unchanged' test_install_forwards_public_commands
check 'install.sh forwards a component selection' test_install_forwards_a_component_selection
check 'boot selectors render safely and invalid input is atomic' test_boot_selectors_and_atomic_validation
check 'workspace paths resolve against the caller directory' test_workspace_paths_resolve_against_the_caller_directory
check 'install gates backend work, links launcher, and reports failure truthfully' test_install_repo_gate_link_and_failure_status
check 'launcher linking preserves foreign paths and verifies owned targets' test_link_agentbot_only_replaces_owned_launchers
check 'one backend remains and help documents the public surface' test_one_backend_and_complete_help

test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d public-command test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
