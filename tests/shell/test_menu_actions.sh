#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/lib/harness.sh
source "$ROOT/tests/lib/harness.sh"
test_harness_setup "$ROOT"
AGENTBOT_MENU_SOURCE_ONLY=1 source "$ROOT/scripts/menu.sh"
AGENTBOT_HOME="$ROOT"
export AGENTBOT_HOME

test_harness_report_init

test_status_uses_one_diagnostics_snapshot() (
	local calls="$TEST_ROOT/status.calls"
	: >"$calls"
	agentbot_run_backend() { printf '%s\n' "$*" >>"$calls"; }
	agentbot_menu_status
	[[ "$(<"$calls")" == 'status --doctor' ]]
)

test_main_dispatch_and_pause_ownership() (
	local calls="$TEST_ROOT/menu.calls"
	: >"$calls"
	local index=0
	local -a choices=(status install update prune-skills token workspaces libraries quit)
	agentbot_menu_run() {
		MENU_SIMPLE_RESULT="${choices[$index]}"
		index=$((index + 1))
	}
	tui_clear() { :; }
	tui_pause() { printf 'pause\n' >>"$calls"; }
	agentbot_menu_status() { printf 'status\n' >>"$calls"; }
	# `install` now opens the component selector rather than installing
	# outright, but it stays a direct action: it does not declare pause
	# ownership, so the parent still pauses once for it.
	agentbot_menu_components() { printf 'install\n' >>"$calls"; }
	agentbot_menu_update() { printf 'update\n' >>"$calls"; }
	agentbot_menu_prune_skills() { printf 'prune-skills\n' >>"$calls"; }
	# Submenus own their action pauses and say so; the parent must not add a
	# second one. Direct actions do not, so the parent pauses for them.
	agentbot_menu_token() {
		tui_menu_declare_owns_pause
		printf 'token\n' >>"$calls"
	}
	agentbot_menu_workspaces() {
		tui_menu_declare_owns_pause
		printf 'workspaces\n' >>"$calls"
	}
	agentbot_menu_libraries() {
		tui_menu_declare_owns_pause
		printf 'libraries\n' >>"$calls"
	}
	agentbot_menu_loop
	[[ "$(<"$calls")" == $'status\npause\ninstall\npause\nupdate\npause\nprune-skills\npause\ntoken\nworkspaces\nlibraries' ]]
)

test_prune_skill_menu_removes_only_checked_names_and_refreshes_agents() (
	# Break caught: the checkbox action omits orphaned skills or leaves
	# Cursor's separate lock stale after removing the selected global copies.
	local calls="$TEST_ROOT/prune-skills.calls" prompt="$TEST_ROOT/prune-skills.prompt"
	: >"$calls"
	agentbot_run_backend() {
		if [[ "$*" == 'skills prune --candidates0' ]]; then
			printf 'gpt-taste\0manual\0on disk, not in the lock; user-placed\0'
			printf 'gitlab-ci\0orphaned\0pinned to owner/retired, no active manifest source\0'
			printf 'pitstop\0excluded\0legacy installs it, manifest excludes it\0'
			return 0
		fi
		printf '%s\n' "$*" >>"$calls"
	}
	agentbot_checkbox_run() {
		[[ "${MENU_CB_LABELS[*]}" == 'gpt-taste gitlab-ci pitstop' ]] || return 1
		[[ "${MENU_CB_STATUS[*]}" == 'manual orphaned excluded' ]] || return 1
		[[ "${MENU_CB_DESCS[1]}" == *'owner/retired'* ]] || return 1
		[[ "${MENU_CB_CHECKED[*]}" == '0 0 0' ]] || return 1
		MENU_CB_CHECKED[0]=1
		MENU_CB_CHECKED[1]=1
	}
	tui_confirm() {
		printf '%s\n' "$1" >"$prompt"
		return 0
	}
	tui_run_to_output() { "$@"; }

	agentbot_menu_prune_skills

	[[ "$(<"$calls")" == $'skills prune gpt-taste gitlab-ci --yes\nskills install' ]] || return 1
	[[ "$(<"$prompt")" == 'Permanently prune 2 skills (gpt-taste, gitlab-ci)?' ]]
)

