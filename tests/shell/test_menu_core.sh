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

strip_ansi_stream() { sed $'s/\033\\[[0-9;?]*[A-Za-z]//g; s/[[:space:]]*$//'; }

test_main_menu_snapshot() {
	local output
	_agentbot_menu_setup
	output="$(AGENTBOT_TUI=1 tui_menu_draw 0 80 | strip_ansi_stream)"
	[[ "$output" == *$'=== Agentbot ===\n  Agentbot'* ]] || return 1
	# Eight entries, not nine: Platform's seven actions became install
	# components and status rows, so the submenu that held them is gone.
	[[ "$output" == *'1. Check Status'* && "$output" == *'8. Quit'* ]] || return 1
	[[ "$output" == *'Prune Skills'* && "$output" == *'GitHub Token Config'* ]] || return 1
	[[ "$output" != *'Platform'* ]] || return 1
	[[ "$output" == *'Libraries'* ]] || return 1
	[[ "$output" == *'Check the installed Agentbot components, editors, and baseline.'* ]]
}

test_width_and_palette_snapshots() (
	local cols output plain line
	for cols in 48 80 120; do
		NO_COLOR=1
		tui_init_colors
		output="$({
			tui_header 'Check Status' 'Agentbot › Check Status' "$cols"
			tui_section Health "$cols"
		} | strip_ansi_stream)"
		[[ "$output" == *'=== Check Status ==='* && "$output" == *'Health'* ]] || return 1
		while IFS= read -r line; do ((${#line} <= cols)) || return 1; done <<<"$output"

		unset NO_COLOR
		tui_init_colors
		output="$(
			tui_header 'Check Status' 'Agentbot › Check Status' "$cols"
			tui_section Health "$cols"
		)"
		[[ "$output" == *$'\033[1m\033[38;5;208m=== Check Status ==='* ]] || return 1
		[[ "$output" == *$'\033[1m\033[33mHealth'* ]] || return 1
		while IFS= read -r line; do
			plain="$(strip_ansi_stream <<<"$line")"
			((${#plain} <= cols)) || return 1
		done <<<"$output"
	done
)

test_draw_clears_line_tails_and_matches_frame_height() (
	_agentbot_menu_setup
	local output frame
	output="$(tui_menu_draw 0 80)"
	frame="$(tui_menu_lines 0)"
	[[ "$output" == *$'\033[K\n'* ]] || return 1
	[[ "$frame" -eq "$(printf '%s\n' "$output" | wc -l)" ]] || return 1
	# Cursor control goes to the terminal adapter rather than stdout, so that
	# it shares a stream with the menu body it positions. Capture the seam.
	local capture="$TEST_ROOT/redraw-capture"
	: >"$capture"
	DOTFILES_TTY_OUTPUT="$capture" tui_redraw_up "$frame"
	[[ "$(cat "$capture")" == $'\033['"${frame}"'A' ]]
)

test_shortcuts_use_cyan_tokens() (
	unset NO_COLOR
	tui_init_colors
	local output
	output="$(tui_color_input_hint 'Up/Down navigate   Enter confirm   q back')"
	[[ "$output" == *$'\033[36mUp/Down'* ]] || return 1
	[[ "$output" == *$'\033[36mEnter'* ]] || return 1
	[[ "$output" == *$'\033[36mq'* ]]
)

test_tui_backend_output_colors_semantic_markers_only() (
	# Break caught: backend progress markers remain plain white in the TUI, or
	# coloring spills across the descriptive text that follows each marker.
	local output="$TEST_ROOT/tui-markers.output" expected
	unset NO_COLOR
	tui_init_colors
	_emit_markers() {
		printf '%s\n' \
			'  [STEP] Installing source' \
			'  [OK] Source installed' \
			'  [FAIL] Source failed' \
			'  [ERR] Invalid source' \
			'  [WARN] Source skipped' \
			'  [INFO] Source detail' \
			'  [SKIP] Skipping Boost integration' \
			'  [Legend] STEP=starting  OK=completed  SKIP=already satisfied  WARN=needs attention' \
			'  ordinary output'
	}

	AGENTBOT_TUI_OUTPUT="$output" tui_run_to_output _emit_markers

	# [SKIP] is dim, matching the result vocabulary: a skip is a deliberate
	# non-event. It was missing from the substitutions while the backend printed
	# it for a deselected component, so the one marker meaning "nothing
	# happened" was drawn plainest of all.
	#
	# The legend names the four markers without bracketing them, so it is
	# coloured word by word -- a legend whose colours are absent is not a key.
	expected="  ${C_BOLD}${C_CYAN}[STEP]${C_RESET} Installing source
  ${C_BOLD}${C_GREEN}[OK]${C_RESET} Source installed
  ${C_BOLD}${C_RED}[FAIL]${C_RESET} Source failed
  ${C_BOLD}${C_RED}[ERR]${C_RESET} Invalid source
  ${C_BOLD}${C_YELLOW}[WARN]${C_RESET} Source skipped
  ${C_CYAN}[INFO]${C_RESET} Source detail
  ${C_DIM}[SKIP]${C_RESET} Skipping Boost integration
  [Legend] ${C_CYAN}STEP=starting${C_RESET}  ${C_GREEN}OK=completed${C_RESET}  ${C_DIM}SKIP=already satisfied${C_RESET}  ${C_YELLOW}WARN=needs attention${C_RESET}
  ordinary output"
	[[ "$(<"$output")" == "$expected" ]]
)

test_tui_backend_output_respects_no_color() (
	# Break caught: semantic markers inject ANSI even when NO_COLOR explicitly
	# requests stable plain output for logs and accessibility, or the stream
	# adapter invents a final newline the child never wrote.
	local output="$TEST_ROOT/tui-markers-no-color.output"
	local expected="$TEST_ROOT/tui-markers-no-color.expected"
	NO_COLOR=1
	tui_init_colors
	_emit_plain_markers() { printf '  [STEP] Installing source\n  [OK] Source installed'; }
	printf '  [STEP] Installing source\n  [OK] Source installed' >"$expected"

	AGENTBOT_TUI_OUTPUT="$output" tui_run_to_output _emit_plain_markers

	cmp -s "$expected" "$output"
)

test_tui_backend_output_preserves_child_failure_status() (
	# Break caught: adding a coloring pipeline turns a failed install into a
	# successful menu action by returning the formatter's status.
	local output="$TEST_ROOT/tui-markers-failure.output" rc=0
	unset NO_COLOR
	tui_init_colors
	_emit_failure() {
		printf '  [FAIL] Source failed\n'
		return 17
	}

	AGENTBOT_TUI_OUTPUT="$output" tui_run_to_output _emit_failure || rc=$?

	[[ "$rc" -eq 17 ]] || return 1
	[[ "$(<"$output")" == "  ${C_BOLD}${C_RED}[FAIL]${C_RESET} Source failed" ]]
)

test_pause_uses_one_blank_line_and_shared_prompt() (
	local input="$TEST_ROOT/pause.input" output="$TEST_ROOT/pause.output"
	printf '\n' >"$input"
	NO_COLOR=1
	tui_init_colors
	AGENTBOT_TUI_INPUT="$input" AGENTBOT_TUI_OUTPUT="$output" tui_pause
	[[ "$(<"$output")" == $'\n  Press Enter to continue: ' ]]
)

test_refresh_preserves_agentbot_and_dotfiles_tty_overrides_for_pause_and_confirm() (
	# Break caught: tui_refresh_tty_seam discards DOTFILES_* after the initial
	# load, sending a later prompt to /dev/tty instead of the caller's stream.
	local agent_pause_in="$TEST_ROOT/agent-pause.input" agent_pause_out="$TEST_ROOT/agent-pause.output"
	local agent_confirm_in="$TEST_ROOT/agent-confirm.input" agent_confirm_out="$TEST_ROOT/agent-confirm.output"
	local dot_pause_in="$TEST_ROOT/dot-pause.input" dot_pause_out="$TEST_ROOT/dot-pause.output"
	local dot_confirm_in="$TEST_ROOT/dot-confirm.input" dot_confirm_out="$TEST_ROOT/dot-confirm.output"
	printf '\n' >"$agent_pause_in"
	printf 'y\n' >"$agent_confirm_in"
	printf '\n' >"$dot_pause_in"
	printf 'y\n' >"$dot_confirm_in"
	NO_COLOR=1
	tui_init_colors
	AGENTBOT_TUI_INPUT="$agent_pause_in" AGENTBOT_TUI_OUTPUT="$agent_pause_out" tui_pause
	AGENTBOT_TUI_INPUT="$agent_confirm_in" AGENTBOT_TUI_OUTPUT="$agent_confirm_out" tui_confirm 'Apply changes'
	[[ "$(<"$agent_pause_out")" == $'\n  Press Enter to continue: ' ]] || {
		printf 'agent pause output: %q\n' "$(<"$agent_pause_out")" >&2
		return 1
	}
	[[ "$(<"$agent_confirm_out")" == '  Apply changes [y/N]: ' ]] || {
		printf 'agent confirm output: %q\n' "$(<"$agent_confirm_out")" >&2
		return 1
	}
	NO_COLOR=1 DOTFILES_TTY_INPUT="$dot_pause_in" DOTFILES_TTY_OUTPUT="$dot_pause_out" bash -c '
		set -euo pipefail
		source "$1/scripts/lib/tui.sh"
		tui_pause
	' _ "$ROOT"
	NO_COLOR=1 DOTFILES_TTY_INPUT="$dot_confirm_in" DOTFILES_TTY_OUTPUT="$dot_confirm_out" bash -c '
		set -euo pipefail
		source "$1/scripts/lib/tui.sh"
		tui_confirm "Apply changes"
	' _ "$ROOT"
	[[ "$(<"$dot_pause_out")" == $'\n  Press Enter to continue: ' ]] || {
		printf 'dotfiles pause output: %q\n' "$(<"$dot_pause_out")" >&2
		return 1
	}
	[[ "$(<"$dot_confirm_out")" == '  Apply changes [y/N]: ' ]] || {
		printf 'dotfiles confirm output: %q\n' "$(<"$dot_confirm_out")" >&2
		return 1
	}
)

test_refresh_restores_immutable_dotfiles_paths_and_descriptors_after_agentbot_overrides() (
	# Break caught: refresh reads its prior effective values as fallbacks, making
	# the last Agentbot override survive after that override is unset.
	local base_in="$TEST_ROOT/base.input" base_out="$TEST_ROOT/base.output"
	local override_a_in="$TEST_ROOT/override-a.input" override_a_out="$TEST_ROOT/override-a.output"
	local override_b_in="$TEST_ROOT/override-b.input" override_b_out="$TEST_ROOT/override-b.output"
	local output
	: >"$base_in"
	: >"$base_out"
	: >"$override_a_in"
	: >"$override_a_out"
	: >"$override_b_in"
	: >"$override_b_out"
	output="$(DOTFILES_TTY_INPUT="$base_in" DOTFILES_TTY_OUTPUT="$base_out" bash -c '
		set -euo pipefail
		exec {base_in}<"$1"; exec {base_out}>>"$2"
		exec {a_in}<"$3"; exec {a_out}>>"$4"
		exec {b_in}<"$5"; exec {b_out}>>"$6"
		DOTFILES_TTY_IN_FD="$base_in" DOTFILES_TTY_OUT_FD="$base_out"
		source "$7/scripts/lib/tui.sh"
		AGENTBOT_TUI_INPUT="$3" AGENTBOT_TUI_OUTPUT="$4" AGENTBOT_TUI_IN_FD="$a_in" AGENTBOT_TUI_OUT_FD="$a_out" tui_refresh_tty_seam
		[[ "$DOTFILES_TTY_IN_FD" == "$a_in" && "$DOTFILES_TTY_OUT_FD" == "$a_out" ]]
		printf "A:%s:%s:%s:%s\\n" "$DOTFILES_TTY_INPUT" "$DOTFILES_TTY_OUTPUT" "$DOTFILES_TTY_IN_FD" "$DOTFILES_TTY_OUT_FD"
		AGENTBOT_TUI_INPUT="$5" AGENTBOT_TUI_OUTPUT="$6" AGENTBOT_TUI_IN_FD="$b_in" AGENTBOT_TUI_OUT_FD="$b_out" tui_refresh_tty_seam
		[[ "$DOTFILES_TTY_IN_FD" == "$b_in" && "$DOTFILES_TTY_OUT_FD" == "$b_out" ]]
		printf "B:%s:%s:%s:%s\\n" "$DOTFILES_TTY_INPUT" "$DOTFILES_TTY_OUTPUT" "$DOTFILES_TTY_IN_FD" "$DOTFILES_TTY_OUT_FD"
		unset AGENTBOT_TUI_INPUT AGENTBOT_TUI_OUTPUT AGENTBOT_TUI_IN_FD AGENTBOT_TUI_OUT_FD
		tui_refresh_tty_seam
		[[ "$DOTFILES_TTY_IN_FD" == "$base_in" && "$DOTFILES_TTY_OUT_FD" == "$base_out" ]]
		printf "BASE:%s:%s:%s:%s\\n" "$DOTFILES_TTY_INPUT" "$DOTFILES_TTY_OUTPUT" "$DOTFILES_TTY_IN_FD" "$DOTFILES_TTY_OUT_FD"
	' _ "$base_in" "$base_out" "$override_a_in" "$override_a_out" "$override_b_in" "$override_b_out" "$ROOT")"
	[[ "$output" == *"A:$override_a_in:$override_a_out:"* && "$output" == *"B:$override_b_in:$override_b_out:"* ]] || return 1
	[[ "$output" == *"BASE:$base_in:$base_out:"* ]]
)

test_token_confirm_returns_to_the_parent_tty_after_its_descriptors_close() (
	# Break caught: a token confirmation leaves its closed scoped descriptor in
	# DOTFILES_TTY_* and the parent menu cannot read its next pause.
	local parent_input="$TEST_ROOT/token-parent.input" parent_output="$TEST_ROOT/token-parent.output"
	local token_input="$TEST_ROOT/token-confirm.input" token_output="$TEST_ROOT/token-confirm.output" output
	printf '\n' >"$parent_input"
	printf 'y\n' >"$token_input"
	output="$(NO_COLOR=1 DOTFILES_TTY_INPUT="$parent_input" DOTFILES_TTY_OUTPUT="$parent_output" bash -c '
		set -euo pipefail
		exec {parent_in}<"$1"; exec {parent_out}>>"$2"
		exec {token_in}<"$3"; exec {token_out}>>"$4"
		DOTFILES_TTY_IN_FD="$parent_in" DOTFILES_TTY_OUT_FD="$parent_out"
		source "$5/scripts/menu.sh"
		AGENTBOT_TOKEN_MENU_IN_FD="$token_in" AGENTBOT_TOKEN_MENU_OUT_FD="$token_out"
		_agentbot_token_menu_confirm "Confirm token" || exit 1
		exec {token_in}<&-; exec {token_out}>&-
		tui_pause
		printf "parent=%s token=%s\\n" "$(<"$2")" "$(<"$4")"
	' _ "$parent_input" "$parent_output" "$token_input" "$token_output" "$ROOT")"
	[[ "$output" == *'parent='$'\n  Press Enter to continue: '* && "$output" == *'token=  Confirm token [y/N]: '* ]] || {
		printf 'token return output: %q\n' "$output" >&2
		return 1
	}
)

test_command_details_fit_narrow_terminals() (
	local output line
	output="$(NO_COLOR=1 AGENTBOT_MENU_COLS=48 "$ROOT/bin/agentbot" help)" || return 1
	while IFS= read -r line; do ((${#line} <= 48)) || return 1; done <<<"$output"
)

test_menu_arrays_stay_aligned() {
	# Three parallel arrays describe one menu. A key added without its label or
	# description shifts every entry below it onto the wrong action.
	_agentbot_menu_setup
	((${#MENU_SIMPLE_LABELS[@]} == ${#MENU_SIMPLE_KEYS[@]})) || return 1
	((${#MENU_SIMPLE_LABELS[@]} == ${#MENU_SIMPLE_DESCS[@]}))
}

test_every_platform_surface_is_an_install_component() (
	# The three used to be a Platform submenu of seven entries, so an install
	# could finish having left the machine's editor configuration untouched and
	# say nothing about it. They are install components now: the selector offers
	# them, and Lifecycle installs whatever the selector returns.
	local key
	_agentbot_components_prepare
	for key in vscode cursor cli-config; do
		[[ " ${AGENTBOT_COMPONENT_KEYS[*]} " == *" $key "* ]] || return 1
	done
	# Every key the selector offers must be one the backend knows, or a chosen
	# component would be silently dropped.
	local known
	known="$(cd "$ROOT" && python3 -c 'from src.lifecycle import Lifecycle
print(" ".join(Lifecycle.SELECTABLE_COMPONENTS))')" || return 1
	for key in "${AGENTBOT_COMPONENT_KEYS[@]}"; do
		[[ " $known " == *" $key "* ]] || return 1
	done
	# And the labels and descriptions stay aligned with the keys.
	((${#AGENTBOT_COMPONENT_KEYS[@]} == ${#AGENTBOT_COMPONENT_LABELS[@]})) || return 1
	((${#AGENTBOT_COMPONENT_KEYS[@]} == ${#AGENTBOT_COMPONENT_DESCS[@]}))
)

check 'main menu snapshot uses the unified labels and breadcrumb' test_main_menu_snapshot
check 'menu label, key, and description arrays stay aligned' test_menu_arrays_stay_aligned
check 'every platform surface is an install component' test_every_platform_surface_is_an_install_component
check 'TUI frames fit 48, 80, and 120 columns with the shared palette' test_width_and_palette_snapshots
check 'menu redraw frames clear stale tails and preserve height' test_draw_clears_line_tails_and_matches_frame_height
check 'shortcut tokens use the shared cyan treatment' test_shortcuts_use_cyan_tokens
check 'TUI backend output colors semantic markers only' test_tui_backend_output_colors_semantic_markers_only
check 'TUI backend output respects NO_COLOR' test_tui_backend_output_respects_no_color
check 'TUI backend output preserves child failure status' test_tui_backend_output_preserves_child_failure_status
check 'pause uses one blank line and one shared prompt' test_pause_uses_one_blank_line_and_shared_prompt
check 'TTY refresh preserves Agentbot and Dotfiles overrides for pause and confirm' test_refresh_preserves_agentbot_and_dotfiles_tty_overrides_for_pause_and_confirm
check 'TTY refresh restores immutable Dotfiles paths and descriptors after Agentbot overrides' test_refresh_restores_immutable_dotfiles_paths_and_descriptors_after_agentbot_overrides
check 'token confirmation returns to the parent TTY after scoped descriptors close' test_token_confirm_returns_to_the_parent_tty_after_its_descriptors_close
check 'command details fit a 48-column terminal' test_command_details_fit_narrow_terminals

test_harness_verify_safety || failed=$((failed + 1))
printf '\nRan %d menu-core test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
test_harness_cleanup || failed=$((failed + 1))
((failed == 0))
