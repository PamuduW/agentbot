#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC1091,SC2034  # Loader paths are rooted beside this file; palette globals are published.

# Agentbot TUI.
#
# The implementation is the shared terminal stack in scripts/lib/shared/tui/,
# which is kept byte-identical with the sibling repository (see
# scripts/sync-shared.sh; the gate fails if the copies diverge). This file is
# the Agentbot-facing naming layer over it, so existing tui_* callers and the
# AGENTBOT_* environment seams keep working.

_AGENTBOT_TUI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/shared/tui" && pwd)"

# One TTY adapter, two addressing forms. Preserve the caller's Dotfiles seam
# before resolving Agentbot overrides: refresh must restore that stable base
# after a scoped token/menu override ends.
_AGENTBOT_TUI_BASE_INPUT="${DOTFILES_TTY_INPUT:-/dev/tty}"
_AGENTBOT_TUI_BASE_OUTPUT="${DOTFILES_TTY_OUTPUT:-/dev/tty}"
_AGENTBOT_TUI_BASE_IN_FD="${DOTFILES_TTY_IN_FD:-}"
_AGENTBOT_TUI_BASE_OUT_FD="${DOTFILES_TTY_OUT_FD:-}"
DOTFILES_TTY_INPUT="${AGENTBOT_TUI_INPUT:-$_AGENTBOT_TUI_BASE_INPUT}"
DOTFILES_TTY_OUTPUT="${AGENTBOT_TUI_OUTPUT:-$_AGENTBOT_TUI_BASE_OUTPUT}"
DOTFILES_TTY_IN_FD="${AGENTBOT_TUI_IN_FD:-$_AGENTBOT_TUI_BASE_IN_FD}"
DOTFILES_TTY_OUT_FD="${AGENTBOT_TUI_OUT_FD:-$_AGENTBOT_TUI_BASE_OUT_FD}"
export DOTFILES_TTY_INPUT DOTFILES_TTY_OUTPUT DOTFILES_TTY_IN_FD DOTFILES_TTY_OUT_FD

source "$_AGENTBOT_TUI_DIR/colors.sh"
source "$_AGENTBOT_TUI_DIR/tty.sh"
source "$_AGENTBOT_TUI_DIR/menu_render.sh"
source "$_AGENTBOT_TUI_DIR/report_table.sh"
source "$_AGENTBOT_TUI_DIR/ui.sh"
source "$_AGENTBOT_TUI_DIR/menu_descriptions.sh"
source "$_AGENTBOT_TUI_DIR/menu_keys.sh"
source "$_AGENTBOT_TUI_DIR/menu_simple.sh"
source "$_AGENTBOT_TUI_DIR/menu_paging.sh"
source "$_AGENTBOT_TUI_DIR/menu_checkbox.sh"
source "$_AGENTBOT_TUI_DIR/menu_runner.sh"

# Submenus accept `q` to go back and never said so, while the sibling
# repository's submenus have always advertised it. Same shared runner, two
# different levels of discoverability -- roadmap 4.1 asks for navigation
# consistency, and this is the whole of the gap.
: "${MENU_SUBMENU_HINT:=Up/Down navigate   Enter confirm   q back}"
export MENU_SUBMENU_HINT

# Re-resolve the TTY seam after a caller changes AGENTBOT_TUI_INPUT/OUTPUT
# mid-process (the token and workspace menus do this to capture output).
tui_refresh_tty_seam() {
	DOTFILES_TTY_INPUT="${AGENTBOT_TUI_INPUT:-$_AGENTBOT_TUI_BASE_INPUT}"
	DOTFILES_TTY_OUTPUT="${AGENTBOT_TUI_OUTPUT:-$_AGENTBOT_TUI_BASE_OUTPUT}"
	DOTFILES_TTY_IN_FD="${AGENTBOT_TUI_IN_FD:-$_AGENTBOT_TUI_BASE_IN_FD}"
	DOTFILES_TTY_OUT_FD="${AGENTBOT_TUI_OUT_FD:-$_AGENTBOT_TUI_BASE_OUT_FD}"
	export DOTFILES_TTY_INPUT DOTFILES_TTY_OUTPUT DOTFILES_TTY_IN_FD DOTFILES_TTY_OUT_FD
	menu_tty_invalidate_size
}

tui_init_colors() { ui_init_colors; }
tui_init_colors

# Agentbot reads its width override from AGENTBOT_MENU_COLS.
tui_cols() {
	if [[ -n "${AGENTBOT_MENU_COLS:-}" ]]; then
		printf '%s\n' "$AGENTBOT_MENU_COLS"
		return 0
	fi
	menu_tty_cols
}