test_prune_skill_menu_does_nothing_when_no_skill_is_checked() (
	# Break caught: pressing Enter on the default screen is interpreted as
	# consent to remove all displayed manual skills.
	local calls="$TEST_ROOT/prune-skills-none.calls"
	: >"$calls"
	agentbot_run_backend() {
		if [[ "$*" == 'skills prune --candidates0' ]]; then
			printf 'gpt-taste\0manual\0on disk, not in the lock; user-placed\0'
			return 0
		fi
		printf '%s\n' "$*" >>"$calls"
	}
	agentbot_checkbox_run() { return 0; }
	tui_confirm() {
		printf 'unexpected confirmation\n' >>"$calls"
		return 0
	}
	tui_run_to_output() { "$@"; }

	agentbot_menu_prune_skills >/dev/null

	[[ ! -s "$calls" ]]
)

test_prune_skill_menu_propagates_discovery_failure() (
	# Break caught: `! command` overwrites the backend status, turning a failed
	# candidate query into a successful and misleading empty list.
	agentbot_run_backend() { return 17; }
	local rc=0
	agentbot_menu_prune_skills >/dev/null || rc=$?
	[[ "$rc" -eq 17 ]]
)

test_repository_change_reaches_the_outer_menu_without_pause() (
	local calls="$TEST_ROOT/repository-change.calls"
	: >"$calls"
	local index=0
	agentbot_menu_run() {
		((index++ == 0)) || return 1
		MENU_SIMPLE_RESULT=update
	}
	tui_clear() { :; }
	tui_pause() { printf 'pause\n' >>"$calls"; }
	agentbot_menu_dispatch() { return 2; }
	local rc=0
	agentbot_menu_loop || rc=$?
	[[ "$rc" -eq 2 && ! -s "$calls" ]]
)

test_tui_install_and_update_preserve_repository_change_exit() (
	# Break caught: an install/update repository change becomes a successful menu
	# return, so the outer agentbot launcher cannot stop the stale process.
	local output="$TEST_ROOT/repository-change.output" update_home="$TEST_ROOT/repository-change-home" rc=0
	mkdir -p "$update_home"
	cat >"$update_home/install.sh" <<'EOF'
#!/usr/bin/env bash
printf 'Repository fast-forward succeeded\n\nRun setup again when ready.\n'
exit 2
EOF
	chmod 700 "$update_home/install.sh"
	agentbot_run_backend() {
		printf 'Repository fast-forward succeeded\n\nRun setup again when ready.\n'
		return 2
	}
	AGENTBOT_TUI=1 AGENTBOT_TUI_OUTPUT="$output" agentbot_menu_install || rc=$?
	[[ "$rc" -eq 2 ]] || return 1
	rc=0
	AGENTBOT_TUI=1 AGENTBOT_TUI_OUTPUT="$output" AGENTBOT_HOME="$update_home" agentbot_menu_update || rc=$?
	[[ "$rc" -eq 2 ]] || return 1
	[[ "$(<"$output")" == $'Repository fast-forward succeeded\n\nRun setup again when ready.\nRepository fast-forward succeeded\n\nRun setup again when ready.' ]]
)

test_tui_update_carries_effective_descriptors_into_python_confirmation() (
	# Break caught: the TUI redirects the update child output but leaves the
	# Python confirmation to reopen unrelated AGENTBOT_UPDATE_TTY_* paths.
	local input="$TEST_ROOT/tui-update-plan.input" output="$TEST_ROOT/tui-update-plan.output"
	local update_home="$TEST_ROOT/tui-update-plan-home" rc=0
	printf 'n\n' >"$input"
	mkdir -p "$update_home"
	cat >"$update_home/install.sh" <<'EOF'
#!/usr/bin/env bash
python3 -c '
from src.cli import confirm_update_plan
if not confirm_update_plan():
    print("Update cancelled.")
'
EOF
	chmod 700 "$update_home/install.sh"
	exec {AGENTBOT_TUI_IN_FD}<"$input"
	exec {AGENTBOT_TUI_OUT_FD}>>"$output"
	AGENTBOT_TUI=1 AGENTBOT_HOME="$update_home" PYTHONPATH="$ROOT" agentbot_menu_update || rc=$?
	exec {AGENTBOT_TUI_IN_FD}<&-
	exec {AGENTBOT_TUI_OUT_FD}>&-
	[[ "$rc" -eq 0 ]] || return 1
	# Both the question and the outcome reach the operator through the menu's
	# output seam, which is where the backend's stdout is relayed to.
	[[ "$(<"$output")" == *'Apply this Agentbot update plan? [y/N]'* && "$(<"$output")" == *'Update cancelled.'* ]]
)