tui_fit() { menu_fit_line "$1" "$(($2 + 1))"; }
tui_clear() { ui_clear; }
tui_header() { ui_print_header "$1" "${2:-}" "${3:-$(tui_cols)}"; }
tui_section() { ui_print_section "$1" "${2:-$(tui_cols)}"; }
tui_shortcuts() { ui_format_shortcuts "$@"; }
tui_color_input_hint() { ui_color_input_hint "$1"; }
tui_print() {
	tui_refresh_tty_seam
	tty_printf "$@"
}

_tui_color_backend_line() {
	local line="$1" terminator="${2-$'\n'}"
	line="${line//\[STEP\]/${C_BOLD}${C_CYAN}[STEP]${C_RESET}}"
	line="${line//\[OK\]/${C_BOLD}${C_GREEN}[OK]${C_RESET}}"
	line="${line//\[FAIL\]/${C_BOLD}${C_RED}[FAIL]${C_RESET}}"
	line="${line//\[ERR\]/${C_BOLD}${C_RED}[ERR]${C_RESET}}"
	line="${line//\[WARN\]/${C_BOLD}${C_YELLOW}[WARN]${C_RESET}}"
	line="${line//\[INFO\]/${C_CYAN}[INFO]${C_RESET}}"
	printf '%s%s' "$line" "$terminator"
}

_tui_color_backend_stream() {
	local line=''
	while IFS= read -r line; do
		_tui_color_backend_line "$line" $'\n'
	done
	[[ -z "$line" ]] || _tui_color_backend_line "$line" ''
}

tui_run_to_output() {
	local -a pipeline_status
	tui_refresh_tty_seam
	if tty_use_output_fd; then
		"$@" 2>&1 | _tui_color_backend_stream >&"$DOTFILES_TTY_OUT_FD"
		pipeline_status=("${PIPESTATUS[@]}")
	else
		local output_path
		tty_output_available || return 1
		output_path="$(tty_output_path)"
		"$@" 2>&1 | _tui_color_backend_stream >>"$output_path"
		pipeline_status=("${PIPESTATUS[@]}")
	fi
	if ((pipeline_status[0] != 0)); then
		return "${pipeline_status[0]}"
	fi
	return "${pipeline_status[1]}"
}
tui_pause() {
	tui_refresh_tty_seam
	ui_pause
}
tui_wait_back() {
	tui_refresh_tty_seam
	ui_wait_back
}
tui_redraw_up() { menu_redraw_up "$1"; }

tui_confirm() {
	tui_refresh_tty_seam
	ui_confirm_yes_no "$1" true
}

# --- Four-column tables -------------------------------------------------
#
# Widths are proportional to the terminal, which is what Agentbot always did
# and is why its tables stay readable below ~90 columns. Dotfiles' fixed-width
# report tables live alongside as rt_print_four_column_*.
tui_table_widths() {
	local cols="$1" available
	available=$((cols - 11))
	((available < 4)) && available=4
	TUI_TABLE_W1=$((available * 20 / 100))
	TUI_TABLE_W2=$((available * 28 / 100))
	TUI_TABLE_W3=$((available * 27 / 100))
	TUI_TABLE_W4=$((available - TUI_TABLE_W1 - TUI_TABLE_W2 - TUI_TABLE_W3))
	((TUI_TABLE_W1 < 1)) && TUI_TABLE_W1=1
	((TUI_TABLE_W2 < 1)) && TUI_TABLE_W2=1
	((TUI_TABLE_W3 < 1)) && TUI_TABLE_W3=1
	((TUI_TABLE_W4 < 1)) && TUI_TABLE_W4=1
	return 0
}