test_workspace_removal_prompt_uses_the_shared_tty_adapter() (
	# Break caught: workspace removal writes directly to /dev/tty, bypassing the
	# injected output path used by a caller or automated terminal session.
	local input="$TEST_ROOT/workspace-remove.input" output="$TEST_ROOT/workspace-remove.output"
	printf 'y\n' >"$input"
	NO_COLOR=1
	tui_init_colors
	AGENTBOT_TUI_INPUT="$input" AGENTBOT_TUI_OUTPUT="$output" \
		agentbot_menu_workspaces_remove_confirm '/tmp/recorded-workspace'
	[[ "$(<"$output")" == $'Stop managing this workspace?\n/tmp/recorded-workspace\nNo workspace files will be changed. [y/N]: ' ]]
)

test_command_lib_selects_one_detail() (
	local capture="$TEST_ROOT/command-lib.capture" output calls=0
	# The menu is derived from the command metadata now, so what this asserts is
	# which menu was asked for and what came back -- the entries themselves are
	# checked against that metadata below.
	agentbot_menu_run() {
		calls=$((calls + 1))
		if ((calls == 1)); then
			printf '%s\n' "$1" >"$capture"
			MENU_SIMPLE_RESULT='boot'
			return 0
		fi
		return 1
	}
	tui_clear() { :; }
	tui_wait_back() { :; }
	output="$(AGENTBOT_MENU_COLS=80 agentbot_menu_command_lib)"
	[[ "$(<"$capture")" == command_lib ]] || return 1
	[[ "$output" == *'Agentbot › Help › boot'* && "$output" == *'agentbot boot'* ]] || return 1

	# And the derived menu carries the public commands plus the way into the
	# bootstrap ones: a command added to src/commands.py appears here with
	# nothing edited, which is the reason it is derived at all.
	agentbot_menu_load command_lib || return 1
	[[ "${MENU_SIMPLE_LABELS[*]}" == *'status [read-only]'* ]] || return 1
	[[ "${MENU_SIMPLE_LABELS[*]}" == *'Bootstrap commands'* ]] || return 1
	[[ "${MENU_SIMPLE_KEYS[*]}" == *__bootstrap__* ]]
)

test_graphify_library_is_data_driven_and_supported() (
	local output step=0 fake_help
	agentbot_menu_run() {
		step=$((step + 1))
		case "$step" in
		1) MENU_SIMPLE_RESULT=assistant ;;
		2) MENU_SIMPLE_RESULT='/graphify query "what connects auth to the database?"' ;;
		3) return 1 ;;
		4) MENU_SIMPLE_RESULT=boundary ;;
		*) return 1 ;;
		esac
	}
	tui_clear() { :; }
	tui_wait_back() { :; }
	output="$(AGENTBOT_MENU_COLS=100 agentbot_menu_graphify_lib)"
	[[ "$output" == *'/graphify query "what connects auth to the database?"'* ]] || return 1
	[[ "$output" == *'graphify install --platform agents'* ]] || return 1
	fake_help=$'Commands:\n  install [--platform P]\n  extract <path>\n  update <path>\n  cluster-only <path>\n  query "<question>"\n  path "A" "B"\n  explain "X"\n  export callflow-html\n  hook status\n  merge-graphs <g1> <g2>'
	agentbot_graphify_validate_rows "$fake_help"
)

test_token_entry_is_hidden_and_reveal_requires_confirmation() (
	local token='saved_token_value_1234567890' input="$TEST_ROOT/token.input" output="$TEST_ROOT/token.output"
	local reveal_input="$TEST_ROOT/reveal.input" reveal_output="$TEST_ROOT/reveal.output" fingerprint
	printf 's\n%s\ny\nq\n' "$token" >"$input"
	NO_COLOR=1
	tui_init_colors
	AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" agentbot_token_config_menu
	fingerprint="$(github_token_fingerprint "$token")"
	[[ "$(<"$output")" == *'Input is hidden; only its fingerprint will be shown.'* ]] || return 1
	[[ "$(<"$output")" == *"$fingerprint"* && "$(<"$output")" != *"$token"* ]] || return 1
	printf 'r\nn\nq\n' >"$reveal_input"
	unset NO_COLOR
	tui_init_colors
	AGENTBOT_TOKEN_TTY_INPUT="$reveal_input" AGENTBOT_TOKEN_TTY_OUTPUT="$reveal_output" agentbot_token_config_menu
	[[ "$(<"$reveal_output")" == *$'\033[31mWARNING: the full token will be printed once on this terminal.\033[0m'* ]] || return 1
	[[ "$(<"$reveal_output")" != *"$token"* ]]
)

test_token_menu_outcomes_survive_the_redraw() (
	# Break caught by comparing this screen with the Dotfiles one: every
	# outcome was printed and then wiped, because the loop clears and
	# re-renders before the operator can read it. Only the reveal survived,
	# and only because it pauses. Carried into the next frame instead.
	local input="$TEST_ROOT/token-status.input" output="$TEST_ROOT/token-status.output"
	NO_COLOR=1
	tui_init_colors

	# An unrecognised key, then quit: the notice must be on the frame drawn
	# after it, not lost with the frame it was printed on.
	printf 'z\nq\n' >"$input"
	AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" agentbot_token_config_menu
	[[ "$(<"$output")" == *'Invalid choice.'* ]] || return 1
	# It appears once, under the options of the following frame.
	[[ "$(grep -c 'Invalid choice.' "$output")" -eq 1 ]] || return 1
	[[ "$(<"$output")" == *$'q Back\n\n  Invalid choice.'* ]] || return 1

	# Removing with nothing saved reports through the same path.
	local removed_input="$TEST_ROOT/token-remove.input" removed_output="$TEST_ROOT/token-remove.output"
	github_token_remove >/dev/null 2>&1 || true
	printf 'd\nq\n' >"$removed_input"
	AGENTBOT_TOKEN_TTY_INPUT="$removed_input" AGENTBOT_TOKEN_TTY_OUTPUT="$removed_output" agentbot_token_config_menu
	[[ "$(<"$removed_output")" == *'No saved token file exists.'* ]]
)

test_saved_token_can_be_checked_from_the_menu() (
	# Saving checks what is being typed; nothing checked what was already
	# saved, and a token that was good when it was written is exactly the thing
	# that expires or gets revoked later. Same action, same words, as Dotfiles.
	local token='saved_token_value_1234567890'
	local input="$TEST_ROOT/check-saved.input" output="$TEST_ROOT/check-saved.output"
	NO_COLOR=1
	tui_init_colors
	github_token_write "$token" || return 1

	local rc
	for rc in 0 1 2; do
		printf 'c\nq\n' >"$input"
		: >"$output"
		(
			eval "github_token_verify() { return $rc; }"
			AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" \
				agentbot_token_config_menu
		) || return 1
		case "$rc" in
		0) grep -Fq 'GitHub accepted the saved token.' "$output" || return 1 ;;
		1) grep -Fq 'GitHub rejected the saved token' "$output" || return 1 ;;
		*) grep -Fq 'Could not reach GitHub to check the saved token.' "$output" || return 1 ;;
		esac
		# Only the render prints an outcome, so its presence proves it was
		# carried; and the saved token never appears to get there.
		! grep -Fq "$token" "$output" || return 1
	done

	# Nothing saved is not an error, and asks GitHub nothing.
	github_token_remove >/dev/null 2>&1 || true
	printf 'c\nq\n' >"$input"
	: >"$output"
	(
		github_token_verify() {
			printf 'must not be asked\n' >>"$output"
			return 0
		}
		AGENTBOT_TOKEN_TTY_INPUT="$input" AGENTBOT_TOKEN_TTY_OUTPUT="$output" \
			agentbot_token_config_menu
	) || return 1
	grep -Fq 'No valid saved token to check.' "$output" || return 1
	! grep -Fq 'must not be asked' "$output"
)

test_workspaces_routes_read_preview_and_apply() (
	local calls="$TEST_ROOT/workspaces.calls"
	: >"$calls"
	local index=0
	local -a choices=(list preview apply back)
	agentbot_menu_run() {
		local choice="${choices[$index]}"
		index=$((index + 1))
		[[ "$choice" != back ]] || return 1
		MENU_SIMPLE_RESULT="$choice"
	}
	tui_clear() { :; }
	tui_pause() { printf 'pause\n' >>"$calls"; }
	agentbot_menu_workspaces_confirm() { return 0; }
	agentbot_run_backend() { printf 'backend:%s\n' "$*" >>"$calls"; }
	agentbot_menu_workspaces
	[[ "$(<"$calls")" == $'backend:workspaces\npause\nbackend:resync --all\npause\nbackend:resync --all --yes\npause' ]]
)