tui_table_cell() {
	local text="$1" width="$2" color="${3:-}" fit padding
	fit="$(tui_fit "$text" "$width")"
	printf '%s%s%s' "$color" "$fit" "${color:+$C_RESET}"
	padding=$((width - ${#fit}))
	((padding > 0)) && printf '%*s' "$padding" ''
	return 0
}

tui_table_rule() { _rt_rule "$1"; }

tui_table_header() {
	local cols="$1" h1="$2" h2="$3" h3="$4" h4="$5"
	tui_table_widths "$cols"
	printf '  %s%s' "$C_BOLD" "$C_WHITE"
	tui_table_cell "$h1" "$TUI_TABLE_W1"
	printf ' | '
	tui_table_cell "$h2" "$TUI_TABLE_W2"
	printf ' | '
	tui_table_cell "$h3" "$TUI_TABLE_W3"
	printf ' | '
	tui_table_cell "$h4" "$TUI_TABLE_W4"
	printf '%s\n' "$C_RESET"
	printf '  %s%s-+-%s-+-%s-+-%s%s\n' "$C_DIM" \
		"$(tui_table_rule "$TUI_TABLE_W1")" "$(tui_table_rule "$TUI_TABLE_W2")" \
		"$(tui_table_rule "$TUI_TABLE_W3")" "$(tui_table_rule "$TUI_TABLE_W4")" "$C_RESET"
}

tui_table_row() {
	local cols="$1" t1="$2" t2="$3" t3="$4" t4="$5" color3="${6:-}" color4="${7:-}"
	tui_table_widths "$cols"
	printf '  '
	tui_table_cell "$t1" "$TUI_TABLE_W1"
	printf ' | '
	tui_table_cell "$t2" "$TUI_TABLE_W2"
	printf ' | '
	tui_table_cell "$t3" "$TUI_TABLE_W3" "$color3"
	printf ' | '
	tui_table_cell "$t4" "$TUI_TABLE_W4" "$color4"
	printf '\n'
}

# --- Menu ---------------------------------------------------------------
#
# menu_simple_run comes from the shared stack and reports its choice in
# MENU_SIMPLE_RESULT. These remain for the Agentbot suites that assert on
# geometry directly.

# agentbot_menu_run: the interactive selection, in Python.
#
# ADR-0001. The Dotfiles menus cannot move -- they draw before that machine has
# an interpreter -- but this repository is not installed until python3 and
# PyYAML exist, so its menus can. One process per menu display, not one per
# keystroke: Python owns the loop and returns the chosen key, and dispatch stays
# here.
#
# The shared Bash loop remains the fallback and is not modified: scripts/lib/
# shared/tui is byte-identical with Dotfiles, and a Python path in there would
# put Python back on the pre-install path the boundary keeps clear.
#
# Reports its choice in MENU_SIMPLE_RESULT and returns non-zero when the
# operator cancelled, which is menu_simple_run's contract exactly.
# Shared code that runs a menu takes the loop from here rather than naming one,
# so menu_runner.sh stays language-neutral and Dotfiles keeps the Bash default.
MENU_SIMPLE_RUNNER=agentbot_menu_run

agentbot_menu_run() {
	local choice rc=0

	if ! command -v python3 >/dev/null 2>&1 || [[ ! -d "${AGENTBOT_HOME:-}/src/ui" ]]; then
		menu_simple_run
		return $?
	fi

	choice="$(_agentbot_menu_spec | (cd "$AGENTBOT_HOME" && PYTHONDONTWRITEBYTECODE=1 python3 -m src.ui.menu_select))" || rc=$?

	# 3 is "this did not work", and only that falls back -- a cancelled menu
	# must stay cancelled rather than being re-drawn by the other loop.
	if ((rc == 3)); then
		menu_simple_run
		return $?
	fi
	if ((rc != 0)); then
		MENU_SIMPLE_RESULT=''
		return 1
	fi
	MENU_SIMPLE_RESULT="$choice"
}

# The menu as `name<FS>value` lines. No quoting to get wrong, and a description
# with newlines travels as \x1e -- the same transport the probe classifier uses.
_agentbot_menu_spec() {
	local fs=$'\x1f' nl=$'\x1e' item
	local color=0

	[[ -n "${C_RESET:-}" ]] && color=1
	printf 'title%s%s\n' "$fs" "${MENU_SIMPLE_TITLE:-}"
	printf 'breadcrumb%s%s\n' "$fs" "${MENU_SIMPLE_BREADCRUMB:-}"
	printf 'hint%s%s\n' "$fs" "${MENU_SIMPLE_HINT:-}"
	printf 'cols%s%s\n' "$fs" "$(tui_cols)"
	printf 'color%s%s\n' "$fs" "$color"
	for item in "${MENU_SIMPLE_LABELS[@]}"; do
		printf 'label%s%s\n' "$fs" "${item//$'\n'/$nl}"
	done
	for item in "${MENU_SIMPLE_KEYS[@]}"; do
		printf 'key%s%s\n' "$fs" "$item"
	done
	for item in ${MENU_SIMPLE_TYPES[@]+"${MENU_SIMPLE_TYPES[@]}"}; do
		printf 'type%s%s\n' "$fs" "$item"
	done
	for item in ${MENU_SIMPLE_DESCS[@]+"${MENU_SIMPLE_DESCS[@]}"}; do
		printf 'desc%s%s\n' "$fs" "${item//$'\n'/$nl}"
	done
}
tui_menu_desc_lines() { menu_desc_footer_rows MENU_SIMPLE; }
tui_menu_lines() { _menu_simple_menu_lines "${#MENU_SIMPLE_LABELS[@]}"; }
tui_menu_draw() { _menu_simple_draw "$1" "${2:-$(tui_cols)}"; }

# Submenu helpers are defined by the shared menu_runner.sh:
#   tui_submenu_loop, tui_menu_declare_owns_pause