test_declined_workspace_apply_is_non_destructive() (
	local calls="$TEST_ROOT/workspace-decline.calls"
	: >"$calls"
	agentbot_menu_workspaces_confirm() { return 1; }
	agentbot_run_backend() { printf 'backend:%s\n' "$*" >>"$calls"; }
	agentbot_menu_workspaces_dispatch apply >/dev/null
	[[ ! -s "$calls" ]]
)

test_failed_actions_are_red() (
	unset NO_COLOR
	tui_init_colors
	agentbot_menu_status() { return 17; }
	local output rc
	set +e
	output="$(agentbot_menu_dispatch status 2>&1)"
	rc=$?
	set -e
	[[ "$rc" -eq 17 && "$output" == *$'\033[31mAction failed (exit 17).\033[0m'* ]]
)

test_undeclared_submenu_still_gets_a_parent_pause() (
	local calls="$TEST_ROOT/undeclared-pause.calls"
	: >"$calls"
	local index=0
	local -a choices=(libraries quit)
	agentbot_menu_run() {
		MENU_SIMPLE_RESULT="${choices[$index]}"
		index=$((index + 1))
	}
	tui_clear() { :; }
	tui_pause() { printf 'pause\n' >>"$calls"; }
	agentbot_menu_libraries() { printf 'libraries\n' >>"$calls"; }
	agentbot_menu_loop
	[[ "$(<"$calls")" == $'libraries\npause' ]]
)
check 'Status uses one diagnostics snapshot' test_status_uses_one_diagnostics_snapshot
test_component_selector_matches_the_sibling_layout() (
	# The wide layout reserves sixteen columns for a status these rows do not
	# have, and draws the `·` separators around the gap:
	#
	#   >  1. [x] ·                  · Skills
	#
	# The sibling product's component selector is compact and has always been.
	_agentbot_components_prepare
	[[ "${MENU_CB_COMPACT:-}" == true ]] || return 1

	# And the descriptions existed from the day this menu was added but were
	# never handed to the renderer, so the footer under the selection was blank.
	[[ "${#MENU_CB_DESCS[@]}" -eq "${#AGENTBOT_COMPONENT_KEYS[@]}" ]] || return 1
	[[ "${MENU_CB_DESCS[0]}" == "${AGENTBOT_COMPONENT_DESCS[0]}" ]]
)

test_component_selector_carries_nothing_over_from_prune() (
	# Prune Skills fills MENU_CB_DESCS and this menu did not clear it, so
	# opening Install straight after Prune put prune descriptions under the
	# component rows -- and four of them against three labels, so the footer
	# named the wrong thing for every row.
	MENU_CB_DESCS=('prune one' 'prune two' 'prune three' 'prune four')
	MENU_CB_TOGGLE_FN=_prune_toggle
	MENU_CB_DESC_FN=_prune_desc

	_agentbot_components_prepare

	[[ "${#MENU_CB_DESCS[@]}" -eq "${#AGENTBOT_COMPONENT_KEYS[@]}" ]] || return 1
	[[ "${MENU_CB_DESCS[*]}" != *prune* ]] || return 1
	# The hooks matter more than the text: left set, they force the checkbox
	# onto the Bash fallback loop and call Prune's callbacks on a toggle here.
	[[ -z "${MENU_CB_TOGGLE_FN:-}" && -z "${MENU_CB_DESC_FN:-}" ]]
)

test_component_selector_keeps_the_install_contracts() (
	# The selector was written calling agentbot_run_backend directly, which went
	# around the two things agentbot_menu_install owns: the TUI output seam and
	# the exit-3 repository-change contract. A repository change then surfaced
	# as a failed action instead of a clean restart.
	#
	# The gate runs first now, so the calls are the gate and then the install.
	local calls="$TEST_ROOT/selector.calls" rc=0
	: >"$calls"
	agentbot_run_backend() {
		printf '%s\n' "$*" >>"$calls"
		[[ "$*" == 'install' ]] && return 0
		return 3
	}
	agentbot_checkbox_run() {
		MENU_CB_CHECKED=(1 0 1)
		return 0
	}
	agentbot_menu_components >/dev/null 2>&1 || rc=$?

	[[ "$(<"$calls")" == $'install\ninstall --components skills,boost' ]] || return 1
	[[ "$rc" -eq 0 ]]
)

test_component_selector_asks_about_the_repository_first() (
	# The operator chose components and was then asked to pull, and pulling
	# moves the checkout and restarts the run -- so the selection was thrown
	# away. Dotfiles asks first; this now does too.
	local calls="$TEST_ROOT/selector-order.calls" rc=0

	# A declined pull: the gate says so, and the selector never opens.
	: >"$calls"
	agentbot_run_backend() {
		printf 'gate %s\n' "$*" >>"$calls"
		return 3
	}
	agentbot_checkbox_run() {
		printf 'selector\n' >>"$calls"
		MENU_CB_CHECKED=(1 1 1)
		return 0
	}
	agentbot_menu_components >/dev/null 2>&1 || rc=$?
	[[ "$(<"$calls")" == 'gate install' && "$rc" -eq 0 ]] || return 1

	# A moved checkout: the caller restarts into the new code, so the selector
	# must not open on the old one either.
	: >"$calls"
	rc=0
	agentbot_run_backend() {
		printf 'gate %s\n' "$*" >>"$calls"
		return 2
	}
	agentbot_menu_components >/dev/null 2>&1 || rc=$?
	[[ "$(<"$calls")" == 'gate install' && "$rc" -eq 2 ]]
)

test_component_selector_cancels_when_nothing_is_checked() (
	local calls="$TEST_ROOT/selector-none.calls" rc=0
	: >"$calls"
	agentbot_run_backend() { printf '%s\n' "$*" >>"$calls"; }
	agentbot_checkbox_run() {
		MENU_CB_CHECKED=(0 0 0)
		return 0
	}
	agentbot_menu_components >/dev/null 2>&1 || rc=$?
	# The gate ran and nothing else did: an empty selection installs nothing.
	[[ "$(<"$calls")" == 'install' && "$rc" -eq 0 ]]
)

check 'main dispatch gives direct actions exactly one pause' test_main_dispatch_and_pause_ownership
check 'component selector uses the sibling compact layout' test_component_selector_matches_the_sibling_layout
check 'component selector carries nothing over from prune' test_component_selector_carries_nothing_over_from_prune
check 'component selector keeps the install contracts' test_component_selector_keeps_the_install_contracts
check 'component selector asks about the repository first' test_component_selector_asks_about_the_repository_first
check 'component selector cancels when nothing is checked' test_component_selector_cancels_when_nothing_is_checked
check 'prune skill menu removes only checked names and refreshes agents' test_prune_skill_menu_removes_only_checked_names_and_refreshes_agents
check 'prune skill menu leaves state unchanged when nothing is checked' test_prune_skill_menu_does_nothing_when_no_skill_is_checked
check 'prune skill menu propagates candidate discovery failures' test_prune_skill_menu_propagates_discovery_failure
check 'a submenu that does not declare pause ownership still gets one' test_undeclared_submenu_still_gets_a_parent_pause
check 'repository changes reach the outer menu without a stale pause' test_repository_change_reaches_the_outer_menu_without_pause
check 'TUI install and update preserve the repository-change exit contract' test_tui_install_and_update_preserve_repository_change_exit
check 'TUI update carries effective descriptors into Python confirmation' test_tui_update_carries_effective_descriptors_into_python_confirmation
check 'Command Lib selects and renders one detail page' test_command_lib_selects_one_detail
check 'Graphify Lib rows match supported command families' test_graphify_library_is_data_driven_and_supported
check 'token entry is hidden and reveal is confirmed' test_token_entry_is_hidden_and_reveal_requires_confirmation
check 'token menu outcomes survive the redraw' test_token_menu_outcomes_survive_the_redraw
check 'the saved token can be checked against GitHub from the menu' test_saved_token_can_be_checked_from_the_menu
check 'Workspaces routes list preview and confirmed apply' test_workspaces_routes_read_preview_and_apply
check 'declined workspace apply performs no backend write' test_declined_workspace_apply_is_non_destructive
check 'workspace removal prompts use the shared TTY adapter' test_workspace_removal_prompt_uses_the_shared_tty_adapter
check 'failed menu actions use the shared red treatment' test_failed_actions_are_red

test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d menu-action test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
